"""LangGraph wiring (SPEC §7).

One graph thread per invoice (thread_id "invoice-<id>"), checkpointed in SQLite so a pause
for a human survives restarts:

  intake -> match -> fraud -> decide --BLOCK--------------------------------> finish
                                     --HOLD_CALLBACK--> callback (interrupt) -> decide / finish
                                     --otherwise------> plan --AUTO_APPROVE--> finish
                                                             --needs approval-> approval (interrupt, loops) -> finish

The run is a fan-out/join: run_ap() pushes every received invoice through its own thread,
then execute_released() sends every released payment in one go (one NACHA file, etc.).
Resuming an approval later releases more payments, and execute_released() runs again.

Nodes never hold DB sessions across steps; the DB is the source of truth and the graph
state only carries ids and summaries. Code before interrupt() is read-only because
LangGraph re-runs the node from the top on resume.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypedDict

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlmodel import Session, select

from app import audit
from app.agents import decision as decision_agent
from app.agents import executor, fraud, intake, matcher, rail_optimizer, reconciler
from app.config import get_settings
from app.llm import ChatModel
from app.models import (
    AccountType,
    Approval,
    ApprovalDecision,
    ApprovalLevel,
    BankChangeRequest,
    BankAccountSource,
    ChangeRequestStatus,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    VendorBankAccount,
    utcnow,
)
from app.payments import vault
from app.payments.routing import is_valid_aba
from app.rules.policy import ApprovalRecord, Outcome, release_allowed

ROUTINE_RAILS = {"ach_standard"}


@dataclass
class Deps:
    engine: Engine
    data_dir: Path
    now: datetime
    bank: httpx.Client | Any
    outbox: Path
    llm: ChatModel | None = None  # None -> Ollama
    embedder: matcher.Embedder | None = None  # None -> Ollama embeddings
    rail_explanations: str = "exceptions"  # all | exceptions | none
    max_steps: int = field(default_factory=lambda: get_settings().max_agent_steps)


class InvoiceState(TypedDict, total=False):
    invoice_id: int
    status: str
    decision: str
    risk_score: int
    signals: list[str]
    rail: str | None
    payment_id: int | None
    approvals: list[dict]
    human_rounds: int  # interrupts answered for this invoice; capped by MAX_AGENT_STEPS
    note: str | None
    errors: list[str]


def thread(invoice_id: int) -> dict:
    return {"configurable": {"thread_id": f"invoice-{invoice_id}"}}


# --- graph ---------------------------------------------------------------------------------


def build_graph(deps: Deps, checkpointer):
    def session() -> Session:
        return Session(deps.engine)

    def n_intake(st: InvoiceState) -> InvoiceState:
        with session() as s:
            r = intake.process_invoice(s, st["invoice_id"], deps.data_dir, llm=deps.llm)
            s.commit()
        return {"status": "needs_review" if r.needs_review else "extracted", "human_rounds": 0, "errors": [],
                "approvals": []}

    def n_match(st: InvoiceState) -> InvoiceState:
        with session() as s:
            matcher.run_match(s, st["invoice_id"], embedder=deps.embedder)
            s.commit()
        return {}

    def n_fraud(st: InvoiceState) -> InvoiceState:
        with session() as s:
            r = fraud.run_fraud(s, st["invoice_id"], today=deps.now.date(), data_dir=deps.data_dir, llm=deps.llm)
            s.commit()
        errors = st.get("errors", []) + ([f"rationale unavailable: {r.rationale_error}"] if r.rationale_error else [])
        return {"risk_score": r.risk_score, "signals": [x.code for x in r.signals], "errors": errors}

    def n_decide(st: InvoiceState) -> InvoiceState:
        with session() as s:
            d = decision_agent.run_decision(s, st["invoice_id"])
            s.commit()
        return {"decision": d.outcome.value}

    def explain_when(plan, outcome: Outcome) -> bool:
        if deps.rail_explanations == "all":
            return True
        if deps.rail_explanations == "none":
            return False
        return outcome != Outcome.AUTO_APPROVE or plan.rail not in ROUTINE_RAILS  # only the unusual ones

    def n_plan(st: InvoiceState) -> InvoiceState:
        with session() as s:
            r = rail_optimizer.plan_payment(s, st["invoice_id"], now=deps.now, llm=deps.llm,
                                            with_explanation=explain_when)
            s.commit()
        status = "scheduled" if r.outcome == Outcome.AUTO_APPROVE and r.payment_id else (
            "awaiting_approval" if r.payment_id else "needs_review")
        return {"decision": r.outcome.value, "rail": r.plan.rail, "payment_id": r.payment_id, "status": status}

    def n_callback(st: InvoiceState) -> Command:
        with session() as s:
            req = s.exec(select(BankChangeRequest).where(BankChangeRequest.invoice_id == st["invoice_id"])).first()
            payload = {"kind": "callback", "invoice_id": st["invoice_id"], **_summary(s, st["invoice_id"]),
                       "bank_change_request_id": req.id if req else None,
                       "new_routing": req.new_routing if req else None,
                       "new_account_last4": req.new_account_last4 if req else None,
                       "instructions": "Call the vendor on the phone number already on file (never one from the "
                                       "email or invoice) and confirm the new bank details."}
        answer = interrupt(payload)
        rounds = st.get("human_rounds", 0) + 1
        verified, who = bool(answer.get("verified")), (answer.get("by") or "").strip()
        account = (answer.get("account_number") or "").strip()
        problem = None
        if not who:
            problem = "the person who made the call-back must be named"
        elif verified and (not account.isdigit() or not req or account[-4:] != req.new_account_last4):
            problem = "confirmed account number must be given and end in the requested last 4 digits"
        if problem:
            if rounds >= deps.max_steps:
                return Command(goto="finish", update={"human_rounds": rounds, "status": "escalated", "note": problem})
            return Command(goto="callback", update={"human_rounds": rounds, "note": problem})

        with session() as s:
            req = s.get(BankChangeRequest, req.id)
            inv = s.get(Invoice, st["invoice_id"])
            before = audit.snapshot(req)
            req.status = ChangeRequestStatus.verified if verified else ChangeRequestStatus.rejected
            req.callback_done_by = who
            audit.record(s, actor=who, entity="bank_change_requests", entity_id=req.id,
                         action="callback_verified" if verified else "callback_rejected", before=before, after=req,
                         reason=answer.get("note") or ("vendor confirmed the change" if verified
                                                       else "vendor did not request this change"))
            if verified:
                _switch_account(s, req, account, who, deps.now)
            else:
                before = audit.snapshot(inv)
                inv.status, inv.decision = InvoiceStatus.blocked, Outcome.BLOCK.value
                audit.record(s, actor=who, entity="invoices", entity_id=inv.id, action="block", before=before,
                             after=inv, reason="bank change not confirmed by call-back: suspected vendor impersonation")
            s.commit()
        if verified:
            return Command(goto="decide", update={"human_rounds": rounds, "note": None})
        return Command(goto="finish", update={"human_rounds": rounds, "status": "blocked", "decision": "BLOCK"})

    def n_approval(st: InvoiceState) -> Command:
        with session() as s:
            pay = s.get(Payment, st["payment_id"])
            inv = s.get(Invoice, st["invoice_id"])
            payload = {"kind": "approval", "invoice_id": inv.id, "payment_id": pay.id, **_summary(s, inv.id),
                       "required_level": "dual" if inv.decision == Outcome.DUAL_APPROVAL.value else "single",
                       "approvals_so_far": st.get("approvals", []), "note": st.get("note")}
        answer = interrupt(payload)
        rounds = st.get("human_rounds", 0) + 1
        who, choice = (answer.get("approver") or "").strip(), answer.get("decision")
        approvals = list(st.get("approvals", []))
        problem = None
        if not who:
            problem = "approver must be named"
        elif choice not in ("approve", "reject"):
            problem = "decision must be 'approve' or 'reject'"
        elif choice == "approve" and any(a["approver"] == who for a in approvals):
            problem = f"{who} has already approved; dual control needs a different person"
        if problem:
            if rounds >= deps.max_steps:
                return Command(goto="finish", update={"human_rounds": rounds, "status": "escalated", "note": problem})
            return Command(goto="approval", update={"human_rounds": rounds, "note": problem})

        approvals.append({"approver": who, "approved": choice == "approve", "reason": answer.get("reason")})
        with session() as s:
            pay = s.get(Payment, st["payment_id"])
            inv = s.get(Invoice, st["invoice_id"])
            row = s.exec(select(Approval).where(Approval.payment_id == pay.id)).first() or Approval(
                payment_id=pay.id, required_level=ApprovalLevel.dual
                if inv.decision == Outcome.DUAL_APPROVAL.value else ApprovalLevel.single)
            before = audit.snapshot(row) if row.id else None
            if row.approver_1 is None:
                row.approver_1 = who
            else:
                row.approver_2 = who
            row.reason = answer.get("reason")
            ok, why = release_allowed(inv.decision, [ApprovalRecord(**{k: a[k] for k in ("approver", "approved")})
                                                     for a in approvals])
            if choice == "reject":
                row.decision = ApprovalDecision.rejected
            elif ok:
                row.decision = ApprovalDecision.approved
            row.decided_at = utcnow() if (ok or choice == "reject") else None  # real time of the human act
            s.add(row)
            s.flush()
            audit.record(s, actor=who, entity="approvals", entity_id=row.id, action=choice, before=before, after=row,
                         reason=answer.get("reason") or why)
            if choice == "reject" or ok:
                before = audit.snapshot(pay)
                pay.status = PaymentStatus.scheduled if ok and choice == "approve" else PaymentStatus.blocked
                inv.status = InvoiceStatus.approved if pay.status == PaymentStatus.scheduled else InvoiceStatus.blocked
                audit.record(s, actor="rules_engine", entity="payments", entity_id=pay.id,
                             action="release" if pay.status == PaymentStatus.scheduled else "reject",
                             before=before, after=pay, reason=why if ok else f"rejected by {who}")
            s.commit()
        if choice == "reject":
            return Command(goto="finish", update={"approvals": approvals, "human_rounds": rounds, "status": "rejected"})
        if ok:
            return Command(goto="finish", update={"approvals": approvals, "human_rounds": rounds,
                                                  "status": "approved", "note": None})
        return Command(goto="approval", update={"approvals": approvals, "human_rounds": rounds,
                                                "note": f"{why}; waiting for a second approver"})

    def n_finish(st: InvoiceState) -> InvoiceState:
        with session() as s:
            inv = s.get(Invoice, st["invoice_id"])
            return {"status": st.get("status") if st.get("status") in ("escalated", "rejected") else inv.status.value}

    def after_decide(st: InvoiceState) -> str:
        return {"BLOCK": "finish", "HOLD_CALLBACK": "callback"}.get(st["decision"], "plan")

    def after_plan(st: InvoiceState) -> str:
        if st.get("payment_id") is None:
            return "finish"
        return "finish" if st["decision"] == Outcome.AUTO_APPROVE.value else "approval"

    g = StateGraph(InvoiceState)
    for name, fn in [("intake", n_intake), ("match", n_match), ("fraud", n_fraud), ("decide", n_decide),
                     ("plan", n_plan), ("finish", n_finish)]:
        g.add_node(name, fn)
    g.add_node("callback", n_callback, destinations=("decide", "finish", "callback"))
    g.add_node("approval", n_approval, destinations=("finish", "approval"))
    g.add_edge(START, "intake")
    g.add_edge("intake", "match")
    g.add_edge("match", "fraud")
    g.add_edge("fraud", "decide")
    g.add_conditional_edges("decide", after_decide, ["finish", "callback", "plan"])
    g.add_conditional_edges("plan", after_plan, ["finish", "approval"])
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)


def _summary(s: Session, invoice_id: int) -> dict:
    """What a human needs to decide: amounts, signals with evidence, rationale, rule reasons, rail."""
    inv = s.get(Invoice, invoice_id)
    pay = s.exec(select(Payment).where(Payment.invoice_id == invoice_id)).first()
    ext = json.loads(inv.extracted_json) if inv.extracted_json else {}
    return {
        "vendor": ext.get("vendor_name"), "invoice_number": inv.invoice_number, "amount_cents": inv.amount_cents,
        "due_date": inv.due_date.isoformat() if inv.due_date else None, "decision": inv.decision,
        "decision_reasons": json.loads(inv.decision_reasons_json or "[]"), "risk_score": inv.risk_score,
        "signals": json.loads(inv.signals_json or "[]"),
        "rationale": json.loads(inv.fraud_rationale_json) if inv.fraud_rationale_json else None,
        "intake_issues": [i for i in json.loads(inv.intake_issues_json or "[]") if i["blocking"]],
        "rail": pay.rail.value if pay else None, "pay_amount_cents": pay.amount_cents if pay else None,
        "send_date": pay.scheduled_date.isoformat() if pay else None,
        "rail_explanation": pay.rail_explanation if pay else None,
    }


def _switch_account(s: Session, req: BankChangeRequest, account_number: str, who: str, now: datetime) -> None:
    """A verified change becomes the vendor's active account (the old one is retired)."""
    old = s.exec(select(VendorBankAccount).where(VendorBankAccount.vendor_id == req.vendor_id,
                                                 VendorBankAccount.active)).first()
    if not is_valid_aba(req.new_routing):
        raise ValueError("verified routing number fails the ABA checksum")
    new = VendorBankAccount(vendor_id=req.vendor_id, routing=req.new_routing, account_last4=account_number[-4:],
                            account_hash=vault.tokenize(s, account_number), account_type=AccountType.checking,
                            validated=True, active=True, effective_from=now.date(),
                            source=BankAccountSource.change_request)
    req.new_account_hash = new.account_hash
    if old:
        before = audit.snapshot(old)
        old.active = False
        audit.record(s, actor=who, entity="vendor_bank_accounts", entity_id=old.id, action="deactivate",
                     before=before, after=old, reason=f"replaced via verified change request {req.id}")
    s.add(new)
    s.flush()
    audit.record(s, actor=who, entity="vendor_bank_accounts", entity_id=new.id, action="activate", after=new,
                 reason=f"verified by call-back (change request {req.id})")


