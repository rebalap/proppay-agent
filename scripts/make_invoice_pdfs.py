"""Invoice PDF templates (reportlab). Four deliberately different layouts so the
intake agent has to cope with varied labels, date formats and field positions.

Usable standalone to re-render PDFs from data/demo_manifest.jsonl:
    uv run python scripts/make_invoice_pdfs.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parent.parent
W, H = LETTER
TEMPLATES = ("classic", "modern", "compact", "pay_app")


@dataclass
class LineItem:
    description: str
    quantity: int
    unit_cents: int
    amount_cents: int


@dataclass
class Remit:
    bank_name: str
    routing: str
    account_last4: str
    account_full: str | None = None  # only printed when a document shows the whole number


@dataclass
class InvoiceDoc:
    template: str
    vendor_name: str
    vendor_address: list[str]
    vendor_phone: str
    vendor_email: str
    bill_to: list[str]
    invoice_number: str
    invoice_date: date
    due_date: date | None
    terms: str | None
    po_number: str | None
    line_items: list[LineItem]
    remit: Remit | None = None
    notes: list[str] = field(default_factory=list)
    pay_app: dict | None = None  # contract summary for the pay_app template

    @property
    def total_cents(self) -> int:
        return sum(li.amount_cents for li in self.line_items)

    def to_json(self) -> dict:
        d = asdict(self)
        d["invoice_date"] = self.invoice_date.isoformat()
        d["due_date"] = self.due_date.isoformat() if self.due_date else None
        return d

    @classmethod
    def from_json(cls, d: dict) -> "InvoiceDoc":
        d = dict(d)
        d["invoice_date"] = date.fromisoformat(d["invoice_date"])
        d["due_date"] = date.fromisoformat(d["due_date"]) if d["due_date"] else None
        d["line_items"] = [LineItem(**li) for li in d["line_items"]]
        d["remit"] = Remit(**d["remit"]) if d["remit"] else None
        return cls(**d)


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100:,}.{abs(cents) % 100:02d}"


# --- templates -------------------------------------------------------------


def _classic(c: Canvas, d: InvoiceDoc) -> None:
    fmt = lambda x: x.strftime("%m/%d/%Y")  # noqa: E731
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, H - 60, d.vendor_name)
    c.setFont("Helvetica", 9)
    y = H - 76
    for line in [*d.vendor_address, d.vendor_phone, d.vendor_email]:
        c.drawString(50, y, line)
        y -= 12

    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(W - 50, H - 60, "INVOICE")
    c.setFont("Helvetica", 10)
    meta = [("Invoice #:", d.invoice_number), ("Date:", fmt(d.invoice_date))]
    if d.due_date:
        meta.append(("Due Date:", fmt(d.due_date)))
    if d.terms:
        meta.append(("Terms:", d.terms))
    if d.po_number:
        meta.append(("PO #:", d.po_number))
    y = H - 82
    for k, v in meta:
        c.drawRightString(W - 150, y, k)
        c.drawString(W - 145, y, v)
        y -= 14

    y = H - 175
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Bill To:")
    c.setFont("Helvetica", 10)
    for line in d.bill_to:
        y -= 13
        c.drawString(50, y, line)

    y -= 30
    c.setFillColor(colors.lightgrey)
    c.rect(45, y - 4, W - 90, 16, stroke=0, fill=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Description")
    c.drawRightString(360, y, "Qty")
    c.drawRightString(450, y, "Rate")
    c.drawRightString(W - 50, y, "Amount")
    c.setFont("Helvetica", 10)
    for li in d.line_items:
        y -= 18
        c.drawString(50, y, li.description)
        c.drawRightString(360, y, str(li.quantity))
        c.drawRightString(450, y, money(li.unit_cents))
        c.drawRightString(W - 50, y, money(li.amount_cents))
    y -= 14
    c.line(300, y, W - 50, y)
    y -= 16
    c.drawRightString(450, y, "Subtotal")
    c.drawRightString(W - 50, y, money(d.total_cents))
    y -= 16
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(450, y, "Total Due")
    c.drawRightString(W - 50, y, "$" + money(d.total_cents))

    y -= 40
    c.setFont("Helvetica", 9)
    for note in d.notes:
        c.drawString(50, y, note)
        y -= 12
    if d.remit:
        r = d.remit
        acct = r.account_full or f"****{r.account_last4}"
        c.drawString(50, 80, f"Remit via ACH to: {r.bank_name}  |  ABA {r.routing}  |  Acct {acct}")
    c.drawString(50, 66, "Thank you for your business.")


def _modern(c: Canvas, d: InvoiceDoc) -> None:
    fmt = lambda x: x.strftime("%b %d, %Y")  # noqa: E731
    c.setFillColor(colors.HexColor("#1f4e79"))
    c.rect(0, H - 90, W, 90, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, H - 50, d.vendor_name.upper())
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 68, "  ·  ".join([*d.vendor_address, d.vendor_phone]))
    c.drawString(40, H - 80, d.vendor_email)
    c.setFillColor(colors.black)

    cells = [
        ("INVOICE NO.", d.invoice_number),
        ("ISSUED", fmt(d.invoice_date)),
        ("DUE DATE", fmt(d.due_date) if d.due_date else "—"),
        ("PO NUMBER", d.po_number or "N/A"),
        ("TERMS", d.terms or "—"),
        ("AMOUNT DUE", "USD " + money(d.total_cents)),
    ]
    for i, (k, v) in enumerate(cells):
        x = 40 + (i % 3) * 180
        y = H - 125 - (i // 3) * 40
        c.setFont("Helvetica", 7)
        c.setFillColor(colors.grey)
        c.drawString(x, y, k)
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y - 14, v)

    y = H - 225
    c.setFont("Helvetica", 7)
    c.setFillColor(colors.grey)
    c.drawString(40, y, "BILLED TO")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    c.drawString(40, y - 14, ", ".join(d.bill_to))

    y -= 50
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "SERVICE")
    c.drawRightString(380, y, "UNITS")
    c.drawRightString(470, y, "UNIT PRICE")
    c.drawRightString(W - 40, y, "LINE TOTAL")
    c.setStrokeColor(colors.HexColor("#1f4e79"))
    c.line(40, y - 5, W - 40, y - 5)
    c.setFont("Helvetica", 10)
    for li in d.line_items:
        y -= 20
        c.drawString(40, y, li.description)
        c.drawRightString(380, y, str(li.quantity))
        c.drawRightString(470, y, money(li.unit_cents))
        c.drawRightString(W - 40, y, money(li.amount_cents))
    y -= 28
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(470, y, "TOTAL")
    c.drawRightString(W - 40, y, "$" + money(d.total_cents))

    y -= 40
    c.setFont("Helvetica", 9)
    for note in d.notes:
        c.drawString(40, y, note)
        y -= 12
    if d.remit:
        r = d.remit
        c.setFont("Helvetica-Bold", 9)
        c.drawString(40, 110, "PAYMENT INFORMATION")
        c.setFont("Helvetica", 9)
        c.drawString(40, 96, f"Bank: {r.bank_name}")
        c.drawString(40, 84, f"Routing No.: {r.routing}")
        acct = r.account_full or f"ending in {r.account_last4}"
        c.drawString(40, 72, f"Account No.: {acct}")


def _compact(c: Canvas, d: InvoiceDoc) -> None:
    fmt = lambda x: x.isoformat()  # noqa: E731
    c.setFont("Courier-Bold", 12)
    y = H - 50
    c.drawString(40, y, d.vendor_name)
    c.setFont("Courier", 9)
    for line in [*d.vendor_address, f"Tel {d.vendor_phone}  {d.vendor_email}"]:
        y -= 11
        c.drawString(40, y, line)
    y -= 24
    c.setFont("Courier", 10)
    rows = [f"Inv No.  : {d.invoice_number}", f"Inv Date : {fmt(d.invoice_date)}"]
    if d.due_date:
        rows.append(f"Pay By   : {fmt(d.due_date)}")
    if d.terms:
        rows.append(f"Terms    : {d.terms}")
    if d.po_number:
        rows.append(f"Ref PO   : {d.po_number}")
    rows.append(f"Customer : {' / '.join(d.bill_to)}")
    for row in rows:
        c.drawString(40, y, row)
        y -= 13
    y -= 10
    c.drawString(40, y, "-" * 84)
    y -= 13
    c.drawString(40, y, f"{'ITEM':<48}{'QTY':>6}{'EACH':>14}{'EXT':>14}")
    y -= 6
    for li in d.line_items:
        y -= 13
        c.drawString(40, y, f"{li.description[:47]:<48}{li.quantity:>6}{money(li.unit_cents):>14}{money(li.amount_cents):>14}")
    y -= 13
    c.drawString(40, y, "-" * 84)
    y -= 15
    c.setFont("Courier-Bold", 11)
    c.drawString(40, y, f"{'BALANCE DUE USD':<62}{money(d.total_cents):>14}")
    c.setFont("Courier", 9)
    y -= 30
    for note in d.notes:
        c.drawString(40, y, note)
        y -= 11
    if d.remit:
        r = d.remit
        acct = r.account_full or f"xxxx{r.account_last4}"
        c.drawString(40, 70, f"ACH: {r.bank_name} RTN {r.routing} ACCT {acct}")


def _pay_app(c: Canvas, d: InvoiceDoc) -> None:
    """Contractor's application for payment (AIA G702-style summary)."""
    fmt = lambda x: x.strftime("%B %-d, %Y")  # noqa: E731
    pa = d.pay_app or {}
    c.setFont("Times-Bold", 14)
    c.drawCentredString(W / 2, H - 50, f"APPLICATION AND CERTIFICATE FOR PAYMENT No. {pa.get('application_no', 1)}")
    c.setFont("Times-Roman", 10)
    c.drawString(50, H - 80, f"Contractor: {d.vendor_name}, {', '.join(d.vendor_address)}")
    c.drawString(50, H - 94, f"Owner: {', '.join(d.bill_to)}")
    c.drawString(50, H - 108, f"Project: {pa.get('project', '')}")
    c.drawString(50, H - 122, f"Application No.: {d.invoice_number}     Period To: {fmt(d.invoice_date)}")
    if d.po_number:
        c.drawString(50, H - 136, f"Contract / PO: {d.po_number}")
    if d.due_date:
        c.drawString(330, H - 136, f"Payment due: {fmt(d.due_date)}")

    y = H - 170
    c.setFont("Times-Bold", 10)
    c.drawString(50, y, "CONTRACT SUMMARY")
    c.setFont("Times-Roman", 10)
    summary = [
        ("1. Original contract sum", pa.get("original_contract_cents", 0)),
        ("2. Net change by change orders", pa.get("change_orders_cents", 0)),
        ("3. Contract sum to date", pa.get("original_contract_cents", 0) + pa.get("change_orders_cents", 0)),
        ("4. Total completed & stored to date", pa.get("completed_to_date_cents", 0)),
        ("5. Retainage (10%)", pa.get("retainage_to_date_cents", 0)),
        ("6. Less previous certificates for payment", pa.get("previous_certificates_cents", 0)),
    ]
    for k, v in summary:
        y -= 15
        c.drawString(60, y, k)
        c.drawRightString(W - 60, y, "$" + money(v))

    y -= 35
    c.setFont("Times-Bold", 10)
    c.drawString(50, y, "SCHEDULE OF VALUES - THIS PERIOD")
    c.drawRightString(W - 60, y, "Amount")
    c.setFont("Times-Roman", 10)
    for li in d.line_items:
        y -= 15
        c.drawString(60, y, li.description)
        c.drawRightString(W - 60, y, money(li.amount_cents))
    y -= 25
    c.setFont("Times-Bold", 12)
    c.drawString(60, y, "CURRENT PAYMENT DUE")
    c.drawRightString(W - 60, y, "$" + money(d.total_cents))
    if d.terms:
        y -= 18
        c.setFont("Times-Roman", 10)
        c.drawString(60, y, f"Terms: {d.terms}")
    y -= 30
    for note in d.notes:
        c.drawString(50, y, note)
        y -= 13
    if d.remit:
        r = d.remit
        c.drawString(50, 90, f"Wire / ACH instructions: {r.bank_name}, ABA {r.routing}, account ending {r.account_last4}")
    c.drawString(50, 70, f"Contractor contact: {d.vendor_phone}  {d.vendor_email}")


_RENDERERS = {"classic": _classic, "modern": _modern, "compact": _compact, "pay_app": _pay_app}


def render(doc: InvoiceDoc, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = Canvas(str(path), pagesize=LETTER, invariant=1)  # invariant: byte-stable output
    c.setTitle(f"Invoice {doc.invoice_number}")
    c.setAuthor(doc.vendor_name)
    _RENDERERS[doc.template](c, doc)
    c.showPage()
    c.save()
    return path


def main() -> None:
    manifest = ROOT / "data" / "demo_manifest.jsonl"
    if not manifest.exists():
        sys.exit("No manifest yet; run scripts/seed_data.py first.")
    n = 0
    for line in manifest.read_text().splitlines():
        rec = json.loads(line)
        render(InvoiceDoc.from_json(rec["doc"]), ROOT / "data" / "invoices" / rec["file"])
        n += 1
    print(f"Rendered {n} PDFs into data/invoices/")


if __name__ == "__main__":
    main()
