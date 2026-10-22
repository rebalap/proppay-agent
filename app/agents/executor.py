"""Execution agent (SPEC §6.6): send released payments to the (mock) bank.

- ACH (standard + same-day): one NACHA CCD+ file per run, one batch per effective date.
- Checks: positive-pay issue file registered with the bank.
- RTP / FedNow, wire, card: one API call each, with Idempotency-Key = "{payment_id}-{attempt}".
  A transport error is retried once with the SAME key, so a timeout can never pay twice.

Nothing is sent unless app/rules/policy.release_allowed() says so for that payment.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import BaseModel
from sqlmodel import Session, select

from app import audit
from app.config import rails_config
from app.models import (
    AccountType,
    Approval,
    ApprovalDecision,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentBatch,
    PaymentStatus,
    Rail,
    Vendor,
    VendorBankAccount,
)
from app.payments import nacha, positive_pay, vault
from app.rules.policy import ApprovalRecord, release_allowed

ACTOR = "executor"
FILE_ID_MODIFIERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # NACHA: unique per origin per day
ACH_RAILS = {Rail.ach_standard, Rail.ach_same_day}
API_RAILS = {Rail.rtp: "/instant", Rail.fednow: "/instant", Rail.wire: "/wire", Rail.virtual_card: "/card"}


class ExecutionReport(BaseModel):
    sent: list[int] = []
    skipped: dict[int, str] = {}
    failed: dict[int, str] = {}
    ach_file: str | None = None
    positive_pay_file: str | None = None


def approvals_for(session: Session, payment: Payment) -> list[ApprovalRecord]:
    out = []
    for a in session.exec(select(Approval).where(Approval.payment_id == payment.id)).all():
        approved = a.decision == ApprovalDecision.approved
        for who in (a.approver_1, a.approver_2):
            if who:
                out.append(ApprovalRecord(approver=who, approved=approved))
        if a.decision == ApprovalDecision.rejected and not (a.approver_1 or a.approver_2):
            out.append(ApprovalRecord(approver="unknown", approved=False))
    return out


def _mark_sent(session: Session, payment: Payment, reference: str | None, batch_id: int | None, reason: str) -> None:
    before = audit.snapshot(payment)
    payment.status = PaymentStatus.sent
    payment.bank_reference = reference
    payment.batch_id = batch_id
    invoice = session.get(Invoice, payment.invoice_id)
    invoice.status = InvoiceStatus.scheduled
    audit.record(session, actor=ACTOR, entity="payments", entity_id=payment.id, action="send",
                 before=before, after=payment, reason=reason)


def _post_raw(client: httpx.Client, path: str, **kwargs) -> httpx.Response:
    """POST, retrying a transport error once with the identical request (same Idempotency-Key)."""
    try:
        return client.post(path, **kwargs)
    except httpx.TransportError:
        return client.post(path, **kwargs)  # same key: the bank de-duplicates


def _post(client: httpx.Client, path: str, payment: Payment, body: dict) -> httpx.Response:
    return _post_raw(client, path, json=body, headers={"Idempotency-Key": payment.idempotency_key})


def _submit_ach(session: Session, client: httpx.Client, path: Path, report: ExecutionReport) -> None:
    batches = session.exec(select(PaymentBatch).where(PaymentBatch.file_path == str(path))).all()
    pays = session.exec(select(Payment).where(Payment.batch_id.in_([pb.id for pb in batches]))).all()
    try:
        resp = _post_raw(client, "/ach/files", content=path.read_text(),
                         headers={"Idempotency-Key": batches[0].idempotency_key, "Content-Type": "text/plain"})
    except httpx.TransportError as exc:
        for p in pays:
            report.failed[p.id] = f"bank unreachable; {path.name} will be resent unchanged: {exc}"
        return
    if resp.status_code >= 400:
        for p in pays:
            report.failed[p.id] = f"ACH file rejected: {resp.text[:200]}"
        return
    file_id = resp.json()["file_id"]
    for pb in batches:
        pb.submitted = True
        audit.record(session, actor=ACTOR, entity="payment_batches", entity_id=pb.id, action="submit_ach",
                     after=pb, reason=f"NACHA file {path.name} accepted as {file_id}")
    for p in pays:
        _mark_sent(session, p, file_id, p.batch_id, f"in ACH batch {p.batch_id}, effective {p.expected_settle_date}")
        report.sent.append(p.id)


def execute(session: Session, *, client: httpx.Client, now: datetime, outbox: Path,
            payment_ids: list[int] | None = None) -> ExecutionReport:
    report = ExecutionReport()
    origin_cfg = rails_config()["originator"]
    q = select(Payment).where(Payment.status == PaymentStatus.scheduled)
    if payment_ids is not None:
        q = q.where(Payment.id.in_(payment_ids))
    payments = session.exec(q.order_by(Payment.id)).all()

    released: list[tuple[Payment, Invoice, Vendor]] = []
    for p in payments:
        inv = session.get(Invoice, p.invoice_id)
        ok, why = release_allowed(inv.decision, approvals_for(session, p))
        if not ok:
            report.skipped[p.id] = why
            continue
        if p.rail in (Rail.rtp, Rail.fednow) and p.scheduled_date > now.date():
            report.skipped[p.id] = "instant payments cannot be future-dated"
            continue
        released.append((p, inv, session.get(Vendor, inv.vendor_id)))

    # --- ACH: one file, one batch per effective (settlement) date -------------------------
    # A file the bank hasn't confirmed is resent as-is with its original key, never rebuilt: rebuilding would
    # give the same payments a new key, and a file that actually arrived would then be paid twice.
    for path_str in sorted({pb.file_path for pb in session.exec(
            select(PaymentBatch).where(PaymentBatch.submitted == False)).all()}):  # noqa: E712
        _submit_ach(session, client, Path(path_str), report)

    ach = [(p, i, v) for p, i, v in released if p.rail in ACH_RAILS and p.batch_id is None]
    if ach:
        by_date: dict = {}
        for p, inv, v in ach:
            acct = session.exec(select(VendorBankAccount).where(VendorBankAccount.vendor_id == v.id,
                                                                VendorBankAccount.active)).one()
            entry = nacha.Entry(
                routing=acct.routing, account_number=vault.detokenize(session, acct.account_hash),
                account_type="savings" if acct.account_type == AccountType.savings else "checking",
                amount_cents=p.amount_cents, individual_id=f"PAY{p.id}", receiver_name=v.legal_name,
                remittance=nacha.remittance_rmr(inv.invoice_number, p.amount_cents))
            by_date.setdefault(p.expected_settle_date, []).append((p, entry))
        batches = [nacha.Batch(d, [e for _, e in items], origin_cfg["entry_description"])
                   for d, items in sorted(by_date.items())]
        origin = nacha.Originator(origin_cfg["company_name"], origin_cfg["company_id"],
                                  origin_cfg["odfi_routing"], origin_cfg["odfi_name"])
        sent_today = {pb.file_path for pb in session.exec(select(PaymentBatch).where(
            PaymentBatch.file_path.like(f"%ach_{now:%Y%m%d}_%"))).all()}
        if len(sent_today) >= len(FILE_ID_MODIFIERS):
            raise RuntimeError("NACHA allows at most 36 files per day")
        modifier = FILE_ID_MODIFIERS[len(sent_today)]
        text = nacha.build_file(origin, batches, now, file_id_modifier=modifier)
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"ach_{now:%Y%m%d}_{modifier}.txt"
        if path.exists():  # never overwrite a file the bank may already hold
            raise RuntimeError(f"{path} already exists")
        path.write_text(text)
        report.ach_file = str(path)
        for d, items in sorted(by_date.items()):
            pb = PaymentBatch(rail=Rail.ach_standard if all(p.rail == Rail.ach_standard for p, _ in items)
                              else Rail.ach_same_day, file_path=str(path),
                              total_cents=sum(p.amount_cents for p, _ in items), entry_count=len(items),
                              idempotency_key=f"achfile-{path.name}", submitted=False)
            session.add(pb)
            session.flush()
            for p, _ in items:
                p.batch_id = pb.id  # reserved for this file until the bank confirms it
        _submit_ach(session, client, path, report)

    # --- checks: positive pay ---------------------------------------------------------------
    checks = [(p, i, v) for p, i, v in released if p.rail == Rail.check]
    if checks:
        first = origin_cfg["first_check_number"] + len(session.exec(select(Payment).where(
            Payment.rail == Rail.check, Payment.status != PaymentStatus.scheduled)).all())
        issued = [positive_pay.IssuedCheck(first + n, p.amount_cents, p.scheduled_date, v.legal_name)
                  for n, (p, _, v) in enumerate(checks)]
        csv_text = positive_pay.build_csv(origin_cfg["operating_account"], issued)
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"positive_pay_{now:%Y%m%d}_{issued[0].check_number}.csv"
        path.write_text(csv_text)
        report.positive_pay_file = str(path)
        try:  # same payments -> same CSV and key, so a retry is safe
            resp = _post_raw(client, "/positive-pay", json={"csv": csv_text},
                             headers={"Idempotency-Key": f"pospay-{'-'.join(str(p.id) for p, _, _ in checks)}"})
        except httpx.TransportError as exc:
            for p, _, _ in checks:
                report.failed[p.id] = f"bank unreachable after retry: {exc}"
            checks, issued = [], []
            resp = None
        for (p, _, _), chk in zip(checks, issued):
            if resp.status_code >= 400:
                report.failed[p.id] = f"positive pay rejected: {resp.text[:200]}"
            else:
                _mark_sent(session, p, f"CHK{chk.check_number}", None, f"check {chk.check_number} on positive pay")
                report.sent.append(p.id)

    # --- API rails ------------------------------------------------------------------------------
    for p, inv, v in released:
        if p.rail not in API_RAILS:
            continue
        acct = session.exec(select(VendorBankAccount).where(VendorBankAccount.vendor_id == v.id,
                                                            VendorBankAccount.active)).first()
        ref = f"{inv.invoice_number} PAY{p.id}"
        if p.rail in (Rail.rtp, Rail.fednow):
            body = {"rail": p.rail.value, "amount_cents": p.amount_cents, "creditor_routing": acct.routing,
                    "creditor_account": vault.detokenize(session, acct.account_hash), "creditor_name": v.legal_name,
                    "reference": ref, "value_date": p.scheduled_date.isoformat()}
        elif p.rail == Rail.wire:
            body = {"amount_cents": p.amount_cents, "beneficiary_routing": acct.routing,
                    "beneficiary_account": vault.detokenize(session, acct.account_hash),
                    "beneficiary_name": v.legal_name, "reference": ref, "value_date": p.scheduled_date.isoformat()}
        else:
            body = {"amount_cents": p.amount_cents, "merchant_name": v.legal_name, "reference": ref,
                    "value_date": p.scheduled_date.isoformat()}
        try:
            resp = _post(client, API_RAILS[p.rail], p, body)
        except httpx.TransportError as exc:
            report.failed[p.id] = f"bank unreachable after retry: {exc}"  # stays scheduled; same key next time
            continue
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or data.get("status") == "rejected":
            report.failed[p.id] = f"{resp.status_code}: {json.dumps(data)[:200]}"
            continue
        _mark_sent(session, p, data.get("bank_reference"), None,
                   f"{p.rail.value} {data.get('status')} (Idempotency-Key {p.idempotency_key})")
        report.sent.append(p.id)
    return report
