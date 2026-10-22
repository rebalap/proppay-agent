"""Phase 5 acceptance: the end-to-end run pauses for approvals and resumes (also across a restart)."""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlmodel import Session, select

from app import graph as G
from app.models import (
    AuditLog,
    BankChangeRequest,
    ChangeRequestStatus,
    Invoice,
    Payment,
    PaymentStatus,
    VendorBankAccount,
)
from mock_bank.main import create_app
from tests.fakes import extraction_json
from tests.test_decisions import BEC_QUOTE, HVAC_QUOTE
from tests.test_matcher import no_embedder

NOW = datetime(2026, 10, 19, 9, 0)


class TruthLLM:
    """Stands in for Ollama: answers extraction from the manifest ground truth, and writes stub prose."""

    def __init__(self, manifest):
        self.by_number = sorted(((m["truth"]["invoice_number"], m) for m in manifest), key=lambda x: -len(x[0]))
        self.calls = {"extract": 0, "rationale": 0, "rail": 0, "notice": 0}

    def invoke(self, messages, **_):
        system, user = messages[0].content, messages[1].content
        if "intake clerk" in system:
            self.calls["extract"] += 1
            m = next(m for n, m in self.by_number if n in user)
            pressure = {2: BEC_QUOTE, 4: HVAC_QUOTE}.get(m["demo_case"])
            return AIMessage(extraction_json({**m["truth"], "pressure_language": pressure}))
        if "fraud analyst" in system:
            self.calls["rationale"] += 1
            return AIMessage(json.dumps({"summary": "Risky.", "concerns": ["see signals"],
                                         "recommended_actions": ["call the vendor on the number on file"]}))
        if "returned by their bank" in system:
            self.calls["notice"] += 1
            return AIMessage(json.dumps({"subject": "Returned payment", "body": "Please confirm your bank details "
                                         "through the vendor portal. Accounts Payable, Bluestem Residential"}))
        self.calls["rail"] += 1
        return AIMessage(json.dumps({"explanation": "Cheapest rail that settles on time."}))


@pytest.fixture
def env(fresh_seed, tmp_path):
    engine = fresh_seed["session"].get_bind()
    bank_app = create_app(fresh_seed["data_dir"] / "mock_bank_scenarios.json", None)
    with TestClient(bank_app) as bank:
        llm = TruthLLM(fresh_seed["manifest"])
        deps = G.Deps(engine=engine, data_dir=fresh_seed["data_dir"], now=NOW, bank=bank, outbox=tmp_path / "out",
                      llm=llm, embedder=no_embedder)
        cp_path = tmp_path / "checkpoints.db"
        graph = G.build_graph(deps, G.open_checkpointer(cp_path))
        by_case = {}
        for m in fresh_seed["manifest"]:
            by_case.setdefault(m["demo_case"], []).append(m["invoice_id"])
        yield {"deps": deps, "graph": graph, "bank": bank, "engine": engine, "llm": llm, "cp_path": cp_path,
               "case": by_case, "manifest": fresh_seed["manifest"]}


def run(env):
    return G.run_ap(env["deps"], env["graph"])


def outcome(summary, invoice_id):
    return next(o for o in summary.outcomes if o.invoice_id == invoice_id)


def bank_txns(env, kind=None):
    return [t for t in env["bank"].app.state.bank.state["transactions"] if kind in (None, t["kind"])]


# --- the run ---------------------------------------------------------------------------------------


def test_run_pauses_exactly_where_humans_are_needed(env):
    summary = run(env)
    waiting = {o.invoice_id: o.waiting_for for o in summary.outcomes if o.waiting_for}
    (c2,), (c3,), (c5,) = env["case"][2], env["case"][3], env["case"][5]
    assert waiting == {c2: "callback", c3: "approval", c5: "approval"}
    assert outcome(summary, env["case"][9][0]).status == "blocked"
    auto = [o for o in summary.outcomes if o.decision == "AUTO_APPROVE"]
    assert len(auto) == 21 and all(o.status == "approved" for o in auto)  # graph done; join step sends
    # join step: everything auto-approved went to the bank in one pass
    assert len(summary.execution.sent) == 21 and summary.execution.ach_file
    assert env["llm"].calls == {"extract": 25, "rationale": 4, "rail": 5, "notice": 0}  # rail prose only for the unusual ones


def test_pending_actions_carry_what_a_reviewer_needs(env):
    run(env)
    actions = {a["invoice_id"]: a for a in G.pending_actions(env["graph"], [i for c in env["case"].values() for i in c])}
    gc = actions[env["case"][5][0]]
    assert gc["required_level"] == "dual" and gc["rail"] == "wire" and gc["amount_cents"] == 48_000_000
    assert any("rails_always_dual" in r for r in gc["decision_reasons"]) and gc["rail_explanation"]
    bec = actions[env["case"][2][0]]
    assert bec["kind"] == "callback" and {s["code"] for s in bec["signals"]} >= {"BANK_DETAILS_CHANGED",
                                                                                 "LOOKALIKE_VENDOR"}
    assert bec["rationale"]["recommended_actions"] and "phone number already on file" in bec["instructions"]


# --- approvals -----------------------------------------------------------------------------------


def test_single_approval_releases_and_executes(env):
    run(env)
    c3 = env["case"][3][0]
    r = G.resume(env["deps"], env["graph"], c3, {"approver": "alice", "decision": "approve",
                                                 "reason": "vendor confirmed a corrected resubmission"})
    assert r.outcomes[0].status == "approved" and len(r.execution.sent) == 1
    with Session(env["engine"]) as s:
        pay = s.exec(select(Payment).where(Payment.invoice_id == c3)).one()
        assert pay.status == PaymentStatus.sent and pay.batch_id
        assert s.exec(select(AuditLog).where(AuditLog.actor == "alice", AuditLog.action == "approve")).one()


