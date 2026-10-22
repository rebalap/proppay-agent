"""One invoice's full story: what the AI read, what was checked, what was found, what was decided, how it was paid."""

import json
from pathlib import Path

import streamlit as st
from sqlmodel import or_, select

import labels as L
from common import (
    USERS,
    day,
    decision_pill,
    finding_html,
    html,
    journey_html,
    latest_payment,
    md,
    money,
    page,
    runtime,
    session,
    status_pill,
    vendor_name,
)

from app.agents.intake import load_source
from invoice_views import render_checks, render_read, render_timeline
from app.metrics import run_invoices
from app.models import Approval, AuditLog, BankChangeRequest, Invoice, Payment

page("Invoice details", "Follow one invoice from the moment it arrived to the moment it was paid (or stopped).")
deps, _ = runtime()

with session() as s:
    invoices = run_invoices(s)
    if not invoices:
        st.info("No invoices yet.")
        st.stop()
    labels = {i.id: f"{vendor_name(s, i)} · {i.invoice_number or 'not read yet'} · {money(i.amount_cents)} · "
                    f"{L.status(i.status.value)[1]}" for i in invoices}
    ids = list(labels)
    wanted = st.session_state.get("selected_invoice", ids[0])
    chosen = st.selectbox("Invoice", ids, index=ids.index(wanted) if wanted in ids else 0, format_func=labels.get)
    st.session_state["selected_invoice"] = chosen

    inv = s.get(Invoice, chosen)
    pay = latest_payment(s, inv.id)
    ext = json.loads(inv.extracted_json) if inv.extracted_json else None
    match = json.loads(inv.match_json) if inv.match_json else None
    issues = json.loads(inv.intake_issues_json or "[]")
    signals = json.loads(inv.signals_json or "[]")
    rationale = json.loads(inv.fraud_rationale_json) if inv.fraud_rationale_json else None
    reasons = json.loads(inv.decision_reasons_json or "[]")
    vname = vendor_name(s, inv)

    # --- header -----------------------------------------------------------------------------------------
    st.markdown(md(f"## {vname} · {inv.invoice_number or 'not read yet'}"))
    risk_text, risk_tone = L.risk_level(inv.risk_score)
    html(L.pill(money(inv.amount_cents), "info", "💵") + status_pill(inv.status.value)
         + (decision_pill(inv.decision) if inv.decision else "") + L.pill(risk_text, risk_tone, "🛡️"))
    _, _, _, status_desc = L.status(inv.status.value)
    _, _, _, decision_desc = L.decision(inv.decision)
    st.markdown(md(f"**{status_desc}.** {decision_desc}"))
    html(journey_html(inv, pay))
    if inv.status.value in ("pending_approval", "on_hold"):
        if st.button("Go to the approvals inbox →", type="primary"):
            st.switch_page("pages/2_Approvals.py")

    # --- read + checked ---------------------------------------------------------------------------------
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.markdown("#### 📖 What the AI read")
            render_read(inv)
    with right:
        with st.container(border=True):
            st.markdown("#### ✅ What we checked")
            render_checks(inv)
            if match:
                result = {"full": "fully matched", "partial": "partly matched", "none": "not matched"}
                html(f"<p class='pp-muted'>Match result: <b>{result[match['match_status']]}</b></p>")

    # --- red flags -----------------------------------------------------------------------------------------
    if signals:
        st.markdown("### 🚩 Red flags")
        html("".join(finding_html(x) for x in signals))
        if rationale:
            with st.expander("🧠 The AI analyst's briefing (explains, doesn't decide)", expanded=True):
                st.markdown(md(rationale["summary"]))
                st.markdown(md("**What to check:**\n" + "\n".join(f"- {a}" for a in rationale["recommended_actions"])))

    # --- decision -----------------------------------------------------------------------------------------------
    if reasons:
        st.markdown("### ⚖️ The decision")
        icon, label, tone, desc = L.decision(inv.decision)
        html(L.pill(label, tone, icon))
        st.markdown(md("\n".join(f"- {L.reason(r)}" for r in reasons)))
        with st.expander("Exact policy rules (for auditors)"):
            st.code("\n".join(reasons), language=None)

    # --- payment ------------------------------------------------------------------------------------------------
    if pay:
        st.markdown("### 💸 The payment")
        r_icon, r_name, r_desc = L.rail(pay.rail.value)
        with st.container(border=True):
            p = st.columns(4)
            p[0].metric("Amount", money(pay.amount_cents))
            p[1].metric("Method", f"{r_icon} {L.rail_short(pay.rail.value)}")
            p[2].metric("Sent on", f"{pay.scheduled_date:%a %d %b}")
            p[3].metric("Arrives", f"{pay.expected_settle_date:%a %d %b}" if pay.expected_settle_date else "-")
            st.caption(md(f"{r_name}: {r_desc}."))
            extras = []
            if pay.discount_cents:
                extras.append(f"🎁 Early-payment discount captured: **{money(pay.discount_cents)}**")
            if pay.rebate_cents:
                extras.append(f"💳 Card rebate earned: **{money(pay.rebate_cents)}**")
            extras.append(f"Bank fee: {money(pay.fee_cents)}")
            st.markdown(md(" · ".join(extras)))
            if pay.rail_explanation:
                st.info(md(f"Why this way: {pay.rail_explanation}"))
            pstat = {"pending_approval": "⏳ waiting for approval", "scheduled": "🗓️ released, goes out with the next "
                     "bank run", "sent": "🏦 with the bank", "settled": "✅ arrived", "returned":
                     f"↩️ bounced: {L.return_reason(pay.return_code)}", "blocked": "⛔ cancelled"}[pay.status.value]
            st.markdown(md(f"**Payment status:** {pstat}" + (f" · bank reference `{pay.bank_reference}`"
                                                             if pay.bank_reference else "")))

    # --- timeline ---------------------------------------------------------------------------------------------------
    st.markdown("### 🕒 Timeline")
    render_timeline(s, inv.id)

    # --- original document ------------------------------------------------------------------------------------------
    src = deps.data_dir / inv.source_file
    with st.expander("📄 Original document"):
        if src.exists():
            st.download_button(f"Download {Path(inv.source_file).name}", src.read_bytes(),
                               file_name=Path(inv.source_file).name)
            st.code(load_source(src).text, language=None)
        else:
            st.caption("The source file isn't available.")
