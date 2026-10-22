"""Phase 3 acceptance: every demo case gets its expected signals and decision.

Runs the real match -> fraud -> decision pipeline on a seeded DB, with extraction
taken from ground truth (the LLM is exercised separately in the evals).
"""

import json
from datetime import date

import pytest
from sqlmodel import select

from app.agents.decision import run_decision
from app.agents.fraud import run_fraud
from app.agents.matcher import run_match
from app.models import AuditLog, BankChangeRequest, ChangeRequestStatus, Invoice, InvoiceStatus
from tests.fakes import FakeLLM, extract_truth
from tests.test_matcher import no_embedder

RUN_DATE = date(2026, 10, 19)
BEC_QUOTE = "We need this invoice paid today - our crews cannot be dispatched to your properties"
HVAC_QUOTE = "Payment due on receipt."


def pipeline(fresh_seed, m, pressure=None, rail=None, llm=None):
    s = fresh_seed["session"]
    extract_truth(fresh_seed, m, pressure_language=pressure)
    run_match(s, m["invoice_id"], embedder=no_embedder)
    fraud = run_fraud(s, m["invoice_id"], today=RUN_DATE, data_dir=fresh_seed["data_dir"], llm=llm,
                      with_rationale=llm is not None)
    decision = run_decision(s, m["invoice_id"], rail=rail)
    s.commit()
    return fraud, decision


def pressure_for(m):
    return {"required": BEC_QUOTE}.get(m["expected"].get("urgency"))


def test_every_demo_case_gets_expected_decision_and_signals(fresh_seed):
    for m in fresh_seed["manifest"]:
        fraud, decision = pipeline(fresh_seed, m, pressure=pressure_for(m))
        codes = {sig.code for sig in fraud.signals} - {"URGENCY_LANGUAGE"}
        assert codes == set(m["expected"]["signals"]), (m["file"], fraud.signals)
        assert decision.outcome.value == m["expected"]["decision"], (m["file"], decision.reasons)


def test_routine_invoices_are_clean(fresh_seed):
    for m in (m for m in fresh_seed["manifest"] if m["demo_case"] in (1, 6, 7, 8)):
        fraud, decision = pipeline(fresh_seed, m)
        assert fraud.signals == [] and fraud.risk_score == 0, m["file"]


@pytest.mark.parametrize("pressure", [None, HVAC_QUOTE])
def test_case4_auto_with_or_without_urgency(fresh_seed, pressure):
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 4)
    fraud, decision = pipeline(fresh_seed, m, pressure=pressure)
    assert decision.outcome.value == "AUTO_APPROVE"
    assert fraud.risk_score == (10 if pressure else 0)


def test_case4_on_rtp_stays_auto_but_case5_on_wire_is_dual(fresh_seed):
    m4 = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 4)
    assert pipeline(fresh_seed, m4, rail="rtp")[1].outcome.value == "AUTO_APPROVE"  # $3,800 < $25k instant limit
    m5 = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 5)
    reasons = pipeline(fresh_seed, m5, rail="wire")[1].reasons
    assert any("rails_always_dual: wire" in r for r in reasons) and any("min_amount_cents" in r for r in reasons)


def test_case2_bec_opens_callback_task_and_releases_after_verification(fresh_seed):
    s = fresh_seed["session"]
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 2)
    fraud, decision = pipeline(fresh_seed, m, pressure=BEC_QUOTE)
    assert decision.outcome.value == "HOLD_CALLBACK"
    assert "URGENCY_LANGUAGE" in {x.code for x in fraud.signals}
    assert fraud.risk_score == 100  # 40 + 40 + 35 + 10, capped
    req = s.exec(select(BankChangeRequest)).one()
    assert req.status == ChangeRequestStatus.pending_callback
    assert (req.new_routing, req.new_account_last4) == (m["expected"]["new_bank"]["routing"],
                                                        m["expected"]["new_bank"]["account_last4"])
    assert s.get(Invoice, m["invoice_id"]).status == InvoiceStatus.on_hold

    # A human verifies by phone: the hold lifts, but risk 100 still means a person approves.
    req.status, req.callback_done_by = ChangeRequestStatus.verified, "ap.manager"
    s.commit()
    again = run_decision(s, m["invoice_id"])
    assert again.outcome.value == "SINGLE_APPROVAL"
    assert any("max_risk_score" in r for r in again.reasons)


def test_case3_duplicate_reviewed_not_blocked(fresh_seed):
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 3)
    fraud, decision = pipeline(fresh_seed, m)
    assert decision.outcome.value == "SINGLE_APPROVAL"
    assert "force_review: DUPLICATE_INVOICE" in decision.reasons
    assert "INV-1043" in fraud.signals[0].evidence


def test_case9_ofac_blocks(fresh_seed):
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 9)
    fraud, decision = pipeline(fresh_seed, m)
    assert decision.outcome.value == "BLOCK" and decision.reasons == ["hard_blocks: OFAC_HIT"]
    assert "MERIDIAN SUPPLY AND LOGISTICS FZE" in next(x for x in fraud.signals if x.code == "OFAC_HIT").evidence


def test_rationale_written_only_when_signals(fresh_seed):
    s = fresh_seed["session"]
    m2 = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 2)
    reply = json.dumps({"summary": "Looks like vendor impersonation.", "concerns": ["new bank"],
                        "recommended_actions": ["call the vendor on the number on file"]})
    fraud, _ = pipeline(fresh_seed, m2, pressure=BEC_QUOTE, llm=FakeLLM([reply]))
    assert fraud.rationale.summary.startswith("Looks like")
    assert json.loads(s.get(Invoice, m2["invoice_id"]).fraud_rationale_json)["concerns"] == ["new bank"]

    m1 = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 1)
    fraud, _ = pipeline(fresh_seed, m1, llm=FakeLLM([]))  # would raise if called
    assert fraud.rationale is None


def test_rationale_failure_does_not_change_decision(fresh_seed):
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 9)
    fraud, decision = pipeline(fresh_seed, m, llm=FakeLLM(["garbage", "garbage"]))
    assert fraud.rationale is None and fraud.rationale_error
    assert decision.outcome.value == "BLOCK"


def test_each_stage_is_audited(fresh_seed):
    s = fresh_seed["session"]
    m = next(m for m in fresh_seed["manifest"] if m["demo_case"] == 2)
    pipeline(fresh_seed, m, pressure=BEC_QUOTE)
    actions = [a.action for a in s.exec(select(AuditLog).where(AuditLog.actor != "seed_script")).all()]
    assert actions == ["extract", "three_way_match", "fraud_screen", "open_callback", "decide"]
