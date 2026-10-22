"""Unit tests for the pure rules: fraud detectors and approval policy."""

from dataclasses import replace
from datetime import date

import pytest

from app.rules import fraud_signals as fs
from app.rules.policy import Outcome, PolicyInput, decide

TODAY = date(2026, 10, 19)


def ctx(**kw) -> fs.FraudContext:
    base = dict(
        today=TODAY, invoice_id=1, vendor_name_on_invoice="Acme Plumbing LLC", invoice_number="AP-100",
        invoice_date=date(2026, 10, 15), amount_cents=250_000, remit_routing=None, remit_last4=None,
        channel="pdf", sender_domain=None, email_mentions_bank_change=False, pressure_language=None,
        vendor_id=1, vendor_legal_name="Acme Plumbing LLC", vendor_domain="acmeplumbing.com",
        active_routing="021000021", active_last4="1234", active_since=date(2025, 1, 1),
        settled_amounts=[240_000, 260_000, 255_000, 245_000],
        known_vendor_names={"ACME PLUMBING": "Acme Plumbing LLC", "GREENLEAF LANDSCAPING": "Greenleaf Landscaping Inc."},
        known_domains={"acmeplumbing.com": "Acme Plumbing LLC", "greenleaflandscape.com": "Greenleaf Landscaping Inc."},
        sdn_names=["MERIDIAN SUPPLY AND LOGISTICS FZE"],
    )
    return fs.FraudContext(**{**base, **kw})


def codes(c):
    return {s.code for s in fs.detect(c)}


def test_clean_invoice_has_no_signals():
    assert codes(ctx()) == set()


# --- bank details -------------------------------------------------------------------


def test_same_bank_is_fine():
    assert fs.bank_details_changed(ctx(remit_routing="021000021", remit_last4="1234")) is None


@pytest.mark.parametrize("routing,last4", [("011000015", "1234"), ("021000021", "9999")])
def test_different_bank_flags(routing, last4):
    assert fs.bank_details_changed(ctx(remit_routing=routing, remit_last4=last4))


def test_bank_details_with_nothing_on_file_flags():
    assert fs.bank_details_changed(ctx(remit_routing="011000015", remit_last4="1", active_routing=None,
                                       active_last4=None))


def test_new_bank_by_email_is_bec_even_if_not_stated():
    c = ctx(channel="email", sender_domain="acmeplumbing.com", remit_routing="011000015", remit_last4="5555")
    assert {"BANK_DETAILS_CHANGED", "BANK_CHANGE_VIA_EMAIL"} <= codes(c)


def test_bank_change_text_only_counts_for_email():
    assert fs.bank_change_via_email(ctx(email_mentions_bank_change=True, channel="pdf")) is None


# --- look-alikes ----------------------------------------------------------------------


def test_lookalike_domain():
    s = fs.lookalike_vendor(ctx(channel="email", sender_domain="acmeplumbimg.com"))
    assert s and "acmeplumbing.com" in s.evidence


def test_known_and_unrelated_domains_do_not_flag():
    assert fs.lookalike_vendor(ctx(sender_domain="acmeplumbing.com")) is None
    assert fs.lookalike_vendor(ctx(sender_domain="gmail.com")) is None


def test_lookalike_name():
    assert fs.lookalike_vendor(ctx(vendor_name_on_invoice="Acme Plumbimg LLC"))


@pytest.mark.parametrize("name", ["ACME PLUMBING LLC", "Acme Plumbing, L.L.C.", "Acme Plumbing Inc"])
def test_name_formatting_is_not_a_lookalike(name):
    assert fs.lookalike_vendor(ctx(vendor_name_on_invoice=name)) is None


# --- duplicates -----------------------------------------------------------------------


@pytest.mark.parametrize("a,b", [("INV-1043-A", "INV-1043"), ("inv 1043a", "INV1043"), ("INV-1043.B", "INV-1043")])
def test_invoice_number_key(a, b):
    assert fs.invoice_number_key(a) == fs.invoice_number_key(b)


def test_number_key_keeps_distinct_numbers():
    assert fs.invoice_number_key("CE2881") != fs.invoice_number_key("CE2882")
    assert fs.invoice_number_key("SBG-PA-006") != fs.invoice_number_key("6")


def test_duplicate_by_number_and_by_amount_date():
    prior = fs.PriorInvoice(9, "AP-100", 100, date(2026, 9, 1), "paid")
    assert fs.duplicate_invoice(ctx(invoice_number="AP-100-A", prior_invoices=[prior]))
    same_amount = fs.PriorInvoice(9, "AP-099", 250_000, date(2026, 10, 1), "paid")
    assert fs.duplicate_invoice(ctx(prior_invoices=[same_amount]))
    too_old = replace(same_amount, invoice_date=date(2026, 8, 1))
    assert fs.duplicate_invoice(ctx(prior_invoices=[too_old])) is None


# --- history-based -----------------------------------------------------------------------


def test_amount_anomaly():
    assert fs.amount_anomaly(ctx(amount_cents=260_000)) is None
    assert fs.amount_anomaly(ctx(amount_cents=900_000))


