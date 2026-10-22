"""Approval policy (SPEC §6.4). Pure and deterministic; thresholds from config/policy.yaml.

Precedence, strictest first:
  1. hard_blocks signal                      -> BLOCK
  2. hold_until_callback signal, unverified  -> HOLD_CALLBACK
  3. dual-control triggers (amount / rail)   -> DUAL_APPROVAL
  4. every auto-pay condition met            -> AUTO_APPROVE
  5. otherwise                               -> SINGLE_APPROVAL (with the reasons auto-pay failed)

The rail is optional: before rail selection the rail-dependent rules are skipped, and
the caller re-runs decide() with the chosen rail so a wire or large instant payment
can only escalate the decision, never relax it.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from app.config import policy_config

INSTANT_RAILS = {"rtp", "fednow"}


class Outcome(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    SINGLE_APPROVAL = "SINGLE_APPROVAL"
    DUAL_APPROVAL = "DUAL_APPROVAL"
    HOLD_CALLBACK = "HOLD_CALLBACK"
    BLOCK = "BLOCK"


class PolicyInput(BaseModel):
    amount_cents: int | None
    vendor_trusted: bool
    vendor_flagged: bool = False
    match_status: str | None  # full / partial / none
    risk_score: int
    signals: list[str]
    intake_needs_review: bool = False
    callback_verified: bool = False
    rail: str | None = None


class Decision(BaseModel):
    outcome: Outcome
    reasons: list[str]  # human-readable, each naming the rule that fired

    @property
    def needs_human(self) -> bool:
        return self.outcome in (Outcome.SINGLE_APPROVAL, Outcome.DUAL_APPROVAL, Outcome.HOLD_CALLBACK)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def decide(inp: PolicyInput) -> Decision:
    p = policy_config()
    signals = set(inp.signals)

    blocks = sorted(signals & set(p["hard_blocks"]))
    if blocks:
        return Decision(outcome=Outcome.BLOCK, reasons=[f"hard_blocks: {s}" for s in blocks])

    holds = sorted(signals & set(p["hold_until_callback"]))
    if holds and not inp.callback_verified:
        return Decision(outcome=Outcome.HOLD_CALLBACK,
                        reasons=[f"hold_until_callback: {s} (call the vendor on a known number before paying)"
                                 for s in holds])

    dual = p["dual_approval"]
    dual_reasons = []
    if inp.amount_cents is not None and inp.amount_cents >= dual["min_amount_cents"]:
        dual_reasons.append(f"dual_approval.min_amount_cents: {_money(inp.amount_cents)} >= "
                            f"{_money(dual['min_amount_cents'])}")
    if inp.rail in dual["rails_always_dual"]:
        dual_reasons.append(f"dual_approval.rails_always_dual: {inp.rail}")
    if (inp.rail in INSTANT_RAILS and inp.amount_cents is not None
            and inp.amount_cents >= dual["instant_min_amount_cents"]):
        dual_reasons.append(f"dual_approval.instant_min_amount_cents: {inp.rail} payment of "
                            f"{_money(inp.amount_cents)} is irrevocable")
    if dual_reasons:
        return Decision(outcome=Outcome.DUAL_APPROVAL, reasons=dual_reasons)

    auto = p["auto_pay"]
    blockers = []
    if inp.intake_needs_review:
        blockers.append("intake: extraction needs human review")
    if inp.amount_cents is None:
        blockers.append("auto_pay: amount unknown")
    elif inp.amount_cents > auto["max_amount_cents"]:
        blockers.append(f"auto_pay.max_amount_cents: {_money(inp.amount_cents)} > {_money(auto['max_amount_cents'])}")
    if auto.get("block_flagged_vendors") and inp.vendor_flagged:
        blockers.append("auto_pay.block_flagged_vendors: vendor is flagged")
    if auto["require_trusted_vendor"] and not inp.vendor_trusted:
        blockers.append("auto_pay.require_trusted_vendor: vendor is not trusted yet")
    if auto["require_full_match"] and inp.match_status != "full":
        blockers.append(f"auto_pay.require_full_match: 3-way match is {inp.match_status or 'missing'}")
    if inp.risk_score > auto["max_risk_score"]:
        blockers.append(f"auto_pay.max_risk_score: risk {inp.risk_score} > {auto['max_risk_score']}")
    for s in sorted(signals & set(p["force_review"])):
        blockers.append(f"force_review: {s}")

    if not blockers:
        return Decision(outcome=Outcome.AUTO_APPROVE,
                        reasons=["auto_pay: trusted vendor, full 3-way match, low risk, within limit"])
    return Decision(outcome=Outcome.SINGLE_APPROVAL, reasons=blockers)


class ApprovalRecord(BaseModel):
    approver: str
    approved: bool


def release_allowed(outcome: Outcome | str, approvals: list[ApprovalRecord]) -> tuple[bool, str]:
    """May a payment with this decision be sent, given the human approvals recorded so far?"""
    outcome = Outcome(outcome)
    if outcome == Outcome.AUTO_APPROVE:
        return True, "auto-approved by policy"
    if outcome in (Outcome.BLOCK, Outcome.HOLD_CALLBACK):
        return False, f"{outcome.value}: payment may not be released"
    if any(not a.approved for a in approvals):
        return False, "rejected by an approver"
    approvers = {a.approver for a in approvals if a.approved}
    need = 2 if outcome == Outcome.DUAL_APPROVAL else 1
    if len(approvers) >= need:
        return True, f"{len(approvers)} distinct approver(s): {', '.join(sorted(approvers))}"
    return False, f"{outcome.value}: {len(approvers)}/{need} distinct approvers so far"
