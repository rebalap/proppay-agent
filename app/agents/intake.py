"""Intake agent (SPEC §6.1): invoice PDF or vendor email -> InvoiceExtraction.

The LLM only extracts. Everything after that is deterministic: ABA checksum,
line items vs total, required fields, confidence floor. Any blocking issue routes
the invoice to human review; we never patch the model's output.
"""

from __future__ import annotations

import email
import io
import json
from email import policy
from pathlib import Path

import pdfplumber
from pydantic import BaseModel
from rapidfuzz import fuzz
from sqlmodel import Session

from app import audit
from app.config import fraud_config, policy_config
from app.llm import ChatModel, render_prompt, structured_call
from app.models import Invoice, InvoiceStatus
from app.payments.routing import is_valid_aba
from app.schemas import InvoiceExtraction

ACTOR = "intake_agent"
MAX_DOC_CHARS = 12_000  # keeps the prompt inside the ~8K-token context


class SourceDoc(BaseModel):
    path: str
    channel: str  # pdf | email
    text: str
    sender_email: str | None = None
    sender_domain: str | None = None
    subject: str | None = None
    attachments: list[str] = []


class Issue(BaseModel):
    code: str
    field: str | None = None
    detail: str
    blocking: bool = True


class IntakeResult(BaseModel):
    source: SourceDoc
    extraction: InvoiceExtraction | None
    issues: list[Issue]
    attempts: int

    @property
    def needs_review(self) -> bool:
        return self.extraction is None or any(i.blocking for i in self.issues)


# --- loading -----------------------------------------------------------------


def pdf_text(data: bytes | Path) -> str:
    src = io.BytesIO(data) if isinstance(data, bytes) else data
    with pdfplumber.open(src) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def load_source(path: Path) -> SourceDoc:
    if path.suffix.lower() == ".pdf":
        return SourceDoc(path=str(path), channel="pdf", text=pdf_text(path))
    if path.suffix.lower() == ".eml":
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        sender = email.utils.parseaddr(msg["From"] or "")[1].lower() or None
        body_part = msg.get_body(preferencelist=("plain",))
        parts = [
            f"From: {msg['From']}",
            f"Subject: {msg['Subject']}",
            "",
            body_part.get_content() if body_part else "",
        ]
        names = []
        for att in msg.iter_attachments():
            name = att.get_filename() or "attachment"
            names.append(name)
            if att.get_content_type() == "application/pdf":
                parts += ["", f'<attachment name="{name}">', pdf_text(att.get_content()), "</attachment>"]
        return SourceDoc(path=str(path), channel="email", text="\n".join(parts), sender_email=sender,
                         sender_domain=sender.split("@")[1] if sender and "@" in sender else None,
                         subject=msg["Subject"], attachments=names)
    raise ValueError(f"unsupported source type: {path.suffix}")


# --- extraction + validation ---------------------------------------------------


def extract(doc: SourceDoc, llm: ChatModel | None = None):
    return structured_call(
        InvoiceExtraction,
        system=render_prompt("intake_system"),
        user=render_prompt("intake_user", source_type="vendor email" if doc.channel == "email" else "invoice PDF",
                           document_text=doc.text[:MAX_DOC_CHARS]),
        llm=llm,
    )


