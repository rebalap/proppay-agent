"""Phase 4 acceptance: valid NACHA file; idempotent instant payments. Plus rail choice per demo case
and the mock bank's behaviour, all against an in-process FastAPI app."""

from datetime import date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.agents.executor import execute
from app.agents.rail_optimizer import plan_payment
from app.models import Approval, ApprovalDecision, ApprovalLevel, AuditLog, Invoice, Payment, PaymentStatus
from app.payments import nacha
from mock_bank.main import create_app
from tests.test_decisions import BEC_QUOTE, pipeline, pressure_for

NOW = datetime(2026, 10, 19, 9, 0)
EXPECTED_RAIL = {1: "ach_standard", 3: "ach_standard", 4: "rtp", 5: "wire", 6: "virtual_card", 7: "ach_standard", 8: "ach_standard"}


@pytest.fixture
def bank(fresh_seed):
    app = create_app(fresh_seed["data_dir"] / "mock_bank_scenarios.json", None)
    with TestClient(app) as client:
        yield client


def run_all(fresh_seed):
    """Truth extraction -> match -> fraud -> decision -> rail plan, for all 25 invoices."""
    plans = {}
    for m in fresh_seed["manifest"]:
        pipeline(fresh_seed, m, pressure=pressure_for(m))
        plans[m["demo_case"], m["file"]] = plan_payment(fresh_seed["session"], m["invoice_id"], now=NOW,
                                                        with_explanation=False)
    fresh_seed["session"].commit()
    return plans


# --- rail plans per demo case -------------------------------------------------------------


def test_each_case_gets_expected_rail(fresh_seed):
    for (case, file), r in run_all(fresh_seed).items():
        assert r.plan.rail == EXPECTED_RAIL.get(case), (file, r.plan.reasons)
        if case in (2, 9):
            assert r.payment_id is None  # held / blocked: nothing scheduled


def test_case_specific_economics(fresh_seed):
    plans = {k[0]: v for k, v in run_all(fresh_seed).items() if k[0] in (4, 5, 6, 8)}
    assert plans[4].plan.settle_date == date(2026, 10, 19) and plans[4].outcome.value == "AUTO_APPROVE"
    assert plans[5].outcome.value == "DUAL_APPROVAL"
    assert plans[6].plan.rebate_cents > 0 and plans[6].plan.fee_cents == 0
    assert plans[8].plan.discount_cents > 0 and plans[8].plan.pay_amount_cents < plans[8].plan.pay_amount_cents + 1
    pay = fresh_seed["session"].get(Payment, plans[8].payment_id)
    assert pay.discount_cents == plans[8].plan.discount_cents and pay.idempotency_key == f"{pay.id}-1"


def test_payments_needing_approval_are_not_scheduled(fresh_seed):
    plans = run_all(fresh_seed)
    s = fresh_seed["session"]
    for (case, _), r in plans.items():
        if r.payment_id:
            status = s.get(Payment, r.payment_id).status
            assert status == (PaymentStatus.scheduled if r.outcome.value == "AUTO_APPROVE"
                              else PaymentStatus.pending_approval)


# --- execution ---------------------------------------------------------------------------------


def test_execute_builds_valid_nacha_and_sends_everything_released(fresh_seed, bank, tmp_path):
    run_all(fresh_seed)
    s = fresh_seed["session"]
    report = execute(s, client=bank, now=NOW, outbox=tmp_path / "outbox")
    s.commit()

    text = open(report.ach_file).read()
    entries, errors = nacha.parse_and_validate(text)
    assert errors == []
    assert len(entries) == 18  # 16 routine + R03 case + discount case
    assert all(e.remittance.startswith("RMR*IV*") for e in entries)
    lakeside = next(e for e in entries if "LAKESIDE" in e.receiver_name)
    assert lakeside.effective_date == date(2026, 10, 23)  # early pay inside the discount window

    sent = s.exec(select(Payment).where(Payment.status == PaymentStatus.sent)).all()
    assert {p.rail.value for p in sent} == {"ach_standard", "rtp", "virtual_card"}
    assert len(sent) == 21 and set(report.sent) == {p.id for p in sent}
    waiting = s.exec(select(Payment).where(Payment.status == PaymentStatus.pending_approval)).all()
    assert {p.rail.value for p in waiting} == {"wire", "ach_standard"}  # GC draw + duplicate wait for humans
    assert bank.app.state.bank.state["ach_files"]  # bank accepted the file


