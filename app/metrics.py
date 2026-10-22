"""Dashboard metrics (SPEC §10). Computed from the DB; money in integer cents."""

from __future__ import annotations

import glob
import json
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import ROOT, rails_config
from app.models import AuditLog, Invoice, InvoiceStatus, Payment, PaymentStatus, ReconException

AGENT_ACTORS = {"intake_agent", "match_agent", "fraud_agent", "rules_engine", "rail_optimizer", "executor",
                "reconciler", "seed_script"}
MONEY_OUT = (PaymentStatus.sent, PaymentStatus.settled)


class Metrics(BaseModel):
    invoices: int
    processed: int
    stp_count: int
    stp_rate: float
    awaiting_human: int
    paid_cents: int
    paid_by_rail: dict[str, int]
    fees_cents: int
    wire_baseline_cents: int
    rail_savings_cents: int
    discounts_cents: int
    rebates_cents: int
    fraud_prevented_cents: int
    fraud_prevented_count: int
    sanctions_blocked_count: int
    returns_count: int
    exceptions_open: int
    extraction_accuracy: float | None = None
    decision_accuracy: float | None = None
    eval_model: str | None = None


def run_invoices(session: Session) -> list[Invoice]:
    """Invoices of the current AP run (everything except the seeded payment history)."""
    return session.exec(select(Invoice).where(Invoice.source_file.not_like("history/%"))
                        .order_by(Invoice.id)).all()


def human_touched(session: Session, invoices: list[Invoice]) -> set[int]:
    """Invoices a person had to act on: any decision that needed a human, or a person editing the invoice."""
    touched = {i.id for i in invoices if i.decision and i.decision != "AUTO_APPROVE"}
    rows = session.exec(select(AuditLog).where(AuditLog.entity == "invoices",
                                               AuditLog.entity_id.in_([i.id for i in invoices]))).all()
    return touched | {r.entity_id for r in rows if r.actor not in AGENT_ACTORS}


def _latest_eval(prefix: str) -> dict | None:
    files = sorted(glob.glob(str(ROOT / "evals" / "results" / f"{prefix}_*.json")), key=lambda f: Path(f).stat().st_mtime)
    return json.loads(Path(files[-1]).read_text()) if files else None


def compute(session: Session) -> Metrics:
    invs = run_invoices(session)
    ids = [i.id for i in invs]
    by_id = {i.id: i for i in invs}
    pays = session.exec(select(Payment).where(Payment.invoice_id.in_(ids))).all() if ids else []
    touched = human_touched(session, invs) if invs else set()

    out = [p for p in pays if p.status in MONEY_OUT]
    stp = {p.invoice_id for p in out
           if by_id[p.invoice_id].decision == "AUTO_APPROVE" and p.invoice_id not in touched}
    processed = [i for i in invs if i.status != InvoiceStatus.received]
    by_rail: dict[str, int] = defaultdict(int)
    for p in out:
        by_rail[p.rail.value] += p.amount_cents
    wire_fee = rails_config()["rails"]["wire"]["fee_cents"]
    fees = sum(p.fee_cents for p in out)
    fraud = [i for i in invs if i.is_fraud and i.status in (InvoiceStatus.blocked, InvoiceStatus.on_hold)]
    sanctions = [i for i in invs if i.decision == "BLOCK" and "OFAC_HIT" in (i.signals_json or "")]
    ext, dec = _latest_eval("extraction"), _latest_eval("decisions")
    return Metrics(
        invoices=len(invs), processed=len(processed), stp_count=len(stp),
        stp_rate=len(stp) / len(processed) if processed else 0.0,
        awaiting_human=sum(i.status in (InvoiceStatus.pending_approval, InvoiceStatus.on_hold) for i in invs),
        paid_cents=sum(p.amount_cents for p in out), paid_by_rail=dict(by_rail),
        fees_cents=fees, wire_baseline_cents=wire_fee * len(out), rail_savings_cents=wire_fee * len(out) - fees,
        discounts_cents=sum(p.discount_cents for p in out), rebates_cents=sum(p.rebate_cents for p in out),
        fraud_prevented_cents=sum(i.amount_cents or 0 for i in fraud), fraud_prevented_count=len(fraud),
        sanctions_blocked_count=len(sanctions),
        returns_count=sum(p.status == PaymentStatus.returned for p in pays),
        exceptions_open=len(session.exec(select(ReconException).where(ReconException.status == "open")).all()),
        extraction_accuracy=ext["field_accuracy"] if ext else None,
        decision_accuracy=dec["decision_accuracy"] if dec else None,
        eval_model=(ext or dec or {}).get("model"),
    )