def test_amount_anomaly_needs_history_and_sd_floor():
    assert fs.amount_anomaly(ctx(amount_cents=900_000, settled_amounts=[100, 100])) is None
    flat = [100_000] * 5  # sd 0 -> floor at 10% of mean, so +35% trips 3 sd
    assert fs.amount_anomaly(ctx(amount_cents=129_000, settled_amounts=flat)) is None
    assert fs.amount_anomaly(ctx(amount_cents=135_000, settled_amounts=flat))


def test_first_time_payee_and_new_account():
    assert fs.first_time_payee(ctx(settled_amounts=[]))
    assert fs.new_account_recent(ctx(active_since=date(2026, 10, 1)))
    assert fs.new_account_recent(ctx(active_since=date(2026, 9, 19))) is None  # exactly 30 days


def test_ofac_matches_normalized_names_only_above_threshold():
    assert fs.ofac_hit(ctx(vendor_legal_name="Meridian Supply & Logistics", vendor_name_on_invoice=None))
    assert fs.ofac_hit(ctx(vendor_legal_name=None, vendor_name_on_invoice="MERIDIAN SUPPLY AND LOGISTICS"))
    assert fs.ofac_hit(ctx(vendor_legal_name="Meridian Property Services")) is None


def test_risk_score_capped():
    sigs = [fs.Signal(code="X", weight=60, evidence=""), fs.Signal(code="Y", weight=60, evidence="")]
    assert fs.risk_score(sigs) == 100


# --- policy ---------------------------------------------------------------------------------


def pin(**kw) -> PolicyInput:
    base = dict(amount_cents=250_000, vendor_trusted=True, match_status="full", risk_score=0, signals=[])
    return PolicyInput(**{**base, **kw})


def test_auto_approve():
    assert decide(pin()).outcome == Outcome.AUTO_APPROVE


def test_auto_limit_is_inclusive():
    assert decide(pin(amount_cents=500_000)).outcome == Outcome.AUTO_APPROVE
    assert decide(pin(amount_cents=500_001)).outcome == Outcome.SINGLE_APPROVAL


@pytest.mark.parametrize("kw,reason", [
    ({"vendor_trusted": False}, "require_trusted_vendor"),
    ({"match_status": "partial"}, "require_full_match"),
    ({"match_status": None}, "require_full_match"),
    ({"risk_score": 21}, "max_risk_score"),
    ({"intake_needs_review": True}, "intake"),
    ({"amount_cents": None}, "amount unknown"),
    ({"signals": ["DUPLICATE_INVOICE"], "risk_score": 0}, "force_review"),
])
def test_single_approval_reasons(kw, reason):
    d = decide(pin(**kw))
    assert d.outcome == Outcome.SINGLE_APPROVAL and any(reason in r for r in d.reasons)


def test_dual_triggers():
    assert decide(pin(amount_cents=5_000_000)).outcome == Outcome.DUAL_APPROVAL
    assert decide(pin(rail="wire")).outcome == Outcome.DUAL_APPROVAL
    assert decide(pin(amount_cents=2_500_000, rail="rtp")).outcome == Outcome.DUAL_APPROVAL
    assert decide(pin(amount_cents=2_499_999, rail="fednow")).outcome == Outcome.SINGLE_APPROVAL  # > auto limit only
    assert decide(pin(amount_cents=2_500_000, rail="ach_standard")).outcome == Outcome.SINGLE_APPROVAL


def test_duplicate_above_dual_threshold_is_dual():
    d = decide(pin(amount_cents=6_000_000, signals=["DUPLICATE_INVOICE"]))
    assert d.outcome == Outcome.DUAL_APPROVAL


def test_precedence_block_over_hold_over_dual():
    assert decide(pin(signals=["OFAC_HIT", "BANK_DETAILS_CHANGED"])).outcome == Outcome.BLOCK
    assert decide(pin(amount_cents=9_000_000, signals=["BANK_CHANGE_VIA_EMAIL"])).outcome == Outcome.HOLD_CALLBACK


def test_verified_callback_lifts_hold_only():
    d = decide(pin(signals=["BANK_DETAILS_CHANGED"], risk_score=40, callback_verified=True))
    assert d.outcome == Outcome.SINGLE_APPROVAL  # still too risky to auto-pay
    assert decide(pin(signals=["OFAC_HIT"], callback_verified=True)).outcome == Outcome.BLOCK


def test_rail_can_only_escalate():
    base = pin(amount_cents=300_000)
    for rail in (None, "ach_standard", "ach_same_day", "rtp", "fednow", "wire", "virtual_card", "check"):
        order = list(Outcome)
        assert order.index(decide(base.model_copy(update={"rail": rail})).outcome) >= order.index(decide(base).outcome)


def test_flagged_vendor_cannot_be_auto_paid():
    d = decide(pin(vendor_flagged=True))
    assert d.outcome == Outcome.SINGLE_APPROVAL and any("block_flagged_vendors" in r for r in d.reasons)
