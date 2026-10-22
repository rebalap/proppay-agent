"""Approvals inbox: a compact table of everything the agents paused for a person.

Design: decide from the row; details and history open in a side panel so the list stays in view; approving
money and rejecting each get a short, specific confirmation; a rejection always needs a reason.
"""

import streamlit as st

import labels as L
from common import (
    USERS,
    day,
    finding_html,
    h,
    html,
    md,
    money,
    open_invoice,
    page,
    runtime,
    session,
    user,
    user_name,
)
from invoice_views import render_checks, render_read, render_timeline

from app import graph as G
from app.metrics import run_invoices
from app.models import Invoice

page("Approvals inbox", "Everything the agents paused for a person. Decide from the table, or open the details.")
deps, graph = runtime()

with session() as s:
    ids = [i.id for i in run_invoices(s)]
actions = {a["invoice_id"]: a for a in G.pending_actions(graph, ids)}

OTHER = "Other (explain below)"
REJECT_REASONS = ["Duplicate of an invoice we already paid", "Amount doesn't match the purchase order",
                  "Work not completed or goods not received", "Suspected fraud", OTHER]


# --- what each row is about ---------------------------------------------------------------------------------------


def approved_by(a: dict) -> list[str]:
    return [x["approver"] for x in a.get("approvals_so_far", []) if x["approved"]]


def needs(a: dict) -> tuple[str, str]:
    if a["kind"] == "callback":
        return "📞 Call-back", "serious"
    if a["required_level"] == "dual":
        return f"👥 2 approvals ({len(approved_by(a))}/2)", "warning"
    return "👤 1 approval", "warning"


def key_issue(a: dict) -> str:
    codes = [x["code"] for x in a["signals"]]
    if a["kind"] == "callback":
        return "🏦 New bank details" + (" by email" if "BANK_CHANGE_VIA_EMAIL" in codes else "")
    if "DUPLICATE_INVOICE" in codes:
        return "📑 Possible duplicate"
    if codes:
        icon, title, _ = L.signal(codes[0])
        return f"{icon} {title}"
    if a["required_level"] == "dual":
        return "💰 Large payment" + (" by wire" if a.get("rail") == "wire" else "")
    return "✋ Needs a check"


# --- doing it -----------------------------------------------------------------------------------------------------


def act(a: dict, answer: dict) -> None:
    summary = G.resume(deps, graph, a["invoice_id"], answer)
    o = summary.outcomes[0]
    vendor, amount = a["vendor"], money(a.get("pay_amount_cents") or a["amount_cents"])
    if o.status == "blocked":
        result = ("⛔", f"Payment stopped. {vendor}'s invoice {a['invoice_number']} is blocked as a suspected "
                        "fraud attempt. Nothing was paid.")
    elif o.status == "rejected":
        result = ("⛔", f"Rejected. {vendor} won't be paid for {a['invoice_number']}.")
    elif o.status == "escalated":
        result = ("⚠️", f"That couldn't be accepted ({o.note}) and has been escalated.")
    elif o.waiting_for == "approval" and o.note and "second approver" in o.note:
        result = ("👍", f"Approval 1 of 2 recorded. A different person (not {user_name()}) must give the second "
                        f"approval before the {amount} payment to {vendor} can go.")
    elif o.waiting_for == "approval" and a["kind"] == "callback":
        result = ("📞", f"Bank change confirmed and {vendor}'s new account saved. Because the risk is still high, "
                        "the payment now needs an approver: it's back in the table.")
    elif o.waiting_for:
        result = ("⚠️", o.note or "Still waiting.")
    elif summary.execution and a.get("payment_id") in summary.execution.failed:
        result = ("⚠️", f"Approved, but the bank couldn't be reached, so the {amount} payment to {vendor} hasn't "
                        "gone yet. It's saved and will be sent automatically on the next bank run, without any "
                        "risk of paying twice.")
    else:
        sent = summary.execution and summary.execution.sent
        rail = L.rail(a.get("rail"))[1].lower()
        result = ("✅", f"Approved. {amount} to {vendor} " + (f"is on its way by {rail}." if sent else
                                                              f"will go out by {rail} on {day(a.get('send_date'))}."))
    st.session_state["last_result"] = result
    close_dialog()
    st.rerun()


