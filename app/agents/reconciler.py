"""Reconciliation agent (SPEC §6.8).

Loads BAI2 statements from the bank, matches every line to a payment (reference first,
then amount + date window), settles matched payments, and turns everything else into an
exceptions-queue item. On an ACH return the invoice reopens, the vendor is flagged (no
auto-pay until a human clears it) and the LLM drafts a vendor notice for a person to send.
Matching is deterministic; the LLM only writes the notice.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import httpx
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import audit
from app.config import policy_config
from app.llm import ChatModel, render_prompt, structured_call
from app.models import (
    BankStatementLine,
    ExceptionKind,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    ReconException,
    Vendor,
    VendorBankAccount,
)
from app.payments import bai2
from app.payments.rcodes import describe
from app.rules.rails import add_business_days

ACTOR = "reconciler"
PAY_REF = re.compile(r"\bPAY(\d+)\b")


class VendorNotice(BaseModel):
    subject: str = Field(description="Email subject line")
    body: str = Field(description="Plain-text body, under 120 words")


class ReconReport(BaseModel):
    days: int = 0
    lines_new: int = 0
    matched: int = 0
    settled: list[int] = []
    returned: list[int] = []
    exceptions: list[int] = []
    statement_errors: dict[str, list[str]] = {}
    unmatched_lines: int = 0
    trusted_now: list[str] = []

    @property
    def fully_matched(self) -> bool:
        return self.unmatched_lines == 0 and not self.statement_errors


def _money(c: int) -> str:
    return f"${c / 100:,.2f}"


def load_statements(session: Session, client: httpx.Client, start: date, end: date,
                    report: ReconReport) -> None:
    d = start
    while d <= end:
        report.days += 1
        resp = client.get("/statement", params={"date": d.isoformat(), "format": "bai2"})
        resp.raise_for_status()
        lines, errors = bai2.parse(resp.text)
        if errors:  # never reconcile against a statement that fails its own control totals
            report.statement_errors[d.isoformat()] = errors
        else:
            for ln in lines:
                exists = session.exec(select(BankStatementLine).where(
                    BankStatementLine.date == ln.as_of, BankStatementLine.bank_reference == ln.bank_reference)).first()
                if exists:
                    continue
                session.add(BankStatementLine(date=ln.as_of, amount_cents=ln.amount_cents, type_code=ln.type_code,
                                              reference=ln.customer_reference, bank_reference=ln.bank_reference,
                                              detail=ln.detail or None, return_code=ln.return_code))
                report.lines_new += 1
        d += timedelta(days=1)
    session.flush()


def _open_exception(session: Session, **fields) -> ReconException:
    q = select(ReconException).where(ReconException.kind == fields["kind"], ReconException.status == "open")
    if fields.get("payment_id"):
        q = q.where(ReconException.payment_id == fields["payment_id"])
    if fields.get("statement_line_id"):
        q = q.where(ReconException.statement_line_id == fields["statement_line_id"])
    existing = session.exec(q).first()
    if existing:
        return existing
    exc = ReconException(**fields)
    session.add(exc)
    session.flush()
    audit.record(session, actor=ACTOR, entity="recon_exceptions", entity_id=exc.id, action="open",
                 after=exc, reason=exc.detail)
    return exc


def _payment_for(session: Session, line: BankStatementLine) -> Payment | None:
    m = PAY_REF.search(line.reference or "")
    if m:
        return session.get(Payment, int(m.group(1)))
    if line.type_code == "475":  # check paid: bank ref is our check number
        return session.exec(select(Payment).where(Payment.bank_reference == line.bank_reference)).first()
    return None


def _fallback_match(session: Session, line: BankStatementLine) -> Payment | None:
    window = policy_config()["reconciliation"]["fallback_date_window_days"]
    cands = [p for p in session.exec(select(Payment).where(Payment.status == PaymentStatus.sent,
                                                           Payment.amount_cents == line.amount_cents)).all()
             if p.expected_settle_date and abs((p.expected_settle_date - line.date).days) <= window]
    return cands[0] if len(cands) == 1 else None  # ambiguous -> a human decides


def draft_notice(session: Session, pay: Payment, line: BankStatementLine, llm: ChatModel | None) -> str | None:
    inv = session.get(Invoice, pay.invoice_id)
    vendor = session.get(Vendor, inv.vendor_id)
    reason, _ = describe(line.return_code)
    r = structured_call(VendorNotice, system=render_prompt("return_notice_system"),
                        user=render_prompt("return_notice_user", vendor=vendor.legal_name,
                                           invoice_number=inv.invoice_number, amount=_money(pay.amount_cents),
                                           sent_date=pay.scheduled_date, settle_date=pay.expected_settle_date,
                                           return_date=line.date, return_code=line.return_code,
                                           return_reason=reason), llm=llm)
    return r.value.model_dump_json() if r.ok else None


def _handle_return(session: Session, line: BankStatementLine, pay: Payment, report: ReconReport,
                   llm: ChatModel | None, with_notices: bool) -> None:
    inv = session.get(Invoice, pay.invoice_id)
    vendor = session.get(Vendor, inv.vendor_id)
    reason, action = describe(line.return_code)

    before = audit.snapshot(pay)
    pay.status, pay.return_code = PaymentStatus.returned, line.return_code
    audit.record(session, actor=ACTOR, entity="payments", entity_id=pay.id, action="returned", before=before,
                 after=pay, reason=f"{line.return_code} {reason}")
    before = audit.snapshot(inv)
    inv.status = InvoiceStatus.reopened
    audit.record(session, actor=ACTOR, entity="invoices", entity_id=inv.id, action="reopen", before=before,
                 after=inv, reason=f"payment {pay.id} returned {line.return_code}: invoice is unpaid again")
    before = audit.snapshot(vendor)
    vendor.flagged, vendor.flag_reason = True, f"ACH return {line.return_code} ({reason}) on payment {pay.id}"
    audit.record(session, actor=ACTOR, entity="vendors", entity_id=vendor.id, action="flag", before=before,
                 after=vendor, reason=vendor.flag_reason)
    acct = session.exec(select(VendorBankAccount).where(VendorBankAccount.vendor_id == vendor.id,
                                                        VendorBankAccount.active)).first()
    if acct and line.return_code in ("R02", "R03", "R04"):
        before = audit.snapshot(acct)
        acct.validated = False
        audit.record(session, actor=ACTOR, entity="vendor_bank_accounts", entity_id=acct.id, action="invalidate",
                     before=before, after=acct, reason=f"{line.return_code}: account must be re-verified")

    notice = draft_notice(session, pay, line, llm) if with_notices else None
    exc = _open_exception(session, kind=ExceptionKind.ach_return, payment_id=pay.id, statement_line_id=line.id,
                          detail=f"{vendor.legal_name} {inv.invoice_number} {_money(pay.amount_cents)} returned "
                                 f"{line.return_code} ({reason}). {action}")
    exc.draft_notice_json = exc.draft_notice_json or notice
    report.returned.append(pay.id)
    report.exceptions.append(exc.id)


def _settle(session: Session, line: BankStatementLine, pay: Payment, report: ReconReport) -> None:
    if pay.amount_cents != line.amount_cents:
        exc = _open_exception(session, kind=ExceptionKind.amount_mismatch, payment_id=pay.id,
                              statement_line_id=line.id,
                              detail=f"bank debited {_money(line.amount_cents)} for payment {pay.id} "
                                     f"of {_money(pay.amount_cents)}")
        report.exceptions.append(exc.id)
        return
    line.matched_payment_id = pay.id
    report.matched += 1
    if pay.status == PaymentStatus.sent:
        before = audit.snapshot(pay)
        pay.status = PaymentStatus.settled
        audit.record(session, actor=ACTOR, entity="payments", entity_id=pay.id, action="settle", before=before,
                     after=pay, reason=f"statement {line.date} {line.bank_reference}")
        inv = session.get(Invoice, pay.invoice_id)
        before = audit.snapshot(inv)
        inv.status = InvoiceStatus.paid
        audit.record(session, actor=ACTOR, entity="invoices", entity_id=inv.id, action="paid", before=before,
                     after=inv, reason=f"payment {pay.id} settled {line.date}")
        report.settled.append(pay.id)


def _update_trust(session: Session, report: ReconReport) -> None:
    """Onboarding rule: a vendor becomes trusted after N clean settled payments."""
    need = policy_config()["vendor_trust"]["min_settled_payments"]
    for v in session.exec(select(Vendor).where(Vendor.trusted == False, Vendor.flagged == False)).all():  # noqa: E712
        n = len(session.exec(select(Payment).join(Invoice).where(
            Invoice.vendor_id == v.id, Payment.status == PaymentStatus.settled)).all())
        if n >= need:
            before = audit.snapshot(v)
            v.trusted = True
            audit.record(session, actor=ACTOR, entity="vendors", entity_id=v.id, action="set_trusted",
                         before=before, after=v, reason=f"{n} clean settled payments")
            report.trusted_now.append(v.legal_name)


def reconcile(session: Session, *, client: httpx.Client, start: date, end: date, llm: ChatModel | None = None,
              with_notices: bool = True) -> ReconReport:
    report = ReconReport()
    load_statements(session, client, start, end, report)

    todo = session.exec(select(BankStatementLine).where(BankStatementLine.matched_payment_id == None,  # noqa: E711
                                                        BankStatementLine.date >= start,
                                                        BankStatementLine.date <= end)
                        .order_by(BankStatementLine.date, BankStatementLine.id)).all()
    for line in todo:
        pay = _payment_for(session, line) or (None if line.return_code else _fallback_match(session, line))
        if line.return_code and pay:
            line.matched_payment_id = pay.id
            report.matched += 1
            _handle_return(session, line, pay, report, llm, with_notices)
        elif pay:
            _settle(session, line, pay, report)
        else:
            exc = _open_exception(session, kind=ExceptionKind.unmatched_line, statement_line_id=line.id,
                                  detail=f"{line.date} {line.type_code} {_money(line.amount_cents)} "
                                         f"ref {line.bank_reference} / {line.reference or '-'}: no matching payment")
            report.exceptions.append(exc.id)
            report.unmatched_lines += 1

    grace = policy_config()["reconciliation"]["missing_settlement_grace_days"]
    for pay in session.exec(select(Payment).where(Payment.status == PaymentStatus.sent)).all():
        if pay.expected_settle_date and add_business_days(pay.expected_settle_date, grace) <= end:
            exc = _open_exception(session, kind=ExceptionKind.missing_settlement, payment_id=pay.id,
                                  detail=f"payment {pay.id} ({pay.rail.value}, {_money(pay.amount_cents)}) expected "
                                         f"to settle {pay.expected_settle_date} but is not on any statement")
            report.exceptions.append(exc.id)

    _update_trust(session, report)
    report.exceptions = sorted(set(report.exceptions))
    return report


def open_exceptions(session: Session) -> list[dict]:
    return [{**e.model_dump(mode="json"), "draft_notice": json.loads(e.draft_notice_json) if e.draft_notice_json else None}
            for e in session.exec(select(ReconException).where(ReconException.status == "open")).all()]
