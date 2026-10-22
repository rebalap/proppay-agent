"""Plain-language vocabulary for the UI.

The engine speaks in rule ids and codes (AUTO_APPROVE, auto_pay.max_risk_score, R03...). Every
string a person reads goes through here first. The technical form stays available for auditors.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root, whichever ui module loads first

from app.config import fraud_config, rails_config  # noqa: E402
from app.payments.rcodes import describe  # noqa: E402

# --- tones: status palette from the dataviz reference (always paired with an icon + label) -----------
TONE = {
    "good": ("#0ca30c", "#e7f6e7"),
    "warning": ("#8a5a00", "#fdf3dc"),
    "serious": ("#b4501f", "#fbeae2"),
    "critical": ("#b02a2a", "#f9e3e3"),
    "info": ("#1f5fae", "#e6f0fb"),
    "neutral": ("#52514e", "#efeeea"),
}

# --- invoice status ---------------------------------------------------------------------------------
STATUS = {
    "received": ("📥", "New", "neutral", "Arrived and waiting to be read"),
    "extracted": ("📖", "Read", "info", "The AI has read it; checks are running"),
    "needs_review": ("🔎", "Needs a human check", "warning", "The AI couldn't read it with confidence"),
    "matched": ("🧾", "Matched", "info", "Matches its purchase order and completed work"),
    "pending_approval": ("✋", "Waiting for approval", "warning", "A person needs to approve this payment"),
    "on_hold": ("📞", "On hold: call the vendor", "serious", "Bank details changed; confirm by phone first"),
    "approved": ("👍", "Approved", "good", "Cleared to pay; goes out with the next bank run"),
    "scheduled": ("🗓️", "Payment scheduled", "good", "Sent to the bank and on its way"),
    "paid": ("✅", "Paid", "good", "The bank confirmed the money arrived"),
    "blocked": ("⛔", "Stopped", "critical", "Will not be paid"),
    "reopened": ("↩️", "Payment bounced", "serious", "The vendor's bank sent the money back"),
}


def status(code: str) -> tuple[str, str, str, str]:
    return STATUS.get(code, ("•", code.replace("_", " ").title(), "neutral", ""))


# --- policy decisions -------------------------------------------------------------------------------
DECISION = {
    "AUTO_APPROVE": ("⚡", "Paid automatically", "good", "Every check passed, so no one needed to touch it."),
    "SINGLE_APPROVAL": ("✋", "Needs one approver", "warning", "A person must review it before it's paid."),
    "DUAL_APPROVAL": ("👥", "Needs two approvers", "warning", "Two different people must approve it."),
    "HOLD_CALLBACK": ("📞", "On hold until we call the vendor", "serious",
                      "The bank details changed. Someone must phone the vendor on the number we already have."),
    "BLOCK": ("⛔", "Stopped", "critical", "This payment is not allowed and will not be made."),
}


def decision(code: str | None) -> tuple[str, str, str, str]:
    return DECISION.get(code or "", ("…", "Not decided yet", "neutral", ""))


# --- fraud signals ---------------------------------------------------------------------------------
SIGNAL = {
    "BANK_DETAILS_CHANGED": ("🏦", "Different bank account",
                             "The invoice asks us to pay into an account that isn't the one on file for this "
                             "vendor. Most payment fraud starts this way."),
    "BANK_CHANGE_VIA_EMAIL": ("✉️", "Bank change requested by email",
                              "Fraudsters pretend to be vendors over email. Real bank changes are confirmed "
                              "by phone."),
    "LOOKALIKE_VENDOR": ("🎭", "Look-alike sender",
                         "The name or email address is almost, but not exactly, a vendor we know."),
    "DUPLICATE_INVOICE": ("📑", "Possible duplicate",
                          "Looks like an invoice we've already paid, perhaps resubmitted with a new number."),
    "AMOUNT_ANOMALY": ("📈", "Unusually large amount", "Much higher than this vendor normally bills us."),
    "FIRST_TIME_PAYEE": ("🆕", "First payment to this vendor", "We have never successfully paid them before."),
    "NEW_ACCOUNT_RECENT": ("🕒", "Bank account added recently", "Their bank account was set up in the last 30 days."),
    "URGENCY_LANGUAGE": ("⏰", "Pressure to pay quickly",
                         "The message pushes for immediate payment, a common fraud tactic."),
    "OFAC_HIT": ("🚫", "Sanctions list match",
                 "The vendor's name matches an entry on the US sanctions list. We can't legally pay them."),
}


def signal(code: str) -> tuple[str, str, str]:
    return SIGNAL.get(code, ("⚠️", code.replace("_", " ").title(), ""))


def risk_level(score: int | None) -> tuple[str, str]:
    t = fraud_config()["thresholds"]
    if score is None:
        return "Not screened", "neutral"
    if score <= t["low"]:
        return f"Low risk ({score})", "good"
    if score < t["high"]:
        return f"Medium risk ({score})", "warning"
    return f"High risk ({score})", "critical"


# --- payment rails ----------------------------------------------------------------------------------
RAIL = {
    "ach_standard": ("🏦", "Standard bank transfer (ACH)", "arrives the next business day"),
    "ach_same_day": ("🏦", "Same-day bank transfer (ACH)", "arrives the same day"),
    "rtp": ("⚡", "Instant payment (RTP)", "arrives in seconds, can't be reversed"),
    "fednow": ("⚡", "Instant payment (FedNow)", "arrives in seconds, can't be reversed"),
    "wire": ("🔁", "Wire transfer", "same day, can't be reversed"),
    "virtual_card": ("💳", "Virtual card", "paid by card"),
    "check": ("✉️", "Paper check", "takes about a week; protected by positive pay"),
}


RAIL_SHORT = {"ach_standard": "ACH", "ach_same_day": "Same-day ACH", "rtp": "Instant (RTP)",
              "fednow": "Instant (FedNow)", "wire": "Wire", "virtual_card": "Card", "check": "Check"}


def rail_short(code: str | None) -> str:
    return RAIL_SHORT.get(code or "", code or "-")


def rail(code: str | None) -> tuple[str, str, str]:
    """(icon, name, description incl. fee from config/rails.yaml)."""
    if code not in RAIL:
        return "", code or "-", ""
    icon, name, desc = RAIL[code]
    cfg = rails_config()["rails"][code]
    fee = f"{cfg.get('rebate_bps', 0) / 100:g}% rebate" if cfg.get("rebate_bps") else f"fee {money(cfg['fee_cents'])}"
    return icon, name, f"{desc} · {fee}"


# --- policy reasons -> sentences ----------------------------------------------------------------------
_MONEY = r"\$[\d,]+\.\d{2}"


def reason(text: str) -> str:
    """Turn a rules-engine reason ('dual_approval.min_amount_cents: $480,000.00 >= $50,000.00') into a sentence."""
    rule, _, detail = text.partition(": ")
    m = re.findall(_MONEY, detail)
    if rule == "hard_blocks":
        return f"{signal(detail)[1]}: payments to this vendor are never allowed."
    if rule == "hold_until_callback":
        code = detail.split()[0]
        return f"{signal(code)[1]}: we must call the vendor on the number we already have before paying."
    if rule == "dual_approval.min_amount_cents" and len(m) == 2:
        return f"It's over {m[1]}, so two different people must approve it."
    if rule == "dual_approval.rails_always_dual":
        return f"It goes by {rail(detail)[1].lower()}, which always needs two approvers."
    if rule == "dual_approval.instant_min_amount_cents":
        return "Instant payments over $25,000 can't be reversed, so two people must approve."
    if rule == "auto_pay.max_amount_cents" and len(m) == 2:
        return f"At {m[0]} it's over the {m[1]} limit for automatic payment."
    if rule == "auto_pay.require_trusted_vendor":
        return "We haven't paid this vendor successfully enough times to pay them automatically yet."
    if rule == "auto_pay.require_full_match":
        state = detail.rsplit(" ", 1)[-1]
        return ("It doesn't fully match its purchase order and completed work order." if state == "partial"
                else "There's no purchase order to match it against.")
    if rule == "auto_pay.max_risk_score":
        nums = re.findall(r"\d+", detail)
        return f"Its fraud risk score ({nums[0]}) is above the automatic-payment limit ({nums[1]})."
    if rule == "auto_pay.block_flagged_vendors":
        return "This vendor is flagged after a payment bounced, so a person must check it."
    if rule == "auto_pay" and "amount unknown" in detail:
        return "We couldn't read the amount."
    if rule == "auto_pay":
        return "Trusted vendor, matches its purchase order and work order, low fraud risk and under the limit."
    if rule == "force_review":
        return f"{signal(detail)[1]}: a person always checks these."
    if rule == "intake":
        return "The AI couldn't read the invoice with confidence, so a person should check the details."
    return text


# --- audit trail -----------------------------------------------------------------------------------------
AGENT = {
    "intake_agent": "Intake agent", "match_agent": "Matching agent", "fraud_agent": "Fraud monitor",
    "rules_engine": "Payment policy", "rail_optimizer": "Payment planner", "executor": "Payment sender",
    "reconciler": "Reconciliation agent", "seed_script": "Demo setup",
}
ACTION = {
    "extract": "read the invoice", "three_way_match": "checked it against the purchase order and work order",
    "fraud_screen": "screened it for fraud", "decide": "decided what happens next",
    "open_callback": "opened a call-back task", "plan_rail": "chose how and when to pay",
    "send": "sent the payment to the bank", "submit_ach": "submitted an ACH file to the bank",
    "settle": "confirmed the money arrived", "paid": "marked the invoice paid",
    "returned": "recorded a bounced payment", "reopen": "reopened the invoice", "flag": "flagged the vendor",
    "invalidate": "marked the bank account as unverified", "open": "opened an exception",
    "approve": "approved", "reject": "rejected", "release": "released the payment", "block": "stopped the invoice",
    "callback_verified": "confirmed the bank change by phone", "callback_rejected": "found the bank change was fake",
    "activate": "activated a new bank account", "deactivate": "retired the old bank account",
    "set_trusted": "marked the vendor as trusted", "clear_flag": "cleared the vendor's flag",
    "resolve": "resolved an exception", "reset_and_seed": "reset the demo data",
}


def actor(name: str, people: dict[str, str]) -> str:
    if name in AGENT:
        return f"🤖 {AGENT[name]}"
    return f"👤 {people.get(name, name).split(' - ')[0]}"


def action(code: str) -> str:
    return ACTION.get(code, code.replace("_", " "))


def _day(iso: str) -> str:
    from datetime import date

    try:
        return f"{date.fromisoformat(iso):%a %d %b}"
    except ValueError:
        return iso


def audit_reason(action: str, text: str | None) -> str:
    """Plain-English version of an audit-log reason."""
    text = text or ""
    if action == "decide":
        outcome, _, rest = text.partition(": ")
        return f"{decision(outcome)[1]}. " + " ".join(reason(r) for r in rest.split("; ") if r)
    if action == "extract":
        if text.startswith("needs review"):
            return "The AI wasn't confident about some details, so a person should check them."
        return "Every field read and double-checked."
    if action == "three_way_match":
        state, _, rest = text.partition(": ")
        return {"full": "Fully matched its purchase order and the completed work.",
                "partial": f"Only partly matched: {rest}.", "none": f"Couldn't be matched: {rest}."}.get(state, text)
    if action == "fraud_screen":
        m = re.match(r"risk (\d+): (.*)", text)
        if not m:
            return text
        level = risk_level(int(m.group(1)))[0]
        if m.group(2) == "no signals":
            return f"{level}. No red flags."
        return f"{level}. Red flags: " + ", ".join(signal(c.strip())[1] for c in m.group(2).split(",")) + "."
    if action == "plan_rail":
        m = re.match(r"(\w+) send (\S+) settle (\S+):", text)
        if m:
            return f"{rail(m.group(1))[1]}, sent {_day(m.group(2))}, arrives {_day(m.group(3))}."
    if action == "send":
        m = re.search(r"effective (\S+)", text)
        if m:
            return f"Added to the bank-transfer file; arrives {_day(m.group(1))}."
        if text.startswith(("rtp", "fednow")):
            return "Instant payment completed: the money has arrived."
        if text.startswith("wire"):
            return "Wire accepted by the bank."
        if text.startswith("virtual_card"):
            return "Card payment authorized."
        if text.startswith("check"):
            return "Check issued and registered for positive pay."
    if action == "settle":
        m = re.match(r"statement (\S+)", text)
        if m:
            return f"Showed up on the bank statement on {_day(m.group(1))}."
    if action == "release":
        return "All required approvals are in, so the payment can go."
    return text


def return_reason(code: str | None) -> str:
    if not code:
        return ""
    what, todo = describe(code)
    return f"{what} ({code}). {todo}"


# --- small HTML helpers ---------------------------------------------------------------------------------


def pill(text: str, tone: str = "neutral", icon: str = "") -> str:
    fg, bg = TONE[tone]
    return (f'<span class="pp-pill" style="color:{fg};background:{bg};border-color:{fg}33">'
            f'{icon + " " if icon else ""}{text}</span>')


def money(cents: int | None) -> str:
    return "-" if cents is None else f"${cents / 100:,.2f}"
