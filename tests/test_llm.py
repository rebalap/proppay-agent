"""structured_call contract tests with a fake LLM, plus one live Ollama round-trip."""

import json
from decimal import Decimal

import httpx
import pytest

from app.config import get_settings
from app.llm import render_prompt, structured_call
from app.schemas import InvoiceExtraction, to_cents
from tests.fakes import FakeLLM

VALID = {
    "vendor_name": "Acme Plumbing LLC",
    "invoice_number": "INV-1043",
    "invoice_date": "2026-10-01",
    "due_date": "2026-10-31",
    "amount": 1250.50,
    "currency": "USD",
    "terms": "net 30",
    "po_number": "PO-2201",
    "line_items": [{"description": "Water heater repair", "quantity": 1, "unit_price": 1250.50, "amount": 1250.50}],
    "remit_to_bank": None,
    "email_mentions_bank_change": False,
    "pressure_language": None,
    "confidence": {k: 0.95 for k in
                   ["vendor_name", "invoice_number", "invoice_date", "due_date", "amount", "po_number", "remit_to_bank"]},
}


def call(llm):
    return structured_call(InvoiceExtraction, system="sys", user="doc", llm=llm)


def test_valid_first_try():
    llm = FakeLLM([json.dumps(VALID)])
    r = call(llm)
    assert r.ok and r.attempts == 1
    assert r.value.amount_cents == 125_050
    assert r.value.line_items_total_cents == r.value.amount_cents


def test_strips_think_block_and_code_fence():
    llm = FakeLLM(["<think>hmm</think>\n```json\n" + json.dumps(VALID) + "\n```"])
    assert call(llm).ok


def test_retries_once_with_error_feedback():
    bad = {**VALID}
    del bad["invoice_number"]
    llm = FakeLLM([json.dumps(bad), json.dumps(VALID)])
    r = call(llm)
    assert r.ok and r.attempts == 2
    feedback = llm.calls[1][-1].content
    assert "invoice_number" in feedback


def test_two_failures_route_to_human_review():
    llm = FakeLLM(["not json", '{"vendor_name": "x"}', json.dumps(VALID)])
    r = call(llm)
    assert not r.ok and r.needs_human_review
    assert r.value is None
    assert r.attempts == 2
    assert len(llm.replies) == 1  # never made a third call


def test_transport_error_not_retried():
    llm = FakeLLM([httpx.ConnectError("refused"), json.dumps(VALID)])
    r = call(llm)
    assert not r.ok and r.attempts == 1 and "ConnectError" in r.error


def test_confidence_out_of_range_rejected():
    bad = json.loads(json.dumps(VALID))
    bad["confidence"]["amount"] = 1.7
    assert not call(FakeLLM([json.dumps(bad)] * 2)).ok


@pytest.mark.parametrize("amount,cents", [("0.1", 10), (Decimal("1234.565"), 123457), (19.99, 1999), ("480000", 48_000_000)])
def test_to_cents(amount, cents):
    assert to_cents(amount) == cents


def test_prompt_render_keeps_braces():
    out = render_prompt("intake_user", source_type="pdf", document_text='{"a": 1}')
    assert '{"a": 1}' in out and "pdf" in out


# --- live round-trip -------------------------------------------------------

SAMPLE_INVOICE = """\
GREENLEAF LANDSCAPING INC.
Invoice #: INV-2087            Invoice Date: 2026-10-06
PO Number: PO-3310             Due Date: 2026-11-05
Terms: 2/10 net 30
Bill To: Maple Court Apartments, 1 Maple Ct, Austin TX

Description                         Qty   Unit Price     Amount
Monthly grounds maintenance          1      1,850.00   1,850.00
Seasonal mulch install               1        640.00     640.00
                                              TOTAL:   $2,490.00
Remit by ACH to Greenleaf Landscaping, routing 021000021, acct ****4417
"""


def _ollama_ready() -> bool:
    s = get_settings()
    try:
        tags = httpx.get(f"{s.ollama_base_url}/api/tags", timeout=2).json()
    except Exception:
        return False
    return any(m["name"] == s.ollama_model or m["name"].startswith(s.ollama_model + ":") for m in tags.get("models", []))


@pytest.mark.ollama
@pytest.mark.skipif(not _ollama_ready(), reason="Ollama not running or OLLAMA_MODEL not pulled")
def test_ollama_roundtrip_returns_valid_extraction():
    r = structured_call(
        InvoiceExtraction,
        system=render_prompt("intake_system"),
        user=render_prompt("intake_user", source_type="pdf", document_text=SAMPLE_INVOICE),
    )
    assert r.ok, r.error
    inv = r.value
    assert isinstance(inv, InvoiceExtraction)
    assert inv.invoice_number == "INV-2087"
    assert inv.amount_cents == 249_000
    assert inv.remit_to_bank and inv.remit_to_bank.routing == "021000021"


@pytest.mark.parametrize("placeholder", ["N/A", "none", " - ", "TBD", ""])
def test_placeholder_po_becomes_null(placeholder):
    doc = {**VALID, "po_number": placeholder}
    assert InvoiceExtraction.model_validate(doc).po_number is None


def test_real_po_kept():
    assert InvoiceExtraction.model_validate({**VALID, "po_number": " PO-2201 "}).po_number == " PO-2201 "
