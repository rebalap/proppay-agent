"""Rail & timing agent (SPEC §6.5): deterministic plan, LLM-written explanation, Payment row.

After the rail is known the policy decision is re-run with it, so a wire or a large
instant payment escalates the approval level before any money can move.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import audit
from app.agents.decision import run_decision
from app.llm import ChatModel, render_prompt, structured_call
from app.models import Invoice, Payment, PaymentStatus, Rail, Vendor
from app.rules import rails
from app.rules.policy import Outcome

ACTOR = "rail_optimizer"


class RailExplanation(BaseModel):
    explanation: str = Field(description="2-3 plain sentences for an AP manager")


class PlanResult(BaseModel):
    plan: rails.RailPlan
    outcome: Outcome
    payment_id: int | None
    explanation: str | None = None


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


RAIL_NAMES = {"ach_standard": "standard ACH bank transfer", "ach_same_day": "same-day ACH bank transfer",
              "rtp": "RTP instant payment", "fednow": "FedNow instant payment", "wire": "wire transfer",
              "virtual_card": "virtual card", "check": "paper check"}


def _day(d) -> str:
    """Dates are formatted here, never by the model (it gets weekdays wrong)."""
    from datetime import date

    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%A} {d.day} {d:%B}" if d else "-"


def _humanize(text: str) -> str:
    import re

    for code, name in RAIL_NAMES.items():
        text = text.replace(code, name)
    return re.sub(r"\d{4}-\d{2}-\d{2}", lambda m: _day(m.group(0)), text)


def explain(vendor: str, plan: rails.RailPlan, llm: ChatModel | None = None) -> str | None:
    opts = "\n".join(
        f"- {RAIL_NAMES.get(o.rail, o.rail)}: " + (f"settles {_day(o.settle_date)}, net cost {_money(o.net_cost_cents)}"
                           + ("" if o.meets_target else " (too late)") if o.eligible else f"not eligible ({o.reason})")
        for o in plan.options)
    r = structured_call(
        RailExplanation,
        system=render_prompt("rail_explain_system"),
        user=render_prompt("rail_explain_user", vendor=vendor, amount=_money(plan.pay_amount_cents),
                           rail=RAIL_NAMES.get(plan.rail, plan.rail),
                           send_date=_day(plan.send_date), settle_date=_day(plan.settle_date),
                           target_date=_day(plan.target_date),
                           fee=_money(plan.fee_cents), rebate=_money(plan.rebate_cents),
                           discount=_money(plan.discount_cents),
                           reasons="\n".join(f"- {_humanize(x)}" for x in plan.reasons),
                           options=opts),
        llm=llm,
    )
    return r.value.explanation if r.ok else None


def plan_payment(session: Session, invoice_id: int, *, now: datetime, llm: ChatModel | None = None,
                 with_explanation: bool | Callable[[rails.RailPlan, Outcome], bool] = True) -> PlanResult:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise KeyError(f"invoice {invoice_id} not found")
    if invoice.decision in (Outcome.BLOCK.value, Outcome.HOLD_CALLBACK.value) or invoice.amount_cents is None:
        return PlanResult(plan=rails.RailPlan(rail=None, send_date=None, settle_date=None, target_date=now.date(),
                                              urgent=False, on_time=False, pay_amount_cents=invoice.amount_cents or 0,
                                              reasons=[f"not planned: decision {invoice.decision}"], options=[]),
                          outcome=Outcome(invoice.decision) if invoice.decision else Outcome.SINGLE_APPROVAL,
                          payment_id=None)
    vendor = session.get(Vendor, invoice.vendor_id)
    plan = rails.plan(amount_cents=invoice.amount_cents, vendor_rails=vendor.enrolled_rails,
                      card_accepting=vendor.card_accepting, terms=invoice.terms, invoice_date=invoice.invoice_date,
                      due_date=invoice.due_date, risk_score=invoice.risk_score or 0, now=now)

    # The rail can only make the decision stricter (e.g. wire -> dual approval).
    decision = run_decision(session, invoice_id, rail=plan.rail) if plan.rail else None
    outcome = decision.outcome if decision else Outcome(invoice.decision)
    if plan.rail is None or outcome in (Outcome.BLOCK, Outcome.HOLD_CALLBACK):
        return PlanResult(plan=plan, outcome=outcome, payment_id=None)

    wanted = with_explanation(plan, outcome) if callable(with_explanation) else with_explanation
    explanation = explain(vendor.legal_name, plan, llm) if wanted else None
    payment = session.exec(select(Payment).where(Payment.invoice_id == invoice.id,
                                                 Payment.status.in_([PaymentStatus.scheduled,
                                                                     PaymentStatus.pending_approval]))).first()
    before = audit.snapshot(payment) if payment else None
    payment = payment or Payment(invoice_id=invoice.id, rail=Rail(plan.rail), amount_cents=0,
                                 scheduled_date=plan.send_date, idempotency_key=f"pending-{invoice.id}")
    payment.rail = Rail(plan.rail)
    payment.amount_cents = plan.pay_amount_cents
    payment.fee_cents = plan.fee_cents
    payment.discount_cents = plan.discount_cents
    payment.rebate_cents = plan.rebate_cents
    payment.scheduled_date = plan.send_date
    payment.expected_settle_date = plan.settle_date
    payment.status = PaymentStatus.scheduled if outcome == Outcome.AUTO_APPROVE else PaymentStatus.pending_approval
    payment.rail_plan_json = plan.model_dump_json()
    payment.rail_explanation = explanation
    session.add(payment)
    session.flush()
    payment.idempotency_key = f"{payment.id}-{payment.attempt}"
    audit.record(session, actor=ACTOR, entity="payments", entity_id=payment.id, action="plan_rail",
                 before=before, after=payment,
                 reason=f"{plan.rail} send {plan.send_date} settle {plan.settle_date}: {plan.reasons[-1]}")
    return PlanResult(plan=plan, outcome=outcome, payment_id=payment.id, explanation=explanation)