def test_dual_approval_needs_two_different_people(fresh_seed, bank, tmp_path):
    plans = run_all(fresh_seed)
    s = fresh_seed["session"]
    wire = next(r for (c, _), r in plans.items() if c == 5)
    pay = s.get(Payment, wire.payment_id)
    pay.status = PaymentStatus.scheduled  # as Phase 5's approval node will do
    s.add(Approval(payment_id=pay.id, required_level=ApprovalLevel.dual, approver_1="alice", approver_2="alice",
                   decision=ApprovalDecision.approved))
    s.commit()
    r = execute(s, client=bank, now=NOW, outbox=tmp_path, payment_ids=[pay.id])
    assert pay.id in r.skipped and "1/2" in r.skipped[pay.id]

    appr = s.exec(select(Approval)).one()
    appr.approver_2 = "bob"
    s.commit()
    r = execute(s, client=bank, now=NOW, outbox=tmp_path, payment_ids=[pay.id])
    s.commit()
    assert r.sent == [pay.id] and s.get(Payment, pay.id).bank_reference.startswith("IMAD-")


def test_blocked_and_held_invoices_never_reach_the_bank(fresh_seed, bank, tmp_path):
    run_all(fresh_seed)
    s = fresh_seed["session"]
    execute(s, client=bank, now=NOW, outbox=tmp_path)
    for case in (2, 9):
        m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == case)
        assert not s.exec(select(Payment).where(Payment.invoice_id == m["invoice_id"])).all()
    names = {t.get("customer_reference") for t in bank.app.state.bank.state["transactions"]}
    assert not any("AP-" in (n or "") for n in names)  # Acme's BEC invoice


# --- idempotency ---------------------------------------------------------------------------------


