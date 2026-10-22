"""Pydantic schemas for LLM inputs/outputs.

Every LLM-facing field is required (nullable where the value may be absent): Ollama's
constrained decoding only forces required keys, so defaulted fields get silently skipped.

LLMs return dollar amounts as decimals; conversion to integer cents happens here,
deterministically, never in the model.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Optional

from pydantic import BaseModel, Field, WithJsonSchema, field_validator

# Decimal in Python, but a plain JSON number in the schema we hand the LLM. Otherwise the
# schema also allows strings and the model writes "182,500.00", which fails validation.
Money = Annotated[Decimal, WithJsonSchema({"type": "number"})]


# Printed placeholders that mean "no value". Mapping them to null is a fixed rule, not a guess.
NULL_PLACEHOLDERS = {"", "N/A", "NA", "NONE", "NULL", "-", "—", "--", "TBD"}


def to_cents(amount: Decimal | float | str) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class LineItem(BaseModel):
    description: str
    quantity: Money
    unit_price: Money
    amount: Money


class RemitToBank(BaseModel):
    routing: str = Field(description="9-digit ABA routing number exactly as printed")
    account_last4: str = Field(description="Exactly the last 4 digits of the account number")


class FieldConfidence(BaseModel):
    """Model's self-reported confidence (0-1) per field. Low values route to human review."""

    vendor_name: float = Field(ge=0, le=1)
    invoice_number: float = Field(ge=0, le=1)
    invoice_date: float = Field(ge=0, le=1)
    due_date: float = Field(ge=0, le=1)
    amount: float = Field(ge=0, le=1)
    po_number: float = Field(ge=0, le=1)
    remit_to_bank: float = Field(ge=0, le=1)


class InvoiceExtraction(BaseModel):
    vendor_name: str
    invoice_number: str
    invoice_date: date
    due_date: Optional[date]
    amount: Money = Field(description="Total amount due in dollars, e.g. 1234.56")
    currency: str
    terms: Optional[str] = Field(description='Payment terms as printed, e.g. "2/10 Net 30"')
    po_number: Optional[str]
    line_items: list[LineItem]
    remit_to_bank: Optional[RemitToBank]
    email_mentions_bank_change: bool
    pressure_language: Optional[str] = Field(
        description="Verbatim sentence pressuring for immediate payment or threatening consequences, else null")
    confidence: FieldConfidence

    @field_validator("po_number", "terms", mode="before")
    @classmethod
    def _placeholder_to_null(cls, v):
        if isinstance(v, str) and v.strip().upper() in NULL_PLACEHOLDERS:
            return None
        return v

    @property
    def amount_cents(self) -> int:
        return to_cents(self.amount)

    @property
    def line_items_total_cents(self) -> int:
        return sum(to_cents(li.amount) for li in self.line_items)


class FraudRationale(BaseModel):
    """LLM explanation of deterministic fraud signals for a human reviewer. Carries no decision."""

    summary: str = Field(description="2-3 plain-English sentences explaining the risk to an AP reviewer")
    concerns: list[str] = Field(description="Specific red flags, each tied to the evidence given")
    recommended_actions: list[str] = Field(description="Concrete verification steps for the reviewer")
