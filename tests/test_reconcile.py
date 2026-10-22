"""Phase 6 acceptance: the R03 case reopens the invoice; the statement is otherwise fully matched."""

import json
from datetime import date

import pytest
from sqlmodel import Session, select

from app import graph as G
from app.agents.decision import policy_input
from app.models import (
    BankStatementLine,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    ReconException,
    Vendor,
    VendorBankAccount,
)
from app.payments import bai2
from tests.test_graph import env  # noqa: F401  (fixture)

THROUGH = date(2026, 11, 20)


def full_run(env):
    G.run_ap(env["deps"], env["graph"])
    G.resume(env["deps"], env["graph"], env["case"][2][0], {"verified": False, "by": "dana"})
    G.resume(env["deps"], env["graph"], env["case"][3][0], {"approver": "alice", "decision": "approve"})
    G.resume(env["deps"], env["graph"], env["case"][5][0], {"approver": "alice", "decision": "approve"})
    G.resume(env["deps"], env["graph"], env["case"][5][0], {"approver": "bob", "decision": "approve"})


def bank_state(env):
    return env["bank"].app.state.bank.state


# --- BAI2 ---------------------------------------------------------------------------------------


def test_bai2_from_mock_bank_parses_and_balances(env):
    full_run(env)
    day = next(t["value_date"] for t in bank_state(env)["transactions"] if t["kind"] == "ach_return")
    lines, errors = bai2.parse(env["bank"].get("/statement", params={"date": day}).text)
    assert errors == [] and lines[0].return_code == "R03" and lines[0].type_code == "166"


@pytest.mark.parametrize("tamper,msg", [
    (lambda t: t.replace("16,495,", "16,495,1", 1), "control total"),
    (lambda t: "\n".join(t.splitlines()[:-1]), "missing 99"),
    (lambda t: t.replace("\n16,", "\n16,", 1).replace("/\n49,", "/\n16,451,1,0,X,Y/\n49,", 1), "record count"),
])
def test_bai2_tampering_detected(tamper, msg):
    text = ("01,PROPPAYMOCK,BLUESTEM,261019,0600,1,,,2/\n02,BLUESTEM,099999999,1,261019,,USD,2/\n03,1,USD/\n"
            "16,495,380000,0,RTP-1,PA-1 PAY1/\n49,380000,3/\n98,380000,1,5/\n99,380000,1,7/\n")
    assert bai2.parse(text)[1] == []
    errors = bai2.parse(tamper(text))[1]
    assert any(msg in e for e in errors), errors


# --- the demo flow ----------------------------------------------------------------------------------


def test_r03_reopens_invoice_and_everything_else_matches(env):
    full_run(env)
    report = G.reconcile_through(env["deps"], THROUGH)
    assert report.fully_matched and report.statement_errors == {}

    with Session(env["engine"]) as s:
        keystone = env["case"][7][0]
        pay = s.exec(select(Payment).where(Payment.invoice_id == keystone)).one()
        assert report.returned == [pay.id]
        assert pay.status == PaymentStatus.returned and pay.return_code == "R03"
        inv = s.get(Invoice, keystone)
        assert inv.status == InvoiceStatus.reopened
        vendor = s.get(Vendor, inv.vendor_id)
        assert vendor.flagged and "R03" in vendor.flag_reason
        assert not s.exec(select(VendorBankAccount).where(VendorBankAccount.vendor_id == vendor.id)).one().validated
        assert policy_input(s, inv).vendor_flagged  # next invoice from this vendor needs a human

        (exc,) = s.exec(select(ReconException).where(ReconException.status == "open")).all()
        assert exc.kind.value == "ach_return" and "re-verify" in exc.detail
        assert json.loads(exc.draft_notice_json)["subject"]

        # every other payment that went to the bank settled, and its invoice is paid
        others = s.exec(select(Payment).where(Payment.id != pay.id, Payment.status.in_(
            [PaymentStatus.sent, PaymentStatus.settled]))).all()
        sent_this_run = [p for p in others if p.invoice_id >= min(i for c in env["case"].values() for i in c)]
        assert len(sent_this_run) == 22 and all(p.status == PaymentStatus.settled for p in sent_this_run)
        assert all(s.get(Invoice, p.invoice_id).status == InvoiceStatus.paid for p in sent_this_run)
        assert all(ln.matched_payment_id for ln in s.exec(select(BankStatementLine)).all())
    assert env["llm"].calls["notice"] == 1


def test_reconcile_is_idempotent(env):
    full_run(env)
    first = G.reconcile_through(env["deps"], THROUGH)
    again = G.reconcile_through(env["deps"], THROUGH)
    assert again.lines_new == 0 and again.matched == 0 and again.returned == [] and again.fully_matched
    with Session(env["engine"]) as s:
        assert len(s.exec(select(ReconException)).all()) == len(first.exceptions) == 1


def test_partial_window_has_nothing_overdue(env):
    full_run(env)
    r = G.reconcile_through(env["deps"], date(2026, 10, 23))  # before most ACH settles; R03 not yet
    assert r.fully_matched and r.returned == [] and r.exceptions == []


def test_missing_settlement_raises_exception(env):
    full_run(env)
    state = bank_state(env)
    state["transactions"] = [t for t in state["transactions"] if t["kind"] != "wire"]  # the bank "lost" the wire
    r = G.reconcile_through(env["deps"], THROUGH)
    with Session(env["engine"]) as s:
        kinds = {e.kind.value: e for e in s.exec(select(ReconException)).all()}
        assert "missing_settlement" in kinds and "wire" in kinds["missing_settlement"].detail
        assert s.get(Payment, kinds["missing_settlement"].payment_id).status == PaymentStatus.sent
    assert r.fully_matched  # every line matched; the gap is on our side and is queued


def test_unknown_bank_debit_is_an_exception(env):
    full_run(env)
    bank_state(env)["transactions"].append({
        "id": "TX999", "kind": "ach", "type_code": "451", "direction": "debit", "amount_cents": 12_345,
        "value_date": "2026-10-22", "bank_reference": "UNKNOWN-1", "customer_reference": "SOMETHING ELSE"})
    r = G.reconcile_through(env["deps"], THROUGH)
    assert not r.fully_matched and r.unmatched_lines == 1
    with Session(env["engine"]) as s:
        exc = s.exec(select(ReconException).where(ReconException.kind == "unmatched_line")).one()
        assert "$123.45" in exc.detail


class TamperedStatements:
    def __init__(self, inner, bad_date):
        self.inner, self.bad_date = inner, bad_date

    def get(self, path, params):
        resp = self.inner.get(path, params=params)
        if params["date"] == self.bad_date:
            resp._content = resp.content.replace(b"\n49,", b"\n16,451,1,0,X,Y/\n49,", 1)
        return resp


def test_statement_failing_control_totals_is_not_reconciled(env):
    full_run(env)
    env["deps"].bank = TamperedStatements(env["bank"], "2026-10-19")
    r = G.reconcile_through(env["deps"], date(2026, 10, 19))
    assert "2026-10-19" in r.statement_errors and r.lines_new == 0 and not r.fully_matched


def test_fallback_match_by_amount_and_date(env):
    full_run(env)
    for t in bank_state(env)["transactions"]:
        if t["kind"] == "card":
            t["customer_reference"] = "card program settlement"  # reference lost by the bank
    r = G.reconcile_through(env["deps"], THROUGH)
    assert r.fully_matched