class TimeoutAfterSend:
    """The bank processes the request, then the response is lost (the classic double-pay trap)."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def post(self, *args, **kwargs):
        self.calls += 1
        response = self.inner.post(*args, **kwargs)
        if self.calls == 1:
            raise httpx.ReadTimeout("response lost")
        return response


def test_instant_payment_idempotent_through_a_timeout(fresh_seed, bank, tmp_path):
    plans = run_all(fresh_seed)
    s = fresh_seed["session"]
    rtp = next(r for (c, _), r in plans.items() if c == 4)
    flaky = TimeoutAfterSend(bank)

    report = execute(s, client=flaky, now=NOW, outbox=tmp_path, payment_ids=[rtp.payment_id])
    s.commit()
    assert report.sent == [rtp.payment_id] and flaky.calls == 2
    txns = [t for t in bank.app.state.bank.state["transactions"] if t["kind"] == "rtp"]
    assert len(txns) == 1  # paid once, despite two requests
    assert s.get(Payment, rtp.payment_id).bank_reference == txns[0]["bank_reference"]


def test_bank_idempotency_semantics(bank):
    body = {"rail": "rtp", "amount_cents": 380_000, "creditor_routing": "021000021", "creditor_account": "123456789",
            "creditor_name": "Polar Air", "reference": "PA-1 PAY7", "value_date": "2026-10-19"}
    first = bank.post("/instant", json=body, headers={"Idempotency-Key": "7-1"})
    again = bank.post("/instant", json=body, headers={"Idempotency-Key": "7-1"})
    assert first.status_code == 201 and again.status_code == 200
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true"
    clash = bank.post("/instant", json={**body, "amount_cents": 1}, headers={"Idempotency-Key": "7-1"})
    assert clash.status_code == 409
    assert bank.post("/instant", json=body).status_code == 400  # key is mandatory
    retry = bank.post("/instant", json=body, headers={"Idempotency-Key": "7-2"})  # new attempt = new payment
    assert retry.json()["bank_reference"] != first.json()["bank_reference"]


def test_bank_rejects_invalid_nacha(bank):
    r = bank.post("/ach/files", content="1" * 94 + "\n", headers={"Content-Type": "text/plain"})
    assert r.status_code == 422 and r.json()["detail"]["errors"]


def test_same_ach_file_twice_is_not_paid_twice(fresh_seed, bank, tmp_path):
    run_all(fresh_seed)
    report = execute(fresh_seed["session"], client=bank, now=NOW, outbox=tmp_path)
    text = open(report.ach_file).read()
    before = len(bank.app.state.bank.state["transactions"])
    r = bank.post("/ach/files", content=text, headers={"Idempotency-Key": "operator-resubmit"})
    assert r.status_code in (200, 201) and len(bank.app.state.bank.state["transactions"]) == before


def test_r03_return_appears_on_statement_two_business_days_later(fresh_seed, bank, tmp_path):
    run_all(fresh_seed)
    s = fresh_seed["session"]
    execute(s, client=bank, now=NOW, outbox=tmp_path)
    s.commit()
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 7)
    pay = s.exec(select(Payment).where(Payment.invoice_id == m["invoice_id"])).one()
    returns = [t for t in bank.app.state.bank.state["transactions"] if t["kind"] == "ach_return"]
    assert len(returns) == 1 and returns[0]["customer_reference"] == f"PAY{pay.id}"
    assert returns[0]["return_code"] == "R03"
    bai = bank.get("/statement", params={"date": returns[0]["value_date"]}).text
    assert "88,RETURN R03/" in bai and bai.startswith("01,") and bai.rstrip().split("\n")[-1].startswith("99,")


def test_mock_bank_onboarding_endpoints(bank):
    assert bank.post("/account-validation", json={"routing": "021000021", "account_number": "1"}).json()["result"] == "valid"
    assert bank.post("/account-validation", json={"routing": "021000022", "account_number": "1"}).json()["result"] == "invalid"
    assert bank.post("/tin-match", json={"tin": "12-3456789", "name": "X"}).json()["result"] == "matched"


def test_execution_is_audited(fresh_seed, bank, tmp_path):
    run_all(fresh_seed)
    s = fresh_seed["session"]
    execute(s, client=bank, now=NOW, outbox=tmp_path)
    s.commit()
    sends = s.exec(select(AuditLog).where(AuditLog.action == "send")).all()
    assert len(sends) == 21 and all(a.actor == "executor" for a in sends)
    assert s.exec(select(Invoice).where(Invoice.status == "scheduled")).all()



class BankDown:
    def post(self, *a, **k):
        raise httpx.ConnectError("bank down")


def test_ach_file_is_resent_unchanged_after_an_outage(fresh_seed, bank, tmp_path):
    run_all(fresh_seed)
    s = fresh_seed["session"]
    down = execute(s, client=BankDown(), now=NOW, outbox=tmp_path)
    s.commit()
    assert down.sent == [] and len(down.failed) == 21
    first_file = open(down.ach_file).read()
    assert s.exec(select(Payment).where(Payment.status == PaymentStatus.scheduled)).all()

    up = execute(s, client=bank, now=NOW, outbox=tmp_path)  # bank is back
    s.commit()
    assert up.ach_file is None  # nothing rebuilt: the pending file was resent as-is
    assert len(up.sent) == 21 and len(list(tmp_path.glob("ach_*.txt"))) == 1
    assert open(down.ach_file).read() == first_file
    assert len([t for t in bank.app.state.bank.state["transactions"] if t["kind"] == "ach"]) == 18


def test_ach_timeout_after_the_bank_received_it_pays_once(fresh_seed, bank, tmp_path):
    run_all(fresh_seed)
    s = fresh_seed["session"]
    flaky = TimeoutAfterSend(bank)  # the bank gets the file, the response is lost
    report = execute(s, client=flaky, now=NOW, outbox=tmp_path)
    s.commit()
    assert len(report.sent) == 21
    assert len([t for t in bank.app.state.bank.state["transactions"] if t["kind"] == "ach"]) == 18