def test_dual_approval_needs_two_different_people(env):
    run(env)
    c5 = env["case"][5][0]
    r = G.resume(env["deps"], env["graph"], c5, {"approver": "alice", "decision": "approve"})
    assert r.outcomes[0].waiting_for == "approval" and "second approver" in r.outcomes[0].note
    assert not bank_txns(env, "wire")

    r = G.resume(env["deps"], env["graph"], c5, {"approver": "alice", "decision": "approve"})
    assert r.outcomes[0].waiting_for == "approval" and "different person" in r.outcomes[0].note

    r = G.resume(env["deps"], env["graph"], c5, {"approver": "bob", "decision": "approve"})
    assert r.outcomes[0].status == "approved"
    (wire,) = bank_txns(env, "wire")
    assert wire["amount_cents"] == 48_000_000


def test_rejection_stops_the_payment(env):
    run(env)
    c3 = env["case"][3][0]
    r = G.resume(env["deps"], env["graph"], c3, {"approver": "alice", "decision": "reject", "reason": "duplicate"})
    assert r.outcomes[0].status == "rejected" and r.execution.sent == []
    with Session(env["engine"]) as s:
        assert s.exec(select(Payment).where(Payment.invoice_id == c3)).one().status == PaymentStatus.blocked


def test_bad_answers_are_capped_by_max_agent_steps(env):
    run(env)
    c3 = env["case"][3][0]
    for i in range(env["deps"].max_steps):
        r = G.resume(env["deps"], env["graph"], c3, {"approver": "", "decision": "approve"}, execute=False)
    assert r.outcomes[0].status == "escalated" and r.outcomes[0].waiting_for is None


# --- call-back flow (case 2) --------------------------------------------------------------------------


def test_callback_rejected_blocks_the_bec_invoice(env):
    run(env)
    c2 = env["case"][2][0]
    r = G.resume(env["deps"], env["graph"], c2, {"verified": False, "by": "dana.ap",
                                                 "note": "Acme says they never changed banks"})
    assert r.outcomes[0].status == "blocked"
    with Session(env["engine"]) as s:
        inv = s.get(Invoice, c2)
        assert inv.decision == "BLOCK" and inv.is_fraud  # fraud prevented
        assert s.exec(select(BankChangeRequest)).one().status == ChangeRequestStatus.rejected
        assert not s.exec(select(Payment).where(Payment.invoice_id == c2)).all()


def test_callback_verified_switches_account_then_still_needs_approval(env):
    run(env)
    c2 = env["case"][2][0]
    m = next(m for m in env["manifest"] if m["invoice_id"] == c2)
    full = m["doc"]["remit"]["account_full"]

    r = G.resume(env["deps"], env["graph"], c2, {"verified": True, "by": "dana.ap", "account_number": "999"},
                 execute=False)
    assert r.outcomes[0].waiting_for == "callback" and "last 4" in r.outcomes[0].note

    r = G.resume(env["deps"], env["graph"], c2, {"verified": True, "by": "dana.ap", "account_number": full},
                 execute=False)
    assert r.outcomes[0].waiting_for == "approval"  # hold lifted, but risk is still too high to auto-pay
    with Session(env["engine"]) as s:
        acct = s.exec(select(VendorBankAccount).where(VendorBankAccount.vendor_id == s.get(Invoice, c2).vendor_id,
                                                      VendorBankAccount.active)).one()
        assert acct.account_last4 == full[-4:] and acct.source.value == "change_request"

    r = G.resume(env["deps"], env["graph"], c2, {"approver": "alice", "decision": "approve"})
    assert r.outcomes[0].status == "approved"
    entries = [t for t in bank_txns(env, "ach") if t["account_last4"] == full[-4:]]
    assert len(entries) == 1  # paid to the verified new account


# --- persistence --------------------------------------------------------------------------------------


def test_approvals_survive_a_restart(env):
    run(env)
    c5 = env["case"][5][0]
    G.resume(env["deps"], env["graph"], c5, {"approver": "alice", "decision": "approve"}, execute=False)

    # "restart": new graph object, new checkpointer connection on the same file
    graph2 = G.build_graph(env["deps"], G.open_checkpointer(env["cp_path"]))
    assert G.outcome_of(graph2, c5).waiting_for == "approval"
    assert G.pending_actions(graph2, [c5])[0]["approvals_so_far"][0]["approver"] == "alice"
    r = G.resume(env["deps"], graph2, c5, {"approver": "bob", "decision": "approve"})
    assert r.outcomes[0].status == "approved" and len(bank_txns(env, "wire")) == 1


def test_rerunning_the_ap_run_does_not_reprocess_or_repay(env):
    run(env)
    before = len(bank_txns(env))
    again = run(env)  # nothing is "received" any more
    assert again.outcomes == [] and again.execution.sent == [] and len(bank_txns(env)) == before


def test_each_payment_run_writes_a_new_nacha_file(env):
    first = run(env).execution.ach_file
    first_text = open(first).read()
    second = G.resume(env["deps"], env["graph"], env["case"][3][0],
                      {"approver": "alice", "decision": "approve"}).execution.ach_file
    assert first != second and open(first).read() == first_text  # first file untouched
    assert first.endswith("_A.txt") and second.endswith("_B.txt")
    assert open(second).readline()[33] == "B"  # file ID modifier in the header
    from app.payments import nacha
    assert nacha.parse_and_validate(open(second).read())[1] == []