# Which dialog is open lives in session state, so it survives any rerun (a sidebar change, a widget inside it).
def open_dialog(kind: str, invoice_id: int) -> None:
    st.session_state["dialog"] = (kind, invoice_id)


def close_dialog() -> None:
    st.session_state.pop("dialog", None)


def switch_to(kind: str, invoice_id: int) -> None:
    """From one dialog to another (Streamlit shows one at a time)."""
    open_dialog(kind, invoice_id)
    st.rerun()


def cancel() -> None:
    close_dialog()
    st.rerun()


@st.dialog("Approve this payment?", width="medium", on_dismiss=close_dialog)
def approve_dialog(a: dict) -> None:
    r_icon, r_name, r_desc = L.rail(a["rail"])
    st.markdown(md(f"You're approving **{money(a['pay_amount_cents'])}** to **{a['vendor']}** "
                   f"(invoice {a['invoice_number']}) by **{r_icon} {r_name}**, sent **{day(a['send_date'])}**."))
    if a["required_level"] == "dual" and not approved_by(a):
        st.info("This is approval 1 of 2. Nothing is sent until a second, different person approves.", icon="👥")
    elif a["rail"] in ("wire", "rtp", "fednow"):
        st.warning(md(f"Once approved it goes straight to the bank. {r_name} payments can't be reversed."), icon="⚠️")
    else:
        st.caption(md(f"It goes to the bank as soon as you confirm ({r_desc})."))
    note = st.text_input("Note for the audit trail (optional)", placeholder="e.g. checked with the property manager")
    c = st.columns(2)
    if c[0].button("Cancel", width="stretch"):
        cancel()
    if c[1].button(f"Approve {money(a['pay_amount_cents'])}", type="primary", width="stretch"):
        act(a, {"approver": user(), "decision": "approve", "reason": note})


@st.dialog("Reject this payment?", width="medium", on_dismiss=close_dialog)
def reject_dialog(a: dict) -> None:
    st.markdown(md(f"**{money(a['amount_cents'])}** to **{a['vendor']}** · invoice {a['invoice_number']}"))
    is_dup = any(x["code"] == "DUPLICATE_INVOICE" for x in a["signals"])
    why = st.radio("Why are you rejecting it?", REJECT_REASONS, index=0 if is_dup else None)
    detail = st.text_input("Details" + (" (required)" if why == OTHER else " (optional)"))
    ok = why is not None and (why != OTHER or len(detail.strip()) >= 5)
    st.caption("The invoice won't be paid. Your reason is recorded in the audit trail.")
    c = st.columns(2)
    if c[0].button("Cancel", width="stretch"):
        cancel()
    if c[1].button("Reject payment", type="primary", width="stretch", disabled=not ok):
        reason = detail.strip() if why == OTHER else (f"{why}: {detail.strip()}" if detail.strip() else why)
        act(a, {"approver": user(), "decision": "reject", "reason": reason})


@st.dialog("Record the vendor call-back", width="medium", on_dismiss=close_dialog)
def callback_dialog(a: dict) -> None:
    st.markdown(md(f"**{a['vendor']}** wants payments to go to routing **{a['new_routing']}**, account ending "
                   f"**{a['new_account_last4']}**."))
    html("<div class='pp-find' style='border-color:#2a78d6;background:#f3f8fe'><b>Before you answer</b>"
         f"1. Phone {h(a['vendor'])} on the number in our vendor file. <b>Never</b> use a number from the email or "
         "invoice.<br>2. Ask whether they really changed their bank account.</div>")
    said = st.radio("What did the vendor say?", ["They did NOT change banks", "They confirmed the new account"],
                    index=None)
    note = st.text_input("Notes from the call", placeholder="e.g. spoke to Maria in accounts; never changed banks")
    acct = ""
    if said == "They confirmed the new account":
        acct = st.text_input("Full new account number, as confirmed on the call")
    c = st.columns(2)
    if c[0].button("Cancel", width="stretch"):
        cancel()
    if said == "They did NOT change banks":
        if c[1].button("⛔ Stop this payment", type="primary", width="stretch"):
            act(a, {"verified": False, "by": user(), "note": note or "vendor denied the change"})
    elif said:
        if c[1].button("Save new account and continue", type="primary", width="stretch", disabled=not acct):
            act(a, {"verified": True, "by": user(), "account_number": acct, "note": note})


