"""Match agent (SPEC §6.2): resolve the vendor, then 3-way match invoice <-> PO <-> work order.

Fully deterministic apart from the embedding lookup, which is only consulted when
fuzzy matching is inconclusive and never overrides a confident fuzzy result.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel
from rapidfuzz import fuzz
from sqlmodel import Session, select

from app import audit
from app.config import get_settings, policy_config
from app.models import Invoice, InvoiceStatus, POStatus, PurchaseOrder, Vendor, WorkOrder, WorkOrderStatus
from app.rules.names import normalize_name

log = logging.getLogger(__name__)
ACTOR = "match_agent"

Embedder = Callable[[list[str]], list[list[float]]]


class Candidate(BaseModel):
    vendor_id: int
    name: str
    fuzzy: float
    embedding: float | None = None


class VendorResolution(BaseModel):
    vendor_id: int | None
    vendor_name: str | None
    method: Literal["fuzzy", "embedding", "none", "ambiguous"]
    score: float
    candidates: list[Candidate]
    note: str | None = None


class MatchResult(BaseModel):
    vendor: VendorResolution
    po_id: int | None = None
    match_status: Literal["full", "partial", "none"]
    checks: dict[str, bool]
    reasons: list[str]


# --- vendor resolution -----------------------------------------------------------

_embed_cache: dict[str, list[float]] = {}


def ollama_embedder() -> Embedder:
    from langchain_ollama import OllamaEmbeddings

    s = get_settings()
    return OllamaEmbeddings(model=s.embed_model, base_url=s.ollama_base_url).embed_documents


def _embed(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    missing = [t for t in texts if t not in _embed_cache]
    if missing:
        for t, v in zip(missing, embedder(missing)):
            _embed_cache[t] = v
    return [_embed_cache[t] for t in texts]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


def _names(v: Vendor) -> list[str]:
    return [n for n in (v.legal_name, v.dba) if n]


def resolve_vendor(session: Session, name: str | None, embedder: Embedder | None = None) -> VendorResolution:
    cfg = policy_config()["vendor_resolution"]
    if not name or not normalize_name(name):
        return VendorResolution(vendor_id=None, vendor_name=None, method="none", score=0, candidates=[],
                                note="no vendor name extracted")
    target = normalize_name(name)
    vendors = session.exec(select(Vendor)).all()
    cands = [
        Candidate(vendor_id=v.id, name=v.legal_name,
                  fuzzy=max(fuzz.token_sort_ratio(target, normalize_name(n)) for n in _names(v)))
        for v in vendors
    ]
    cands.sort(key=lambda c: c.fuzzy, reverse=True)
    top = cands[:5]
    best = top[0] if top else None

    if best and best.fuzzy >= cfg["fuzzy_min"]:
        if len(top) > 1 and top[1].fuzzy >= cfg["fuzzy_min"] and best.fuzzy - top[1].fuzzy < cfg["ambiguity_margin"]:
            return VendorResolution(vendor_id=None, vendor_name=None, method="ambiguous", score=best.fuzzy,
                                    candidates=top, note="two vendors match almost equally")
        return VendorResolution(vendor_id=best.vendor_id, vendor_name=best.name, method="fuzzy",
                                score=best.fuzzy, candidates=top)

    # Fuzzy inconclusive: try semantic similarity over the whole vendor master.
    embedder = embedder or ollama_embedder()
    try:
        names = [c.name for c in cands]
        vecs = _embed(embedder, [name, *names])
    except Exception as exc:  # embedding service down: degrade to "unresolved", never guess
        log.warning("embedding lookup failed: %s", exc)
        return VendorResolution(vendor_id=None, vendor_name=None, method="none", score=best.fuzzy if best else 0,
                                candidates=top, note=f"embedding unavailable: {type(exc).__name__}")
    for c, v in zip(cands, vecs[1:]):
        c.embedding = round(_cosine(vecs[0], v), 4)
    by_embed = sorted(cands, key=lambda c: c.embedding or 0, reverse=True)
    e_best = by_embed[0]
    runner_up = by_embed[1].embedding if len(by_embed) > 1 else 0.0
    if (e_best.embedding >= cfg["embedding_min"] and e_best.embedding - runner_up >= cfg["embedding_margin"]
            and e_best.fuzzy >= cfg["embedding_fuzzy_floor"]):
        return VendorResolution(vendor_id=e_best.vendor_id, vendor_name=e_best.name, method="embedding",
                                score=e_best.embedding, candidates=by_embed[:5])
    return VendorResolution(vendor_id=None, vendor_name=None, method="none", score=e_best.embedding or 0,
                            candidates=by_embed[:5], note="no vendor in the master file is close enough")


# --- 3-way match -----------------------------------------------------------------------


def three_way_match(session: Session, vendor: VendorResolution, po_number: str | None,
                    amount_cents: int | None) -> MatchResult:
    tol_pct = policy_config()["match"]["po_amount_tolerance_pct"]
    checks: dict[str, bool] = {"vendor_resolved": vendor.vendor_id is not None}
    reasons: list[str] = []

    def done(status: str, po_id: int | None = None) -> MatchResult:
        return MatchResult(vendor=vendor, po_id=po_id, match_status=status, checks=checks, reasons=reasons)

    if vendor.vendor_id is None:
        reasons.append(f"vendor not resolved ({vendor.note or vendor.method})")
        return done("none")

    checks["po_referenced"] = bool(po_number)
    if not po_number:
        reasons.append("invoice has no PO number")
        return done("none")

    po = session.exec(select(PurchaseOrder).where(PurchaseOrder.po_number == po_number.strip())).first()
    checks["po_found"] = po is not None
    if po is None:
        reasons.append(f"purchase order {po_number} not found")
        return done("none")

    checks["po_vendor_matches"] = po.vendor_id == vendor.vendor_id
    if not checks["po_vendor_matches"]:
        reasons.append(f"purchase order {po.po_number} belongs to a different vendor")
        return done("none", po.id)

    checks["po_open"] = po.status == POStatus.open
    if not checks["po_open"]:
        reasons.append(f"purchase order {po.po_number} is already {po.status.value} (possible duplicate billing)")

    if amount_cents is None:
        checks["amount_within_tolerance"] = False
        reasons.append("invoice amount missing")
    else:
        diff = abs(amount_cents - po.amount_cents)
        checks["amount_within_tolerance"] = diff * 100 <= po.amount_cents * tol_pct
        if not checks["amount_within_tolerance"]:
            reasons.append(f"invoice ${amount_cents / 100:,.2f} vs purchase order ${po.amount_cents / 100:,.2f} is more than {tol_pct}% apart")

    wos = session.exec(select(WorkOrder).where(WorkOrder.po_id == po.id)).all()
    checks["work_order_completed"] = bool(wos) and all(w.status == WorkOrderStatus.completed for w in wos)
    if not checks["work_order_completed"]:
        reasons.append("work order not completed" if wos else "no work order for this PO")

    if all(checks.values()):
        reasons.append("vendor, PO amount and completed work order all match")
        return done("full", po.id)
    return done("partial", po.id)


def run_match(session: Session, invoice_id: int, embedder: Embedder | None = None) -> MatchResult:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise KeyError(f"invoice {invoice_id} not found")
    extracted = json.loads(invoice.extracted_json) if invoice.extracted_json else {}
    vendor = resolve_vendor(session, extracted.get("vendor_name"), embedder=embedder)
    result = three_way_match(session, vendor, invoice.po_number, invoice.amount_cents)

    before = audit.snapshot(invoice)
    invoice.vendor_id = vendor.vendor_id
    invoice.match_status = result.match_status
    invoice.match_json = result.model_dump_json()
    if result.match_status == "full" and invoice.status == InvoiceStatus.extracted:
        invoice.status = InvoiceStatus.matched
    audit.record(session, actor=ACTOR, entity="invoices", entity_id=invoice.id, action="three_way_match",
                 before=before, after=invoice, reason=f"{result.match_status}: {'; '.join(result.reasons)}")
    session.add(invoice)
    return result
