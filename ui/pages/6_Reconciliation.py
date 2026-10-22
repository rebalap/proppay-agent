"""Bank reconciliation: check the bank's statements against every payment we sent."""

from datetime import date

import pandas as pd
import streamlit as st
from sqlmodel import select

import labels as L
from common import html, md, money, page, runtime, session, user

from app import audit
from app import graph as G
from app.agents.reconciler import open_exceptions
from app.models import BankStatementLine, Invoice, Payment, ReconException, Vendor

page("Bank reconciliation", "Every day the bank sends a statement. The agent matches each line to a payment we sent "
                            "and flags anything that doesn't add up, such as a payment that bounced.")
deps, _ = runtime()

with st.container(border=True):
    c = st.columns([2, 1.3, 4], vertical_alignment="bottom")
    through = c[0].date_input("Check bank statements up to", value=date(2026, 11, 20), min_value=deps.now.date(),
                              help="In the demo, pick a date about a month ahead to see payments arrive or bounce.")
    if c[1].button("🔍 Check statements", type="primary", width="stretch"):
        with st.spinner("Downloading statements and matching them to payments…"):
            st.session_state["recon"] = G.reconcile_through(deps, through)
    c[2].caption("Statements are in the banks' standard format (BAI2). A statement whose totals don't add up is "
                 "rejected, not trusted.")

if "recon" in st.session_state:
    r = st.session_state["recon"]
    m = st.columns(4)
    m[0].metric("Bank transactions checked", r.matched + r.unmatched_lines, border=True)
    m[1].metric("Payments confirmed arrived", len(r.settled), border=True)
    m[2].metric("Payments that bounced", len(r.returned), border=True)
    m[3].metric("Transactions we can't explain", r.unmatched_lines, border=True)
    if r.statement_errors:
        st.error("Some statements failed their own control totals and were not used: "
                 + ", ".join(r.statement_errors))
    elif r.fully_matched:
        st.success("Every transaction on the bank statements matches a payment we sent.", icon="✅")

with session() as s:
    excs = open_exceptions(s)
    st.subheader("Problems to follow up" + (f" ({len(excs)})" if excs else ""))
    if not excs:
        st.caption("None. 🎉")
    for e in excs:
        pay = s.get(Payment, e["payment_id"]) if e["payment_id"] else None
        inv = s.get(Invoice, pay.invoice_id) if pay else None
        vendor = s.get(Vendor, inv.vendor_id).legal_name if inv and inv.vendor_id else None
        with st.container(border=True):
            if e["kind"] == "ach_return" and pay:
                what, todo = L.return_reason(pay.return_code).split(". ", 1)
                st.markdown(md(f"### ↩️ Payment to {vendor} bounced"))
                st.markdown(md(f"The {money(pay.amount_cents)} payment for invoice **{inv.invoice_number}** came "
                               f"back from {vendor}'s bank: **{what}**."))
                html("<div class='pp-find'><b>What the agents already did</b>"
                     "Reopened the invoice (it's unpaid again) · flagged the vendor so they can't be paid "
                     "automatically · marked their bank account as unverified · drafted the notice below.</div>")
                st.markdown(md(f"**What you should do:** {todo}"))
            else:
                titles = {"unmatched_line": "❓ A bank transaction we can't explain",
                          "missing_settlement": "⏳ A payment hasn't shown up at the bank",
                          "amount_mismatch": "⚠️ The bank took a different amount than we sent"}
                st.markdown(f"### {titles.get(e['kind'], e['kind'])}")
                st.markdown(md(e["detail"]))
            if e["draft_notice"]:
                st.markdown("**Email to the vendor**, drafted by the AI. Review it and send it yourself; "
                            "nothing is sent automatically.")
                st.text_input("Subject", e["draft_notice"]["subject"], key=f"subj-{e['id']}")
                st.text_area("Message", e["draft_notice"]["body"], key=f"body-{e['id']}", height=140)
            note = st.text_input("How was it resolved?", key=f"res-{e['id']}",
                                 placeholder="e.g. notice sent; vendor confirming new details by phone")
            if st.button("Mark as resolved", key=f"done-{e['id']}", disabled=not note):
                exc = s.get(ReconException, e["id"])
                before = audit.snapshot(exc)
                exc.status = "resolved"
                audit.record(s, actor=user(), entity="recon_exceptions", entity_id=exc.id, action="resolve",
                             before=before, after=exc, reason=note)
                s.commit()
                st.rerun()

    lines = s.exec(select(BankStatementLine).order_by(BankStatementLine.date, BankStatementLine.id)).all()
    st.subheader("Bank statement")
    if not lines:
        st.caption("No statements checked yet. Use **Check statements** above.")
    else:
        rows = []
        for ln in lines:
            pay = s.get(Payment, ln.matched_payment_id) if ln.matched_payment_id else None
            inv = s.get(Invoice, pay.invoice_id) if pay else None
            vendor = s.get(Vendor, inv.vendor_id).legal_name if inv and inv.vendor_id else "?"
            if ln.return_code:
                what = f"↩️ Returned by {vendor}'s bank"
            elif pay:
                what = f"{L.rail(pay.rail.value)[0]} Payment to {vendor}"
            else:
                what = "❓ Unknown transaction"
            rows.append({"Date": ln.date, "What": what, "Invoice": inv.invoice_number if inv else "",
                         "Amount": (ln.amount_cents if ln.return_code else -ln.amount_cents) / 100,
                         "Matched": "✅ matched" if pay else "❌ not matched",
                         "Bank reference": ln.bank_reference})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"Amount": st.column_config.NumberColumn("Amount", format="$%,.2f",
                                                                            help="Negative = money out"),
                                    "Date": st.column_config.DateColumn("Date", format="ddd D MMM")})
