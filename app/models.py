"""SQLModel tables (spec §5). Money is always integer cents."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Enums -----------------------------------------------------------------


class TinMatchStatus(str, Enum):
    pending = "pending"
    matched = "matched"
    mismatched = "mismatched"


class OfacStatus(str, Enum):
    pending = "pending"
    clear = "clear"
    hit = "hit"


class AccountType(str, Enum):
    checking = "checking"
    savings = "savings"


class BankAccountSource(str, Enum):
    onboarding = "onboarding"
    change_request = "change_request"


class ChangeRequestChannel(str, Enum):
    email = "email"
    portal = "portal"
    phone = "phone"


class ChangeRequestStatus(str, Enum):
    pending_callback = "pending_callback"
    verified = "verified"
    rejected = "rejected"


class POStatus(str, Enum):
    open = "open"
    closed = "closed"


class WorkOrderStatus(str, Enum):
    open = "open"
    completed = "completed"


class InvoiceStatus(str, Enum):
    received = "received"
    extracted = "extracted"
    needs_review = "needs_review"
    matched = "matched"
    pending_approval = "pending_approval"
    on_hold = "on_hold"
    blocked = "blocked"
    approved = "approved"
    scheduled = "scheduled"
    paid = "paid"
    reopened = "reopened"


class Rail(str, Enum):
    ach_standard = "ach_standard"
    ach_same_day = "ach_same_day"
    rtp = "rtp"
    fednow = "fednow"
    wire = "wire"
    virtual_card = "virtual_card"
    check = "check"


class PaymentStatus(str, Enum):
    scheduled = "scheduled"
    pending_approval = "pending_approval"
    sent = "sent"
    settled = "settled"
    returned = "returned"
    blocked = "blocked"


class ApprovalLevel(str, Enum):
    single = "single"
    dual = "dual"


class ApprovalDecision(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


# --- Tables ----------------------------------------------------------------


class Property(SQLModel, table=True):
    __tablename__ = "properties"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    address: str
    units: int


class Vendor(SQLModel, table=True):
    __tablename__ = "vendors"
    id: Optional[int] = Field(default=None, primary_key=True)
    legal_name: str = Field(index=True)
    dba: Optional[str] = None
    email_domain: Optional[str] = None  # used by LOOKALIKE_VENDOR domain check
    tin_masked: Optional[str] = None  # e.g. "***-**-1234"; never store the full TIN
    w9_received: bool = False
    tin_match_status: TinMatchStatus = TinMatchStatus.pending
    ofac_status: OfacStatus = OfacStatus.pending
    category: str
    card_accepting: bool = False
    enrolled_rails: list[str] = Field(default_factory=list, sa_column=Column(JSON))  # Rail values
    trusted: bool = False
    flagged: bool = False  # e.g. after an ACH return; blocks auto-pay until cleared
    flag_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class VendorBankAccount(SQLModel, table=True):
    __tablename__ = "vendor_bank_accounts"
    id: Optional[int] = Field(default=None, primary_key=True)
    vendor_id: int = Field(foreign_key="vendors.id", index=True)
    routing: str = Field(max_length=9)
    account_last4: str = Field(max_length=4)
    account_hash: str = Field(foreign_key="account_vault.account_hash")  # token; number lives in the vault
    account_type: AccountType = AccountType.checking
    validated: bool = False
    active: bool = True
    effective_from: date
    source: BankAccountSource = BankAccountSource.onboarding


class AccountVault(SQLModel, table=True):
    """Mock tokenization vault. Only payments/vault.py reads account_number (NACHA build)."""

    __tablename__ = "account_vault"
    account_hash: str = Field(primary_key=True)
    account_number: str


class BankChangeRequest(SQLModel, table=True):
    __tablename__ = "bank_change_requests"
    id: Optional[int] = Field(default=None, primary_key=True)
    vendor_id: int = Field(foreign_key="vendors.id", index=True)
    invoice_id: Optional[int] = Field(default=None, foreign_key="invoices.id")  # invoice that triggered it
    requested_via: ChangeRequestChannel
    new_routing: str
    new_account_last4: Optional[str] = None
    new_account_hash: Optional[str] = None  # only when the full number came through a trusted channel
    status: ChangeRequestStatus = ChangeRequestStatus.pending_callback
    callback_done_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class PurchaseOrder(SQLModel, table=True):
    __tablename__ = "purchase_orders"
    id: Optional[int] = Field(default=None, primary_key=True)
    po_number: str = Field(index=True, unique=True)
    vendor_id: int = Field(foreign_key="vendors.id", index=True)
    property_id: int = Field(foreign_key="properties.id")
    amount_cents: int
    status: POStatus = POStatus.open


class WorkOrder(SQLModel, table=True):
    __tablename__ = "work_orders"
    id: Optional[int] = Field(default=None, primary_key=True)
    po_id: int = Field(foreign_key="purchase_orders.id", index=True)
    status: WorkOrderStatus = WorkOrderStatus.open
    completed_at: Optional[datetime] = None


class Invoice(SQLModel, table=True):
    __tablename__ = "invoices"
    id: Optional[int] = Field(default=None, primary_key=True)
    vendor_id: Optional[int] = Field(default=None, foreign_key="vendors.id", index=True)
    invoice_number: Optional[str] = Field(default=None, index=True)
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    amount_cents: Optional[int] = None
    terms: Optional[str] = None  # e.g. "2/10 net 30"
    po_number: Optional[str] = None
    remit_routing: Optional[str] = None  # remit-to bank as stated on the invoice
    remit_account_last4: Optional[str] = None
    source_file: str  # relative to the data dir
    channel: Optional[str] = None  # pdf / email
    sender_email: Optional[str] = None  # From: address when received by email
    status: InvoiceStatus = InvoiceStatus.received
    risk_score: Optional[int] = None
    extracted_json: Optional[str] = None  # raw InvoiceExtraction JSON
    intake_issues_json: Optional[str] = None  # deterministic validation findings
    match_status: Optional[str] = None  # full / partial / none
    match_json: Optional[str] = None
    signals_json: Optional[str] = None  # deterministic fraud signals with evidence
    fraud_rationale_json: Optional[str] = None  # LLM explanation for the reviewer (never a decision)
    decision: Optional[str] = None  # AUTO_APPROVE / SINGLE_APPROVAL / DUAL_APPROVAL / HOLD_CALLBACK / BLOCK
    decision_reasons_json: Optional[str] = None
    # Seeded ground truth for demo metrics; never read by agents or rules.
    is_fraud: bool = False
    demo_case: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


class Payment(SQLModel, table=True):
    __tablename__ = "payments"
    id: Optional[int] = Field(default=None, primary_key=True)
    invoice_id: int = Field(foreign_key="invoices.id", index=True)
    rail: Rail
    amount_cents: int
    fee_cents: int = 0
    discount_cents: int = 0
    rebate_cents: int = 0
    scheduled_date: date  # send / value date
    expected_settle_date: Optional[date] = None
    status: PaymentStatus = PaymentStatus.scheduled
    attempt: int = 1
    idempotency_key: str = Field(unique=True)  # f"{payment_id}-{attempt}"
    batch_id: Optional[int] = Field(default=None, foreign_key="payment_batches.id")
    return_code: Optional[str] = None
    bank_reference: Optional[str] = None  # mock bank's id for the payment
    rail_plan_json: Optional[str] = None  # deterministic rail choice + alternatives considered
    rail_explanation: Optional[str] = None  # LLM-written, for humans only
    created_at: datetime = Field(default_factory=utcnow)


class PaymentBatch(SQLModel, table=True):
    __tablename__ = "payment_batches"
    id: Optional[int] = Field(default=None, primary_key=True)
    rail: Rail
    file_path: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    total_cents: int = 0
    entry_count: int = 0
    idempotency_key: Optional[str] = None  # one per NACHA file; reused for every resubmission of that file
    submitted: bool = True  # False while the bank hasn't confirmed the file (it will be resent, never rebuilt)


class Approval(SQLModel, table=True):
    __tablename__ = "approvals"
    id: Optional[int] = Field(default=None, primary_key=True)
    payment_id: int = Field(foreign_key="payments.id", index=True)
    required_level: ApprovalLevel
    approver_1: Optional[str] = None
    approver_2: Optional[str] = None
    decision: ApprovalDecision = ApprovalDecision.pending
    reason: Optional[str] = None
    decided_at: Optional[datetime] = None


class BankStatementLine(SQLModel, table=True):
    __tablename__ = "bank_statement_lines"
    __table_args__ = (UniqueConstraint("date", "bank_reference"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    date: date
    amount_cents: int
    type_code: str  # BAI2 type code
    reference: Optional[str] = None  # customer reference (ours)
    bank_reference: str = ""
    detail: Optional[str] = None  # BAI2 88 continuation text
    return_code: Optional[str] = None
    matched_payment_id: Optional[int] = Field(default=None, foreign_key="payments.id")


class ExceptionKind(str, Enum):
    ach_return = "ach_return"
    unmatched_line = "unmatched_line"
    missing_settlement = "missing_settlement"
    amount_mismatch = "amount_mismatch"


class ReconException(SQLModel, table=True):
    """Reconciliation exceptions queue: everything a human must look at."""

    __tablename__ = "recon_exceptions"
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: ExceptionKind
    statement_line_id: Optional[int] = Field(default=None, foreign_key="bank_statement_lines.id")
    payment_id: Optional[int] = Field(default=None, foreign_key="payments.id")
    detail: str
    draft_notice_json: Optional[str] = None  # LLM-drafted vendor notice; never sent automatically
    status: str = "open"  # open / resolved
    created_at: datetime = Field(default_factory=utcnow)


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    actor: str  # agent name or user id
    entity: str  # table name
    entity_id: Optional[int] = None
    action: str
    before_json: Optional[str] = None
    after_json: Optional[str] = None
    reason: Optional[str] = None
