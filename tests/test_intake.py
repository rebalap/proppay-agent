import json
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import select

from app.agents.intake import load_source, process_invoice, run_intake, validate
from app.models import AuditLog, Invoice, InvoiceStatus
from app.schemas import InvoiceExtraction
from tests.fakes import FakeLLM, extraction_json


def ext_from(truth: dict, **overrides) -> InvoiceExtraction:
    return InvoiceExtraction.model_validate_json(extraction_json({**truth, **overrides}))


def codes(issues, blocking_only=True):
    return {i.code for i in issues if i.blocking or not blocking_only}


def case(manifest, n):
    return next(m for m in manifest if m["demo_case"] == n)


# --- loading -----------------------------------------------------------------


def test_load_pdf(fresh_seed):
    m = case(fresh_seed["manifest"], 4)
    doc = load_source(fresh_seed["data_dir"] / "invoices" / m["file"])
    assert doc.channel == "pdf" and m["truth"]["invoice_number"] in doc.text


def test_load_email_includes_headers_body_and_attachment(fresh_seed):
    m = case(fresh_seed["manifest"], 2)
    doc = load_source(fresh_seed["data_dir"] / m["email_file"])
    assert doc.channel == "email"
    assert doc.sender_domain == "acmeplumbimg.com"
    assert "changed banks" in doc.text
    assert m["truth"]["invoice_number"] in doc.text  # from the attached PDF
    assert doc.attachments == [m["file"]]


# --- deterministic validation ----------------------------------------------------


def test_gold_truth_passes_validation(fresh_seed):
    """The ground truth for all 25 invoices is clean under our own validators."""
    for m in fresh_seed["manifest"]:
        assert codes(validate(ext_from(m["truth"]))) == set(), m["file"]


def test_invalid_routing_blocks(fresh_seed):
    t = case(fresh_seed["manifest"], 4)["truth"]
    bad = {**t["remit_to_bank"], "routing": t["remit_to_bank"]["routing"][:-1] + "0"}
    if bad["routing"] == t["remit_to_bank"]["routing"]:
        bad["routing"] = bad["routing"][:-1] + "1"
    assert "INVALID_ROUTING" in codes(validate(ext_from(t, remit_to_bank=bad)))


def test_bad_last4_blocks(fresh_seed):
    t = case(fresh_seed["manifest"], 4)["truth"]
    bad = {**t["remit_to_bank"], "account_last4": "451"}
    assert "INVALID_ACCOUNT" in codes(validate(ext_from(t, remit_to_bank=bad)))


def test_line_items_mismatch_blocks(fresh_seed):
    t = case(fresh_seed["manifest"], 5)["truth"]
    without_retainage = [li for li in t["line_items"] if not li["amount"].startswith("-")]
    assert "LINE_ITEMS_MISMATCH" in codes(validate(ext_from(t, line_items=without_retainage)))


def test_missing_line_items_is_warning_only(fresh_seed):
    t = case(fresh_seed["manifest"], 4)["truth"]
    issues = validate(ext_from(t, line_items=[]))
    assert codes(issues) == set() and "NO_LINE_ITEMS" in codes(issues, blocking_only=False)


def test_due_before_invoice_date_blocks(fresh_seed):
    t = case(fresh_seed["manifest"], 4)["truth"]
    assert "DATE_ORDER" in codes(validate(ext_from(t, due_date="2026-01-01")))


def test_low_confidence_only_counts_for_present_values(fresh_seed):
    t = case(fresh_seed["manifest"], 9)["truth"]  # no PO on this invoice
    assert t["po_number"] is None
    ext = ext_from(t)
    ext.confidence.po_number = 0.1  # absent value, low confidence: fine
    assert codes(validate(ext)) == set()
    ext.confidence.amount = 0.3  # present value, low confidence: review
    assert "LOW_CONFIDENCE" in codes(validate(ext))


# --- run + persist ------------------------------------------------------------------


def _invoice_for(session, m):
    return session.get(Invoice, m["invoice_id"])


def test_process_invoice_persists_and_audits(fresh_seed):
    s, m = fresh_seed["session"], case(fresh_seed["manifest"], 2)
    result = process_invoice(s, m["invoice_id"], fresh_seed["data_dir"], llm=FakeLLM([extraction_json(m["truth"])]))
    s.commit()
    inv = _invoice_for(s, m)
    assert not result.needs_review
    assert inv.status == InvoiceStatus.extracted
    assert inv.invoice_number == m["truth"]["invoice_number"]
    assert inv.invoice_date == date.fromisoformat(m["truth"]["invoice_date"])
    assert inv.remit_routing == m["truth"]["remit_to_bank"]["routing"]
    assert inv.channel == "email" and inv.sender_email == "accounts@acmeplumbimg.com"
    assert isinstance(inv.amount_cents, int)
    row = s.exec(select(AuditLog).where(AuditLog.actor == "intake_agent")).one()
    assert row.entity_id == inv.id and json.loads(row.after_json)["status"] == "extracted"


def test_unparseable_llm_output_routes_to_review(fresh_seed):
    s, m = fresh_seed["session"], case(fresh_seed["manifest"], 1)
    result = process_invoice(s, m["invoice_id"], fresh_seed["data_dir"], llm=FakeLLM(["nope", "{}"]))
    s.commit()
    assert result.needs_review and result.extraction is None
    assert [i.code for i in result.issues] == ["LLM_PARSE_FAILED"]
    inv = _invoice_for(s, m)
    assert inv.status == InvoiceStatus.needs_review and inv.amount_cents is None


def test_unsupported_file_type(tmp_path):
    p = tmp_path / "x.docx"
    p.write_text("x")
    with pytest.raises(ValueError):
        run_intake(p, llm=FakeLLM([]))
