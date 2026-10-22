"""Phase 1 acceptance: DB populated; 25 PDFs covering all 9 demo cases; data is internally consistent."""

import csv
import email
import json
import re
from collections import Counter
from datetime import date

import pdfplumber
import pytest
from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.config import fraud_config, policy_config
from app.db import make_engine
from app.models import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Property,
    PurchaseOrder,
    Vendor,
    VendorBankAccount,
    WorkOrder,
    WorkOrderStatus,
)
from app.payments.routing import is_valid_aba
from app.schemas import to_cents
from scripts.seed_data import seed

RUN_DATE = date(2026, 10, 19)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    engine = make_engine(f"sqlite:///{out / 'test.db'}")
    summary = seed(engine, out, RUN_DATE)
    manifest = [json.loads(line) for line in (out / "demo_manifest.jsonl").read_text().splitlines()]
    with Session(engine) as s:
        yield {"out": out, "summary": summary, "manifest": manifest, "session": s}


def by_case(manifest, n):
    return [m for m in manifest if m["demo_case"] == n]


def norm_name(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", s.upper().replace("&", " AND "))
    return " ".join(t for t in s.split() if t not in {"LLC", "INC", "CO", "LTD", "FZE", "FZCO", "SA", "GMBH"})


# --- counts / coverage ---------------------------------------------------------


def test_counts(seeded):
    s = seeded["session"]
    assert len(s.exec(select(Property)).all()) == 12
    assert len(s.exec(select(Vendor)).all()) == 30
    monday = s.exec(select(Invoice).where(Invoice.status == InvoiceStatus.received)).all()
    assert len(monday) == 25
    assert len(s.exec(select(Payment).where(Payment.status == PaymentStatus.settled)).all()) > 100


def test_all_nine_cases_covered(seeded):
    counts = Counter(m["demo_case"] for m in seeded["manifest"])
    assert set(counts) == set(range(1, 10))
    assert counts[1] + counts[7] + counts[8] == 18  # 18 routine invoices (7 and 8 are routine variants)
    assert counts[6] == 2
    assert all(counts[c] == 1 for c in (2, 3, 4, 5, 7, 8, 9))


def test_pdfs_and_emails_exist(seeded):
    out = seeded["out"]
    assert len(list((out / "invoices").glob("*.pdf"))) == 25
    for m in seeded["manifest"]:
        assert (out / "invoices" / m["file"]).exists()
        if m["channel"] == "email":
            msg = email.message_from_bytes((out / m["email_file"]).read_bytes())
            attachments = [p.get_filename() for p in msg.walk() if p.get_filename()]
            assert attachments == [m["file"]]


def test_pdf_text_contains_key_fields(seeded):
    """Every truth value we will grade extraction on is actually present in the PDF text."""
    out = seeded["out"]
    for m in seeded["manifest"]:
        t = m["truth"]
        with pdfplumber.open(out / "invoices" / m["file"]) as pdf:
            text = pdf.pages[0].extract_text()
        total = f"{to_cents(t['amount']) // 100:,}.{to_cents(t['amount']) % 100:02d}"
        assert t["invoice_number"] in text, m["file"]
        assert total in text, m["file"]
        assert t["vendor_name"].upper() in text.upper(), m["file"]
        if t["po_number"]:
            assert t["po_number"] in text, m["file"]
        if t["remit_to_bank"]:
            assert t["remit_to_bank"]["routing"] in text, m["file"]
            assert t["remit_to_bank"]["account_last4"] in text, m["file"]


# --- consistency -----------------------------------------------------------------


def test_line_items_sum_to_total(seeded):
    for m in seeded["manifest"]:
        t = m["truth"]
        assert sum(to_cents(li["amount"]) for li in t["line_items"]) == to_cents(t["amount"]), m["file"]


def test_routing_numbers_valid(seeded):
    s = seeded["session"]
    assert all(is_valid_aba(a.routing) for a in s.exec(select(VendorBankAccount)).all())
    for m in seeded["manifest"]:
        if m["truth"]["remit_to_bank"]:
            assert is_valid_aba(m["truth"]["remit_to_bank"]["routing"])


def test_trust_follows_policy(seeded):
    s = seeded["session"]
    need = policy_config()["vendor_trust"]["min_settled_payments"]
    for v in s.exec(select(Vendor)).all():
        settled = s.exec(select(Payment).join(Invoice).where(
            Invoice.vendor_id == v.id, Payment.status == PaymentStatus.settled)).all()
        assert v.trusted == (len(settled) >= need), v.legal_name


def test_routine_invoices_meet_auto_pay_preconditions(seeded):
    s = seeded["session"]
    max_auto = policy_config()["auto_pay"]["max_amount_cents"]
    tol = policy_config()["match"]["po_amount_tolerance_pct"]
    routine = [m for m in seeded["manifest"] if m["demo_case"] in (1, 4, 6, 7, 8)]
    for m in routine:
        t = m["truth"]
        amount = to_cents(t["amount"])
        assert amount < max_auto, m["file"]
        v = s.exec(select(Vendor).where(Vendor.legal_name == t["vendor_name"])).one()
        assert v.trusted, v.legal_name
        po = s.exec(select(PurchaseOrder).where(PurchaseOrder.po_number == t["po_number"])).one()
        assert po.vendor_id == v.id
        assert abs(po.amount_cents - amount) * 100 <= po.amount_cents * tol, m["file"]
        wo = s.exec(select(WorkOrder).where(WorkOrder.po_id == po.id)).one()
        assert wo.status == WorkOrderStatus.completed
        if t["remit_to_bank"]:  # routine invoices quote the account already on file
            acct = s.exec(select(VendorBankAccount).where(VendorBankAccount.vendor_id == v.id,
                                                          VendorBankAccount.active)).one()
            assert (acct.routing, acct.account_last4) == (t["remit_to_bank"]["routing"], t["remit_to_bank"]["account_last4"])


def test_money_is_integer_cents(seeded):
    s = seeded["session"]
    for p in s.exec(select(Payment)).all():
        assert isinstance(p.amount_cents, int) and isinstance(p.fee_cents, int)


# --- specific demo cases ---------------------------------------------------------


def test_case2_bec(seeded):
    s = seeded["session"]
    (m,) = by_case(seeded["manifest"], 2)
    assert m["is_fraud"] and m["channel"] == "email" and m["truth"]["email_mentions_bank_change"]
    v = s.exec(select(Vendor).where(Vendor.legal_name == m["truth"]["vendor_name"])).one()
    acct = s.exec(select(VendorBankAccount).where(VendorBankAccount.vendor_id == v.id)).one()
    assert m["truth"]["remit_to_bank"]["routing"] != acct.routing
    msg = email.message_from_bytes((seeded["out"] / m["email_file"]).read_bytes())
    sender_domain = msg["From"].split("@")[1].rstrip(">")
    assert sender_domain != v.email_domain
    assert fuzz.ratio(sender_domain, v.email_domain) >= fraud_config()["signals"]["LOOKALIKE_VENDOR"]["domain_similarity_min"]


def test_case3_duplicate_of_paid_invoice(seeded):
    s = seeded["session"]
    (m,) = by_case(seeded["manifest"], 3)
    assert m["truth"]["invoice_number"] == "INV-1043-A"
    orig = s.exec(select(Invoice).where(Invoice.invoice_number == "INV-1043")).one()
    assert orig.status == InvoiceStatus.paid
    assert orig.amount_cents == to_cents(m["truth"]["amount"])
    assert orig.po_number == m["truth"]["po_number"]


def test_case4_emergency_due_today(seeded):
    (m,) = by_case(seeded["manifest"], 4)
    assert m["truth"]["amount"] == "3800.00"
    assert m["truth"]["due_date"] == RUN_DATE.isoformat()
    assert m["expected"]["rail"] == "rtp"


def test_case5_gc_draw(seeded):
    (m,) = by_case(seeded["manifest"], 5)
    assert m["truth"]["amount"] == "480000.00"
    assert m["expected"] == {"decision": "DUAL_APPROVAL", "rail": "wire", "signals": []}


def test_case6_card_vendors(seeded):
    s = seeded["session"]
    for m in by_case(seeded["manifest"], 6):
        v = s.exec(select(Vendor).where(Vendor.legal_name == m["truth"]["vendor_name"])).one()
        assert v.card_accepting and "virtual_card" in v.enrolled_rails
        assert m["expected"]["rebate_cents"] == to_cents(m["truth"]["amount"]) // 100


def test_case7_r03_scenario_points_at_vendor_account(seeded):
    s = seeded["session"]
    (m,) = by_case(seeded["manifest"], 7)
    scen = json.loads((seeded["out"] / "mock_bank_scenarios.json").read_text())["ach_returns"][0]
    assert scen["return_code"] == "R03"
    v = s.exec(select(Vendor).where(Vendor.legal_name == m["truth"]["vendor_name"])).one()
    acct = s.exec(select(VendorBankAccount).where(VendorBankAccount.vendor_id == v.id)).one()
    assert scen["account_hash"] == acct.account_hash


def test_case8_discount_window_open(seeded):
    (m,) = by_case(seeded["manifest"], 8)
    assert m["truth"]["terms"] == "2/10 Net 30"
    assert date.fromisoformat(m["expected"]["discount_deadline"]) > RUN_DATE


def test_case9_only_meridian_near_sdn(seeded):
    sdn = [r["sdn_name"] for r in csv.DictReader((seeded["out"] / "ofac" / "sdn.csv").open())]
    threshold = fraud_config()["signals"]["OFAC_HIT"]["name_similarity_min"]
    hits = {v.legal_name for v in seeded["session"].exec(select(Vendor)).all()
            if max(fuzz.token_sort_ratio(norm_name(v.legal_name), norm_name(x)) for x in sdn) >= threshold}
    (m,) = by_case(seeded["manifest"], 9)
    assert hits == {m["truth"]["vendor_name"]}


def test_fraud_ground_truth_only_on_case2(seeded):
    s = seeded["session"]
    assert [i.demo_case for i in s.exec(select(Invoice).where(Invoice.is_fraud)).all()] == [2]


def test_deterministic(seeded, tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'again.db'}")
    seed(engine, tmp_path, RUN_DATE)
    assert (tmp_path / "demo_manifest.jsonl").read_text() == (seeded["out"] / "demo_manifest.jsonl").read_text()
    for m in seeded["manifest"]:
        assert (tmp_path / "invoices" / m["file"]).read_bytes() == (seeded["out"] / "invoices" / m["file"]).read_bytes()