# --- run-level orchestration --------------------------------------------------------------------


class InvoiceOutcome(BaseModel):
    invoice_id: int
    status: str
    decision: str | None = None
    waiting_for: str | None = None  # approval | callback
    note: str | None = None


class RunSummary(BaseModel):
    outcomes: list[InvoiceOutcome]
    execution: executor.ExecutionReport | None = None


def open_checkpointer(path: Path) -> SqliteSaver:
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def outcome_of(graph, invoice_id: int) -> InvoiceOutcome:
    snap = graph.get_state(thread(invoice_id))
    st = snap.values
    waiting = None
    for task in snap.tasks:
        for intr in task.interrupts:
            waiting = intr.value.get("kind")
    return InvoiceOutcome(invoice_id=invoice_id, status="waiting" if waiting else st.get("status", "?"),
                          decision=st.get("decision"), waiting_for=waiting, note=st.get("note"))


def pending_actions(graph, invoice_ids: list[int]) -> list[dict]:
    """Interrupt payloads currently waiting for a human (for the approvals inbox)."""
    out = []
    for iid in invoice_ids:
        for task in graph.get_state(thread(iid)).tasks:
            out += [i.value for i in task.interrupts]
    return out


def execute_released(deps: Deps) -> executor.ExecutionReport:
    with Session(deps.engine) as s:
        report = executor.execute(s, client=deps.bank, now=deps.now, outbox=deps.outbox)
        s.commit()
    return report