def validate(ext: InvoiceExtraction) -> list[Issue]:
    cfg = policy_config()["intake"]
    issues: list[Issue] = []

    for f in cfg["required_fields"]:
        value = getattr(ext, f)
        if value is None or (isinstance(value, str) and not value.strip()):
            issues.append(Issue(code="MISSING_FIELD", field=f, detail=f"{f} is missing"))

    if ext.amount_cents <= 0:
        issues.append(Issue(code="INVALID_AMOUNT", field="amount", detail=f"amount {ext.amount} is not positive"))
    if ext.currency.upper() != "USD":
        issues.append(Issue(code="UNSUPPORTED_CURRENCY", field="currency", detail=f"currency {ext.currency}"))
    if ext.due_date and ext.due_date < ext.invoice_date:
        issues.append(Issue(code="DATE_ORDER", field="due_date", detail="due date is before invoice date"))

    if ext.remit_to_bank:
        if not is_valid_aba(ext.remit_to_bank.routing):
            issues.append(Issue(code="INVALID_ROUTING", field="remit_to_bank",
                                detail=f"routing {ext.remit_to_bank.routing!r} fails the ABA checksum"))
        last4 = ext.remit_to_bank.account_last4
        if len(last4) != 4 or not last4.isdigit():
            issues.append(Issue(code="INVALID_ACCOUNT", field="remit_to_bank", detail=f"account_last4 {last4!r}"))

    if ext.line_items:
        if ext.line_items_total_cents != ext.amount_cents:
            issues.append(Issue(code="LINE_ITEMS_MISMATCH", field="line_items",
                                detail=f"line items sum to {ext.line_items_total_cents} cents, "
                                       f"total is {ext.amount_cents} cents"))
    else:
        issues.append(Issue(code="NO_LINE_ITEMS", field="line_items", detail="no line items found", blocking=False))

    # Low confidence only matters for values the model actually returned; absent values are handled above.
    floor = cfg["min_field_confidence"]
    for f, conf in ext.confidence.model_dump().items():
        if conf < floor and getattr(ext, f) is not None:
            issues.append(Issue(code="LOW_CONFIDENCE", field=f, detail=f"confidence {conf:.2f} < {floor}"))
    return issues


def _squash(s: str) -> str:
    return " ".join(s.lower().split())


def verify_pressure_quote(ext: InvoiceExtraction, doc_text: str) -> list[Issue]:
    """The LLM's pressure-language claim only stands if the quote is really in the document."""
    if not ext.pressure_language:
        return []
    quote, text = _squash(ext.pressure_language), _squash(doc_text)
    if quote in text or fuzz.partial_ratio(quote, text) >= fraud_config()["signals"]["URGENCY_LANGUAGE"]["quote_match_min"]:
        return []
    dropped = ext.pressure_language
    ext.pressure_language = None
    return [Issue(code="PRESSURE_QUOTE_UNVERIFIED", field="pressure_language", blocking=False,
                  detail=f"quoted text not found in document, ignored: {dropped[:80]!r}")]


def run_intake(path: Path, llm: ChatModel | None = None) -> IntakeResult:
    """Pure: load, extract, validate. No DB writes (used by evals too)."""
    doc = load_source(path)
    r = extract(doc, llm=llm)
    if not r.ok:
        issue = Issue(code="LLM_PARSE_FAILED", detail=r.error or "unknown error")
        return IntakeResult(source=doc, extraction=None, issues=[issue], attempts=r.attempts)
    issues = verify_pressure_quote(r.value, doc.text) + validate(r.value)
    return IntakeResult(source=doc, extraction=r.value, issues=issues, attempts=r.attempts)


# --- persistence ----------------------------------------------------------------


def persist(session: Session, invoice: Invoice, result: IntakeResult) -> None:
    before = audit.snapshot(invoice)
    ext = result.extraction
    invoice.channel = result.source.channel
    invoice.sender_email = result.source.sender_email
    invoice.intake_issues_json = json.dumps([i.model_dump() for i in result.issues])
    if ext is not None:
        invoice.invoice_number = ext.invoice_number
        invoice.invoice_date = ext.invoice_date
        invoice.due_date = ext.due_date
        invoice.amount_cents = ext.amount_cents
        invoice.terms = ext.terms
        invoice.po_number = ext.po_number
        invoice.remit_routing = ext.remit_to_bank.routing if ext.remit_to_bank else None
        invoice.remit_account_last4 = ext.remit_to_bank.account_last4 if ext.remit_to_bank else None
        invoice.extracted_json = ext.model_dump_json()
    invoice.status = InvoiceStatus.needs_review if result.needs_review else InvoiceStatus.extracted
    blocking = [i.code for i in result.issues if i.blocking]
    audit.record(session, actor=ACTOR, entity="invoices", entity_id=invoice.id, action="extract",
                 before=before, after=invoice,
                 reason=f"needs review: {', '.join(blocking)}" if blocking else "extracted, all checks passed")
    session.add(invoice)


def process_invoice(session: Session, invoice_id: int, data_dir: Path, llm: ChatModel | None = None) -> IntakeResult:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise KeyError(f"invoice {invoice_id} not found")
    result = run_intake(data_dir / invoice.source_file, llm=llm)
    persist(session, invoice, result)
    return result
