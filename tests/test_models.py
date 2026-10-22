import json
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app import audit
from app.payments import vault
from app.models import (
    AuditLog,
    Invoice,
    Payment,
    PaymentStatus,
    Property,
    PurchaseOrder,
    Rail,
    Vendor,
    VendorBankAccount,
)

EXPECTED_TABLES = {
    "properties", "vendors", "vendor_bank_accounts", "bank_change_requests",
    "purchase_orders", "work_orders", "invoices", "payments", "payment_batches",
    "approvals", "bank_statement_lines", "audit_log",
}


def test_all_spec_tables_created(engine):
    from sqlalchemy import inspect

    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())


def _vendor(session) -> Vendor:
    v = Vendor(legal_name="Acme Plumbing LLC", category="plumbing", email_domain="acmeplumbing.com")
    session.add(v)
    session.commit()
    return v


def test_vendor_invoice_payment_roundtrip(session):
    v = _vendor(session)
    prop = Property(name="Maple Court", address="1 Maple Ct", units=40)
    session.add(prop)
    session.commit()
    session.add(PurchaseOrder(po_number="PO-1001", vendor_id=v.id, property_id=prop.id, amount_cents=120_000))
    token = vault.tokenize(session, "000123456789")
    session.add(VendorBankAccount(vendor_id=v.id, routing="021000021", account_last4="6789",
                                  account_hash=token, effective_from=date(2026, 1, 1)))
    inv = Invoice(vendor_id=v.id, invoice_number="INV-1", amount_cents=120_000, source_file="x.pdf")
    session.add(inv)
    session.commit()

    pay = Payment(invoice_id=inv.id, rail=Rail.ach_standard, amount_cents=120_000, fee_cents=25,
                  scheduled_date=date(2026, 10, 19), idempotency_key=f"{inv.id}-1")
    session.add(pay)
    session.commit()

    assert vault.detokenize(session, token) == "000123456789"
    got = session.exec(select(Payment)).one()
    assert got.amount_cents == 120_000 and isinstance(got.amount_cents, int)
    assert got.rail is Rail.ach_standard
    assert got.status is PaymentStatus.scheduled


def test_idempotency_key_unique(session):
    v = _vendor(session)
    inv = Invoice(vendor_id=v.id, source_file="x.pdf")
    session.add(inv)
    session.commit()
    for _ in range(2):
        session.add(Payment(invoice_id=inv.id, rail=Rail.rtp, amount_cents=1, scheduled_date=date.today(),
                            idempotency_key="1-1"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_foreign_keys_enforced(session):
    session.add(Invoice(vendor_id=999, source_file="x.pdf"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_audit_record(session):
    v = _vendor(session)
    before = audit.snapshot(v)
    v.trusted = True
    audit.record(session, actor="onboarding_agent", entity="vendors", entity_id=v.id,
                 action="set_trusted", before=before, after=v, reason="3 clean settled payments")
    session.commit()

    row = session.exec(select(AuditLog)).one()
    assert row.actor == "onboarding_agent"
    assert json.loads(row.before_json)["trusted"] is False
    assert json.loads(row.after_json)["trusted"] is True
