"""Fraud monitoring agent (SPEC §6.3).

Signals and the risk score are deterministic (app/rules/fraud_signals.py). The LLM
only writes a rationale for the human reviewer, and only when there is something
to explain.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from sqlmodel import Session, select

from app import audit
from app.llm import ChatModel, render_prompt, structured_call
from app.models import Invoice, Payment, PaymentStatus, Vendor, VendorBankAccount
from app.rules.fraud_signals import FraudContext, PriorInvoice, Signal, detect, risk_score
from app.rules.names import normalize_name
from app.schemas import FraudRationale

ACTOR = "fraud_agent"


class FraudResult(BaseModel):
    signals: list[Signal]
    risk_score: int
    rationale: FraudRationale | None = None
    rationale_error: str | None = None


@lru_cache
def load_sdn(path: Path) -> tuple[str, ...]:
    with path.open() as f:
        return tuple(row["sdn_name"] for row in csv.DictReader(f))


def build_context(session: Session, invoice: Invoice, today: date, sdn_path: Path) -> FraudContext:
    ext = json.loads(invoice.extracted_json) if invoice.extracted_json else {}
    vendors = session.exec(select(Vendor)).all()
    vendor = session.get(Vendor, invoice.vendor_id) if invoice.vendor_id else None

    active = None
    settled: list[int] = []
    prior: list[PriorInvoice] = []
    if vendor:
        active = session.exec(select(VendorBankAccount).where(
            VendorBankAccount.vendor_id == vendor.id, VendorBankAccount.active)).first()
        settled = [p.amount_cents for p in session.exec(select(Payment).join(Invoice).where(
            Invoice.vendor_id == vendor.id, Payment.status == PaymentStatus.settled)).all()]
        prior = [PriorInvoice(i.id, i.invoice_number, i.amount_cents, i.invoice_date, i.status.value)
                 for i in session.exec(select(Invoice).where(Invoice.vendor_id == vendor.id,
                                                             Invoice.id != invoice.id)).all()]

    sender = invoice.sender_email
    return FraudContext(
        today=today,
        invoice_id=invoice.id,
        vendor_name_on_invoice=ext.get("vendor_name"),
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        amount_cents=invoice.amount_cents,
        remit_routing=invoice.remit_routing,
        remit_last4=invoice.remit_account_last4,
        channel=invoice.channel,
        sender_domain=sender.split("@")[1] if sender and "@" in sender else None,
        email_mentions_bank_change=bool(ext.get("email_mentions_bank_change")),
        pressure_language=ext.get("pressure_language"),
        vendor_id=vendor.id if vendor else None,
        vendor_legal_name=vendor.legal_name if vendor else None,
        vendor_domain=vendor.email_domain if vendor else None,
        active_routing=active.routing if active else None,
        active_last4=active.account_last4 if active else None,
        active_since=active.effective_from if active else None,
        settled_amounts=settled,
        prior_invoices=prior,
        known_vendor_names={normalize_name(v.legal_name): v.legal_name for v in vendors},
        known_domains={v.email_domain: v.legal_name for v in vendors if v.email_domain},
        sdn_names=list(load_sdn(sdn_path)),
    )


def explain(invoice: Invoice, ctx: FraudContext, signals: list[Signal], score: int,
            llm: ChatModel | None = None):
    lines = "\n".join(f"- {s.code} (weight {s.weight}): {s.evidence}" for s in signals)
    amount = f"${invoice.amount_cents / 100:,.2f}" if invoice.amount_cents is not None else "unknown amount"
    return structured_call(
        FraudRationale,
        system=render_prompt("fraud_rationale_system"),
        user=render_prompt("fraud_rationale_user", invoice_number=invoice.invoice_number or "?",
                           vendor_name=ctx.vendor_legal_name or ctx.vendor_name_on_invoice or "unknown vendor",
                           amount=amount, channel=invoice.channel or "unknown",
                           sender=f"from {invoice.sender_email}" if invoice.sender_email else "",
                           risk_score=score, signals=lines),
        llm=llm,
    )


def run_fraud(session: Session, invoice_id: int, *, today: date, data_dir: Path,
              llm: ChatModel | None = None, with_rationale: bool = True) -> FraudResult:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise KeyError(f"invoice {invoice_id} not found")
    ctx = build_context(session, invoice, today, data_dir / "ofac" / "sdn.csv")
    signals = detect(ctx)
    result = FraudResult(signals=signals, risk_score=risk_score(signals))

    if signals and with_rationale:
        r = explain(invoice, ctx, signals, result.risk_score, llm=llm)
        if r.ok:
            result.rationale = r.value
        else:  # the reviewer still sees every signal's evidence; only the prose is missing
            result.rationale_error = r.error

    before = audit.snapshot(invoice)
    invoice.risk_score = result.risk_score
    invoice.signals_json = json.dumps([s.model_dump() for s in signals])
    invoice.fraud_rationale_json = result.rationale.model_dump_json() if result.rationale else None
    audit.record(session, actor=ACTOR, entity="invoices", entity_id=invoice.id, action="fraud_screen",
                 before=before, after=invoice,
                 reason=f"risk {result.risk_score}: {', '.join(s.code for s in signals) or 'no signals'}")
    session.add(invoice)
    return result
