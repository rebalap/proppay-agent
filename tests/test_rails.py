from datetime import date, datetime

import pytest

from app.rules import rails

MON_9AM = datetime(2026, 10, 19, 9, 0)
ACH = ["ach_standard", "ach_same_day", "check"]


def plan(**kw):
    base = dict(amount_cents=250_000, vendor_rails=ACH, card_accepting=False, terms="Net 30",
                invoice_date=date(2026, 10, 10), due_date=date(2026, 11, 9), risk_score=0, now=MON_9AM)
    return rails.plan(**{**base, **kw})


# --- dates & terms ----------------------------------------------------------------------


def test_business_days_skip_weekends():
    assert rails.add_business_days(date(2026, 10, 23), 1) == date(2026, 10, 26)  # Fri -> Mon
    assert rails.next_business_day(date(2026, 10, 24)) == date(2026, 10, 26)


@pytest.mark.parametrize("terms,expected", [
    ("2/10 Net 30", (2, 10, 30)), ("2% 10, net 30", (2, 10, 30)), ("1.5/15 net 45", (1.5, 15, 45)),
    ("Net 30", (0, 0, 30)), ("Due upon receipt", (0, 0, None)), (None, (0, 0, None)),
])
def test_parse_terms(terms, expected):
    t = rails.parse_terms(terms)
    assert (t.discount_pct, t.discount_days, t.net_days) == expected


def test_discount_annualized():
    assert round(rails.discount_annualized_bps(rails.parse_terms("2/10 Net 30"))) == 3724  # ~37.2%/yr


def test_cutoffs():
    cfg = rails.rails_config()["rails"]
    assert rails.earliest_send("ach_same_day", cfg["ach_same_day"], datetime(2026, 10, 19, 14, 45)) == date(2026, 10, 19)
    assert rails.earliest_send("ach_same_day", cfg["ach_same_day"], datetime(2026, 10, 19, 14, 46)) == date(2026, 10, 20)
    assert rails.earliest_send("rtp", cfg["rtp"], datetime(2026, 10, 24, 23, 0)) == date(2026, 10, 24)  # Saturday


# --- rail choice ----------------------------------------------------------------------------


def test_routine_ach_sent_as_late_as_possible():
    p = plan()
    assert p.rail == "ach_standard" and p.on_time
    assert (p.send_date, p.settle_date) == (date(2026, 11, 6), date(2026, 11, 9))  # Fri send -> Mon settle
    assert p.fee_cents == 25 and p.pay_amount_cents == 250_000


def test_instant_excluded_when_not_urgent():
    p = plan(vendor_rails=["ach_standard", "rtp"])
    assert p.rail == "ach_standard"
    assert next(o for o in p.options if o.rail == "rtp").reason == "instant rails only for urgent payments"


def test_urgent_low_risk_takes_cheapest_same_day_rail():
    p = plan(vendor_rails=["ach_standard", "ach_same_day", "rtp", "check"], due_date=date(2026, 10, 19),
             terms="Due upon receipt", amount_cents=380_000)
    assert p.urgent and p.rail == "rtp" and p.settle_date == date(2026, 10, 19)  # $0.50 beats same-day ACH $1.00


def test_urgent_high_risk_never_instant():
    p = plan(vendor_rails=["ach_standard", "ach_same_day", "rtp"], due_date=date(2026, 10, 19), risk_score=25)
    assert p.rail == "ach_same_day"
    assert "too high for an irrevocable rail" in next(o for o in p.options if o.rail == "rtp").reason


def test_after_same_day_cutoff_pays_late_on_cheapest_fastest():
    # 15:00: same-day ACH missed its 14:45 cutoff; standard ACH (17:00 cutoff) settles tomorrow just as
    # fast as tomorrow's same-day ACH, and is cheaper.
    p = plan(due_date=date(2026, 10, 19), now=datetime(2026, 10, 19, 15, 0))
    assert not p.on_time and p.rail == "ach_standard" and p.settle_date == date(2026, 10, 20)
    assert "no eligible rail settles by" in p.reasons[-1]


def test_card_rebate_wins():
    p = plan(vendor_rails=[*ACH, "virtual_card"], card_accepting=True, amount_cents=180_000)
    assert p.rail == "virtual_card" and p.rebate_cents == 1_800 and p.fee_cents == 0


def test_card_needs_acceptance():
    p = plan(vendor_rails=[*ACH, "virtual_card"], card_accepting=False)
    assert p.rail == "ach_standard"


def test_wire_only_vendor():
    p = plan(vendor_rails=["wire"], amount_cents=48_000_000, due_date=date(2026, 10, 21))
    assert p.rail == "wire" and p.send_date == date(2026, 10, 21) and p.fee_cents == 2_500


def test_amount_over_limit_excludes_rail():
    p = plan(vendor_rails=["ach_same_day", "wire"], amount_cents=100_000_001, due_date=date(2026, 10, 19))
    assert p.rail == "wire"


def test_early_pay_discount_captured():
    p = plan(terms="2/10 Net 30", invoice_date=date(2026, 10, 15), due_date=date(2026, 11, 14), amount_cents=271_900)
    assert p.target_date == date(2026, 10, 25) and p.discount_cents == 5_438
    assert p.pay_amount_cents == 271_900 - 5_438
    assert p.settle_date == date(2026, 10, 23)  # deadline is a Sunday -> settle the Friday before


def test_closed_discount_window_pays_full_on_due_date():
    p = plan(terms="2/10 Net 30", invoice_date=date(2026, 9, 25), due_date=date(2026, 10, 25))
    assert p.discount_cents == 0 and p.target_date == date(2026, 10, 25)


def test_discount_below_cost_of_capital_not_taken():
    p = plan(terms="0.1/10 Net 60", invoice_date=date(2026, 10, 15), due_date=date(2026, 12, 14))
    assert p.discount_cents == 0 and "does not beat" in " ".join(p.reasons)


def test_no_eligible_rail():
    p = plan(vendor_rails=[])
    assert p.rail is None
