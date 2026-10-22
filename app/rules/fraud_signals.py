"""Deterministic fraud-signal detectors (SPEC §6.3).

Each detector is a pure function of a FraudContext and returns a Signal (with
evidence a reviewer can check) or None. Weights and thresholds come from
config/fraud.yaml. The one LLM-derived input, pressure language, arrives already
verified against the document text by the intake agent.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel
from rapidfuzz import fuzz

from app.config import fraud_config
from app.rules.names import normalize_name


class Signal(BaseModel):
    code: str
    weight: int
    evidence: str


@dataclass(frozen=True)
class PriorInvoice:
    invoice_id: int
    invoice_number: str | None
    amount_cents: int | None
    invoice_date: date | None
    status: str


@dataclass
class FraudContext:
    today: date
    invoice_id: int
    vendor_name_on_invoice: str | None
    invoice_number: str | None
    invoice_date: date | None
    amount_cents: int | None
    remit_routing: str | None
    remit_last4: str | None
    channel: str | None  # pdf / email
    sender_domain: str | None
    email_mentions_bank_change: bool
    pressure_language: str | None
    # vendor master (None when the vendor could not be resolved)
    vendor_id: int | None
    vendor_legal_name: str | None
    vendor_domain: str | None
    active_routing: str | None
    active_last4: str | None
    active_since: date | None
    settled_amounts: list[int] = field(default_factory=list)
    prior_invoices: list[PriorInvoice] = field(default_factory=list)  # same vendor, excluding this one
    known_vendor_names: dict[str, str] = field(default_factory=dict)  # normalized -> legal name
    known_domains: dict[str, str] = field(default_factory=dict)  # domain -> legal name
    sdn_names: list[str] = field(default_factory=list)


def _cfg(code: str) -> dict:
    return fraud_config()["signals"][code]


def _sig(code: str, evidence: str) -> Signal:
    return Signal(code=code, weight=_cfg(code)["weight"], evidence=evidence)


# --- detectors -----------------------------------------------------------------------


def bank_details_changed(c: FraudContext) -> Signal | None:
    if not c.remit_routing:
        return None
    if c.active_routing is None:
        return _sig("BANK_DETAILS_CHANGED",
                    f"invoice gives bank {c.remit_routing} / ****{c.remit_last4} but no account is on file")
    if (c.remit_routing, c.remit_last4) != (c.active_routing, c.active_last4):
        return _sig("BANK_DETAILS_CHANGED",
                    f"invoice remit-to {c.remit_routing} / ****{c.remit_last4} differs from the account on file "
                    f"{c.active_routing} / ****{c.active_last4}")
    return None


def bank_change_via_email(c: FraudContext) -> Signal | None:
    if c.channel != "email":
        return None
    if c.email_mentions_bank_change:
        return _sig("BANK_CHANGE_VIA_EMAIL", f"email from {c.sender_domain} asks to change payment details")
    if bank_details_changed(c):  # new bank details arriving by email is the BEC pattern even if unstated
        return _sig("BANK_CHANGE_VIA_EMAIL", f"email from {c.sender_domain} carries different bank details")
    return None


def lookalike_vendor(c: FraudContext) -> Signal | None:
    cfg = _cfg("LOOKALIKE_VENDOR")
    if c.sender_domain and c.sender_domain not in c.known_domains:
        best = max(c.known_domains, key=lambda d: fuzz.ratio(c.sender_domain, d), default=None)
        if best and fuzz.ratio(c.sender_domain, best) >= cfg["domain_similarity_min"]:
            return _sig("LOOKALIKE_VENDOR", f"sender domain {c.sender_domain} imitates {best} "
                                            f"({c.known_domains[best]})")
    name = normalize_name(c.vendor_name_on_invoice)
    if name and name not in c.known_vendor_names:
        best = max(c.known_vendor_names, key=lambda n: fuzz.ratio(name, n), default=None)
        if best and fuzz.ratio(name, best) >= cfg["name_similarity_min"]:
            return _sig("LOOKALIKE_VENDOR", f"vendor name {c.vendor_name_on_invoice!r} is close to but not "
                                            f"the same as {c.known_vendor_names[best]!r}")
    return None


def invoice_number_key(number: str | None) -> str:
    """'INV-1043-A', 'inv 1043a', 'INV1043' all -> 'INV1043'."""
    key = re.sub(r"[^A-Z0-9]", "", (number or "").upper())
    return re.sub(r"(?<=\d)[A-Z]{1,2}$", "", key)


def duplicate_invoice(c: FraudContext) -> Signal | None:
    window = _cfg("DUPLICATE_INVOICE")["window_days"]
    key = invoice_number_key(c.invoice_number)
    for p in c.prior_invoices:
        if key and invoice_number_key(p.invoice_number) == key:
            return _sig("DUPLICATE_INVOICE", f"invoice {c.invoice_number!r} matches {p.invoice_number!r} "
                                             f"(invoice #{p.invoice_id}, {p.status})")
        if (c.amount_cents is not None and p.amount_cents == c.amount_cents and c.invoice_date and p.invoice_date
                and abs((c.invoice_date - p.invoice_date).days) <= window):
            return _sig("DUPLICATE_INVOICE", f"same amount {c.amount_cents} cents as {p.invoice_number!r} "
                                             f"dated {p.invoice_date} (within {window} days)")
    return None


def amount_anomaly(c: FraudContext) -> Signal | None:
    cfg = _cfg("AMOUNT_ANOMALY")
    if c.amount_cents is None or len(c.settled_amounts) < cfg["min_history"]:
        return None
    mean = statistics.fmean(c.settled_amounts)
    sd = max(statistics.stdev(c.settled_amounts), mean * cfg["min_sd_pct"] / 100)
    limit = mean + cfg["sigma"] * sd
    if c.amount_cents > limit:
        return _sig("AMOUNT_ANOMALY", f"amount {c.amount_cents} cents exceeds {cfg['sigma']} sd above this "
                                      f"vendor's mean {mean:.0f} (limit {limit:.0f}, n={len(c.settled_amounts)})")
    return None


def first_time_payee(c: FraudContext) -> Signal | None:
    if not c.settled_amounts:
        return _sig("FIRST_TIME_PAYEE", "no settled payments to this payee")
    return None


def new_account_recent(c: FraudContext) -> Signal | None:
    days = _cfg("NEW_ACCOUNT_RECENT")["days"]
    if c.active_since and (c.today - c.active_since).days < days:
        return _sig("NEW_ACCOUNT_RECENT", f"bank account on file since {c.active_since} "
                                          f"({(c.today - c.active_since).days} days ago, threshold {days})")
    return None


def urgency_language(c: FraudContext) -> Signal | None:
    if c.pressure_language:
        return _sig("URGENCY_LANGUAGE", f"document says: {c.pressure_language!r}")
    return None


def ofac_hit(c: FraudContext) -> Signal | None:
    threshold = _cfg("OFAC_HIT")["name_similarity_min"]
    for name in {c.vendor_legal_name, c.vendor_name_on_invoice} - {None}:
        n = normalize_name(name)
        for sdn in c.sdn_names:
            score = fuzz.token_sort_ratio(n, normalize_name(sdn))
            if score >= threshold:
                return _sig("OFAC_HIT", f"{name!r} matches SDN entry {sdn!r} (score {score:.0f})")
    return None


DETECTORS: list[Callable[[FraudContext], Signal | None]] = [
    ofac_hit, bank_details_changed, bank_change_via_email, lookalike_vendor, duplicate_invoice,
    amount_anomaly, first_time_payee, new_account_recent, urgency_language,
]


def detect(c: FraudContext) -> list[Signal]:
    return [s for d in DETECTORS if (s := d(c)) is not None]


def risk_score(signals: list[Signal]) -> int:
    return min(sum(s.weight for s in signals), fraud_config()["max_score"])