def run_ap(deps: Deps, graph, invoice_ids: list[int] | None = None,
           on_progress: Callable[[int, str], None] | None = None) -> RunSummary:
    """Fan out: every received invoice through its own thread. Join: one execution pass."""
    if invoice_ids is None:
        with Session(deps.engine) as s:
            invoice_ids = [i.id for i in s.exec(select(Invoice).where(Invoice.status == InvoiceStatus.received)
                                                 .order_by(Invoice.id)).all()]
    outcomes = []
    for iid in invoice_ids:
        for update in graph.stream({"invoice_id": iid}, thread(iid), stream_mode="updates"):
            if on_progress:
                for node in update:
                    on_progress(iid, node)
        outcomes.append(outcome_of(graph, iid))
    return RunSummary(outcomes=outcomes, execution=execute_released(deps))


def resume(deps: Deps, graph, invoice_id: int, answer: dict, execute: bool = True) -> RunSummary:
    """Answer a pending approval/call-back, then send anything it released."""
    graph.invoke(Command(resume=answer), thread(invoice_id))
    return RunSummary(outcomes=[outcome_of(graph, invoice_id)],
                      execution=execute_released(deps) if execute else None)


def reconcile_through(deps: Deps, end: date, start: date | None = None) -> reconciler.ReconReport:
    """Step 7: pull bank statements from the run date through `end` and reconcile them."""
    with Session(deps.engine) as s:
        report = reconciler.reconcile(s, client=deps.bank, start=start or deps.now.date(), end=end, llm=deps.llm)
        s.commit()
    return report
