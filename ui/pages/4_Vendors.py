"""Vendors: who we pay, how, and whether anything about them needs attention."""

import pandas as pd
import streamlit as st
from sqlmodel import select

import labels as L
from common import md, page, session, user

from app import audit
from app.models import BankChangeRequest, Payment, PaymentStatus, Invoice, Vendor, VendorBankAccount

page("Vendors", "Everyone we pay, how we pay them, and anything about them that needs attention.")

with session() as s:
    vendors = s.exec(select(Vendor).order_by(Vendor.legal_name)).all()
    accts = {a.vendor_id: a for a in s.exec(select(VendorBankAccount).where(VendorBankAccount.active)).all()}
    reqs = s.exec(select(BankChangeRequest).order_by(BankChangeRequest.id.desc())).all()
    names = {v.id: v.legal_name for v in vendors}
    settled = {}
    for p, i in s.exec(select(Payment, Invoice).join(Invoice).where(Payment.status == PaymentStatus.settled)).all():
        settled[i.vendor_id] = settled.get(i.vendor_id, 0) + 1

    m = st.columns(4)
    m[0].metric("Vendors", len(vendors), border=True)
    m[1].metric("Trusted for automatic payment", sum(v.trusted for v in vendors), border=True,
                help="A vendor becomes trusted after 3 payments that arrived without problems.")
    m[2].metric("Flagged", sum(v.flagged for v in vendors), border=True,
                help="Can't be paid automatically until someone clears the flag.")
    m[3].metric("Bank-change requests", len(reqs), border=True)

    flagged = [v for v in vendors if v.flagged]
    if flagged:
        st.subheader("⚠️ Flagged vendors")
        for v in flagged:
            with st.container(border=True):
                st.markdown(md(f"**{v.legal_name}**: {v.flag_reason}."))
                st.caption("While flagged, every invoice from this vendor needs a person to approve it. Clear the "
                           "flag once their bank details are confirmed through a trusted channel.")
                why = st.text_input("How was it resolved?", key=f"why-{v.id}",
                                    placeholder="e.g. new account confirmed by phone and validated")
                if st.button("Clear flag", key=f"clear-{v.id}", disabled=not why):
                    before = audit.snapshot(v)
                    v.flagged, v.flag_reason = False, None
                    audit.record(s, actor=user(), entity="vendors", entity_id=v.id, action="clear_flag",
                                 before=before, after=v, reason=why)
                    s.commit()
                    st.rerun()

    st.subheader("📞 Requests to change bank details")
    st.caption("Any change of bank details must be confirmed by phoning the vendor on the number we already have, "
               "before a single dollar goes to the new account.")
    if reqs:
        status_text = {"pending_callback": "📞 Waiting for a call-back", "verified": "✅ Confirmed by phone",
                       "rejected": "⛔ Fake: vendor never asked"}
        st.dataframe(pd.DataFrame([{
            "Vendor": names[r.vendor_id], "Asked by": r.requested_via.value, "New bank": r.new_routing,
            "New account": f"ending {r.new_account_last4}", "Status": status_text[r.status.value],
            "Checked by": L.actor(r.callback_done_by, {}).replace("👤 ", "") if r.callback_done_by else ""}
            for r in reqs]), hide_index=True, width="stretch")
        if any(r.status.value == "pending_callback" for r in reqs):
            if st.button("Do the call-back in the approvals inbox →", type="primary"):
                st.switch_page("pages/2_Approvals.py")
    else:
        st.caption("None.")

    st.subheader("All vendors")
    q = st.text_input("Search", placeholder="vendor name or category")
    rows = []
    for v in vendors:
        a = accts.get(v.id)
        rails = ", ".join(L.rail(r)[1].split(" (")[0] for r in v.enrolled_rails)
        rows.append({
            "Vendor": v.legal_name, "Type": v.category.replace("_", " "),
            "Status": "⚠️ Flagged" if v.flagged else ("✅ Trusted" if v.trusted else "🆕 New"),
            "Good payments": settled.get(v.id, 0),
            "Sanctions check": {"clear": "✅ Clear", "hit": "⛔ Match", "pending": "⏳ Pending"}[v.ofac_status.value],
            "Tax ID check": {"matched": "✅ Matched", "mismatched": "❌ Mismatch", "pending": "⏳ Pending"}[
                v.tin_match_status.value],
            "We can pay by": rails, "Bank account": f"ending {a.account_last4}" if a else "checks only",
            "Account verified": ("✅" if a.validated else "❌") if a else "",
            "Account since": a.effective_from if a else None,
        })
    df = pd.DataFrame(rows)
    if q:
        df = df[df.Vendor.str.contains(q, case=False) | df.Type.str.contains(q, case=False)]
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"Account since": st.column_config.DateColumn(format="D MMM YYYY")})