@st.dialog("Invoice details", width="large", position="right", on_dismiss=close_dialog)
def details_dialog(a: dict) -> None:
    label, tone = needs(a)
    risk_text, risk_tone = L.risk_level(a["risk_score"])
    st.markdown(md(f"### {a['vendor']}"))
    html(L.pill(money(a["amount_cents"]), "info", "💵") + L.pill(label, tone)
         + L.pill(risk_text, risk_tone, "🛡️") + L.pill(f"due {day(a['due_date'])}", "neutral", "🗓️"))
    st.caption(md(f"Invoice {a['invoice_number']}"))

    st.markdown("**Why it's here**")
    st.markdown(md("\n".join(f"- {L.reason(r)}" for r in a["decision_reasons"])))
    if a["signals"]:
        st.markdown("**Red flags**")
        html("".join(finding_html(x) for x in a["signals"]))
    if a.get("rationale"):
        st.markdown("**🧠 AI analyst's briefing** <span class='pp-muted'>(explains, doesn't decide)</span>",
                    unsafe_allow_html=True)
        st.markdown(md(a["rationale"]["summary"]))
        st.markdown(md("\n".join(f"- {x}" for x in a["rationale"]["recommended_actions"])))
    if a["kind"] == "approval":
        r_icon, r_name, r_desc = L.rail(a["rail"])
        st.markdown("**If you approve**")
        st.markdown(md(f"We pay {money(a['pay_amount_cents'])} by {r_icon} {r_name}, sent {day(a['send_date'])} "
                       f"({r_desc})."))
        if a.get("rail_explanation"):
            st.caption(md(a["rail_explanation"]))
        if approved_by(a):
            st.caption("Approved so far by " + ", ".join(user_name(x) for x in approved_by(a)) + ".")

    with session() as s:
        inv = s.get(Invoice, a["invoice_id"])
        t1, t2, t3 = st.tabs(["What the AI read", "What we checked", "History"])
        with t1:
            render_read(inv, compact=True)
        with t2:
            render_checks(inv)
        with t3:
            render_timeline(s, inv.id)

    st.divider()
    c = st.columns(3)
    if a["kind"] == "approval":
        mine = user() in approved_by(a)
        if c[0].button("✅ Approve…", type="primary", width="stretch", disabled=mine,
                       help="You've already approved this one" if mine else None):
            switch_to("approve", a["invoice_id"])
        if c[1].button("✕ Reject…", width="stretch"):
            switch_to("reject", a["invoice_id"])
    elif c[0].button("📞 Record call-back…", type="primary", width="stretch"):
        switch_to("callback", a["invoice_id"])
    if c[2].button("Open full invoice page", width="stretch"):
        close_dialog()
        open_invoice(a["invoice_id"])


@st.dialog("History", width="large", position="right", on_dismiss=close_dialog)
def history_dialog(a: dict) -> None:
    st.markdown(md(f"**{a['vendor']}** · invoice {a['invoice_number']} · {money(a['amount_cents'])}"))
    with session() as s:
        render_timeline(s, a["invoice_id"])


DIALOGS = {"approve": approve_dialog, "reject": reject_dialog, "callback": callback_dialog,
           "details": details_dialog, "history": history_dialog}

# --- page ----------------------------------------------------------------------------------------------------------

if "last_result" in st.session_state:
    icon, text = st.session_state.pop("last_result")
    (st.success if icon in ("✅", "👍", "📞") else st.warning)(md(text), icon=icon)

if not actions:
    st.success("🎉 You're all caught up. Nothing is waiting for a decision.")
    st.stop()

