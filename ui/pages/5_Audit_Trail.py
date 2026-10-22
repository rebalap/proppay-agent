"""Audit trail: every action by an agent or a person, in plain English, with the before/after record."""

import json

import pandas as pd
import streamlit as st
from sqlmodel import select

import labels as L
from common import USERS, md, page, session

from app.metrics import AGENT_ACTORS
from app.models import AuditLog, Invoice, Payment

page("Audit trail", "Every change made by an AI agent or a person: who did it, what changed, and why.")

with session() as s:
    rows = s.exec(select(AuditLog).where(AuditLog.actor != "seed_script").order_by(AuditLog.id.desc())).all()
    inv_num = {i.id: i.invoice_number for i in s.exec(select(Invoice)).all()}
    pay_inv = {p.id: p.invoice_id for p in s.exec(select(Payment)).all()}

if not rows:
    st.info("Nothing has happened yet.")
    st.stop()


def subject(r: AuditLog) -> str:
    if r.entity == "invoices":
        return f"invoice {inv_num.get(r.entity_id) or '#' + str(r.entity_id)}"
    if r.entity == "payments":
        return f"payment for {inv_num.get(pay_inv.get(r.entity_id)) or 'invoice'}"
    return {"vendors": "a vendor", "approvals": "a payment", "bank_change_requests": "a bank-change request",
            "vendor_bank_accounts": "a bank account", "payment_batches": "a payment batch",
            "recon_exceptions": "a reconciliation problem"}.get(r.entity, r.entity)


def why(r: AuditLog) -> str:
    return L.audit_reason(r.action, r.reason)


df = pd.DataFrame([{
    "id": r.id, "When (UTC)": r.ts, "Who": L.actor(r.actor, USERS), "Did what": f"{L.action(r.action)}",
    "To": subject(r), "Why": why(r), "kind": "agent" if r.actor in AGENT_ACTORS else "person",
} for r in rows])

f = st.columns([1.4, 2, 3])
who = f[0].radio("Show actions by", ["Everyone", "AI agents", "People"], horizontal=False)
actors = f[1].multiselect("Only these", sorted(df.Who.unique()))
text = f[2].text_input("Search", placeholder="e.g. AP-4686, bounced, sanctions, wire")
view = df
if who != "Everyone":
    view = view[view.kind == ("agent" if who == "AI agents" else "person")]
if actors:
    view = view[view.Who.isin(actors)]
if text:
    hay = view.Why.fillna("") + " " + view.To + " " + view["Did what"]
    view = view[hay.str.contains(text, case=False)]

st.caption(f"Showing {len(view)} of {len(df)} actions, newest first. Select one to see exactly what changed.")
picked = st.dataframe(view, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                      column_order=["When (UTC)", "Who", "Did what", "To", "Why"],
                      column_config={"When (UTC)": st.column_config.DatetimeColumn(format="ddd D MMM, HH:mm:ss")})
if picked.selection.rows:
    entry = next(r for r in rows if r.id == int(view.iloc[picked.selection.rows[0]].id))
    with st.container(border=True):
        st.markdown(md(f"**{L.actor(entry.actor, USERS)}** {L.action(entry.action)} · {subject(entry)}"))
        st.caption(md(why(entry)))
        before = json.loads(entry.before_json) if entry.before_json else {}
        after = json.loads(entry.after_json) if entry.after_json else {}
        changed = [{"Field": k.replace("_", " "), "Before": before.get(k), "After": after.get(k)}
                   for k in sorted(set(before) | set(after))
                   if before.get(k) != after.get(k) and not k.endswith("_json")]
        if changed and before:
            st.markdown("**What changed**")
            st.dataframe(pd.DataFrame(changed).astype(str), hide_index=True, width="stretch")
        with st.expander("Full record (technical)"):
            st.json(after or before)
