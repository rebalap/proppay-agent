"""Dashboard metrics on the full demo flow."""

from sqlmodel import Session

from app import graph as G
from app.metrics import compute
from tests.test_graph import env  # noqa: F401  (fixture)
from tests.test_reconcile import THROUGH, full_run


def test_metrics_after_full_demo(env):
    full_run(env)
    G.reconcile_through(env["deps"], THROUGH)
    with Session(env["engine"]) as s:
        m = compute(s)
    assert (m.invoices, m.processed) == (25, 25)
    assert m.stp_count == 20 and m.stp_rate == 0.8  # 21 auto-approved, minus Keystone (returned R03)
    assert m.fraud_prevented_count == 1 and m.fraud_prevented_cents == 284_000  # the BEC email, blocked
    assert m.sanctions_blocked_count == 1 and m.returns_count == 1 and m.exceptions_open == 1
    assert set(m.paid_by_rail) == {"ach_standard", "rtp", "wire", "virtual_card"}
    assert m.wire_baseline_cents - m.fees_cents == m.rail_savings_cents > 0
    assert m.discounts_cents > 0 and m.rebates_cents > 0
    assert m.awaiting_human == 0


def test_metrics_mid_run(env):
    G.run_ap(env["deps"], env["graph"])
    with Session(env["engine"]) as s:
        m = compute(s)
    assert m.stp_count == 21 and m.awaiting_human == 3 and m.fraud_prevented_count == 1  # BEC on hold counts