rows = sorted(actions.values(), key=lambda a: (a["kind"] != "callback", a["due_date"] or "9999", a["invoice_id"]))
m = st.columns(4)
m[0].metric("Waiting", len(rows), border=True)
m[1].metric("Total value", money(sum(a["amount_cents"] or 0 for a in rows)), border=True)
m[2].metric("Call-backs", sum(a["kind"] == "callback" for a in rows), border=True,
            help="Bank details changed: phone the vendor before anything is paid.")
m[3].metric("Need 2 approvers", sum(a.get("required_level") == "dual" for a in rows), border=True)

f = st.columns([3, 2], vertical_alignment="center")
show = f[0].segmented_control("Show", ["All", "Call-backs", "Approvals", "Possible duplicates"], default="All",
                              label_visibility="collapsed") or "All"
f[1].caption(md(f"Acting as **{USERS[user()]}**. Switch user in the sidebar to give a second approval."))
if show == "Call-backs":
    rows = [a for a in rows if a["kind"] == "callback"]
elif show == "Approvals":
    rows = [a for a in rows if a["kind"] == "approval"]
elif show == "Possible duplicates":
    rows = [a for a in rows if any(x["code"] == "DUPLICATE_INVOICE" for x in a["signals"])]

WIDTHS = [1.3, 2.4, 1.15, 0.95, 1.8, 0.9, 2.4]
html("<style>.pp-row {border-top:1px solid #e6e4df; margin-top:2px;} "
     ".pp-th {font-size:0.76rem; color:#6b6a66; text-transform:uppercase; letter-spacing:.05em;}</style>")
head = st.columns(WIDTHS)
for col, label in zip(head, ["Needs", "Vendor · invoice", "Amount", "Due", "Key issue", "Risk", "Actions"]):
    col.markdown(f"<div class='pp-th'>{label}</div>", unsafe_allow_html=True)

for a in rows:
    iid = a["invoice_id"]
    html("<div class='pp-row'></div>")
    c = st.columns(WIDTHS, vertical_alignment="center")
    label, tone = needs(a)
    c[0].markdown(L.pill(label, tone), unsafe_allow_html=True)
    c[1].markdown(md(f"**{a['vendor']}**  \n<span class='pp-muted'>{a['invoice_number']}</span>"),
                  unsafe_allow_html=True)
    c[2].markdown(md(f"**{money(a['amount_cents'])}**"))
    c[3].markdown(day(a["due_date"]))
    c[4].markdown(key_issue(a))
    risk_text, risk_tone = L.risk_level(a["risk_score"])
    c[5].markdown(L.pill(risk_text.split(" (")[0].replace(" risk", ""), risk_tone), unsafe_allow_html=True)
    b = c[6].columns(4, gap="small")
    if a["kind"] == "approval":
        mine = user() in approved_by(a)
        b[0].button("✓", key=f"ok-{iid}", type="primary", disabled=mine, width="stretch",
                    help="You've already approved this; a different person must approve too" if mine
                    else f"Approve {money(a['pay_amount_cents'])}", on_click=open_dialog, args=("approve", iid))
        b[1].button("✕", key=f"no-{iid}", width="stretch", help="Reject (asks for a reason)",
                    on_click=open_dialog, args=("reject", iid))
    else:
        b[0].button("📞", key=f"cb-{iid}", type="primary", width="stretch", help="Record the vendor call-back",
                    on_click=open_dialog, args=("callback", iid))
    b[2].button("👁", key=f"det-{iid}", width="stretch", help="See all the details", on_click=open_dialog,
                args=("details", iid))
    b[3].button("🕒", key=f"his-{iid}", width="stretch", help="See the history", on_click=open_dialog,
                args=("history", iid))

if not rows:
    st.caption("Nothing in this filter.")

opened = st.session_state.get("dialog")
if opened and opened[1] in actions:
    DIALOGS[opened[0]](actions[opened[1]])
elif opened:
    close_dialog()  # the item was dealt with elsewhere
st.caption("✓ approve · ✕ reject · 📞 record the call-back · 👁 details · 🕒 history. Approving or rejecting always "
           "asks you to confirm first.")
