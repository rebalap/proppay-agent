"""Rules-engine node: turns the facts gathered so far into a policy decision and persists it.

All of the judgement lives in app/rules/policy.py; this module only gathers inputs,
writes the result, and opens a call-back task when bank details changed.
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from app import audit
from app.models import (
    BankChangeRequest,
    ChangeRequestChannel,
    ChangeRequestStatus,
    Invoice,
    InvoiceStatus,
    Vendor,
)
from app.rules.policy import Decision, Outcome, PolicyInput, decide

ACTOR = "rules_engine"

STATUS_FOR = {
    Outcome.AUTO_APPROVE: InvoiceStatus.approved,
    Outcome.SINGLE_APPROVAL: InvoiceStatus.pending_approval,
    Outcome.DUAL_APPROVAL: InvoiceStatus.pending_approval,
    Outcome.HOLD_CALLBACK: InvoiceStatus.on_hold,
    Outcome.BLOCK: InvoiceStatus.blocked,
}


def policy_input(session: Session, invoice: Invoice, rail: str | None = None) -> PolicyInput:
    vendor = session.get(Vendor, invoice.vendor_id) if invoice.vendor_id else None
    signals = [s["code"] for s in json.loads(invoice.signals_json or "[]")]
    issues = json.loads(invoice.intake_issues_json or "[]")
    callback_verified = False
    if vendor and invoice.remit_routing:
        callback_verified = session.exec(select(BankChangeRequest).where(
            BankChangeRequest.vendor_id == vendor.id,
            BankChangeRequest.new_routing == invoice.remit_routing,
            BankChangeRequest.new_account_last4 == invoice.remit_account_last4,
            BankChangeRequest.status == ChangeRequestStatus.verified)).first() is not None
    return PolicyInput(
        amount_cents=invoice.amount_cents,
        vendor_trusted=bool(vendor and vendor.trusted),
        vendor_flagged=bool(vendor and vendor.flagged),
        match_status=invoice.match_status,
        risk_score=invoice.risk_score or 0,
        signals=signals,
        intake_needs_review=invoice.extracted_json is None or any(i["blocking"] for i in issues),
        callback_verified=callback_verified,
        rail=rail,
    )


def _open_callback_task(session: Session, invoice: Invoice) -> None:
    if not invoice.vendor_id or not invoice.remit_routing:
        return
    exists = session.exec(select(BankChangeRequest).where(
        BankChangeRequest.invoice_id == invoice.id)).first()
    if exists:
        return
    req = BankChangeRequest(
        vendor_id=invoice.vendor_id, invoice_id=invoice.id,
        requested_via=ChangeRequestChannel.email if invoice.channel == "email" else ChangeRequestChannel.portal,
        new_routing=invoice.remit_routing, new_account_last4=invoice.remit_account_last4,
    )
    session.add(req)
    session.flush()
    audit.record(session, actor=ACTOR, entity="bank_change_requests", entity_id=req.id, action="open_callback",
                 after=req, reason="bank details differ from the account on file; verify by phone before paying")


def run_decision(session: Session, invoice_id: int, rail: str | None = None) -> Decision:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise KeyError(f"invoice {invoice_id} not found")
    decision = decide(policy_input(session, invoice, rail))

    before = audit.snapshot(invoice)
    invoice.decision = decision.outcome.value
    invoice.decision_reasons_json = json.dumps(decision.reasons)
    invoice.status = STATUS_FOR[decision.outcome]
    if decision.outcome == Outcome.HOLD_CALLBACK:
        _open_callback_task(session, invoice)
    audit.record(session, actor=ACTOR, entity="invoices", entity_id=invoice.id, action="decide",
                 before=before, after=invoice, reason=f"{decision.outcome.value}: {'; '.join(decision.reasons)}")
    session.add(invoice)
    return decision
