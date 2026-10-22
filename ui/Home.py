"""Dashboard: what happened to Monday's invoices, in plain English."""

from collections import Counter

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import labels as L
from common import COLUMNS, SERIES_1, html, invoice_rows, md, money, open_invoice, page, session

from app.metrics import compute

page("Monday's accounts-payable run", "AI agents read, check and pay vendor invoices. Fixed rules, not the AI, "
                                      "decide whether money moves.")

with session() as s:
    m = compute(s)
    rows = invoice_rows(s)

if not rows:
    st.info("There's no AP run yet. Set up the demo with `uv run python scripts/prepare_demo.py`.")
    st.stop()


def n(k: int, one: str, many: str) -> str:
    return f"{k} {one if k == 1 else many}"


# --- the story in one paragraph ------------------------------------------------------------------------
by = Counter(r["_status"] for r in rows)
done = ("approved", "scheduled", "paid")
auto = sum(1 for r in rows if r["Outcome"] == "Paid automatically" and r["_status"] in done)
by_person = sum(1 for r in rows if r["Outcome"] != "Paid automatically" and r["_status"] in done)
waiting = by["pending_approval"] + by["on_hold"] + by["needs_review"]
stopped = by["blocked"]

parts = [f"The agents paid <b>{auto} automatically</b>"]
if by_person:
    parts.append(f"people approved <b>{by_person}</b>")
parts.append(f"<b>{waiting}</b> {'is' if waiting == 1 else 'are'} waiting for a decision" if waiting
             else "nothing is waiting for you")
if stopped:
    why = []
    if m.fraud_prevented_count:
        why.append(n(m.fraud_prevented_count, "fraud attempt", "fraud attempts"))
    if m.sanctions_blocked_count:
        why.append(n(m.sanctions_blocked_count, "sanctions match", "sanctions matches"))
    parts.append(f"<b>{stopped} {'was' if stopped == 1 else 'were'} stopped</b>"
                 + (f" ({', '.join(why)})" if why else ""))
story = ", ".join(parts[:-1]) + " and " + parts[-1] + "."
extra = []
if by["received"]:
    extra.append(f"{n(by['received'], 'new invoice has', 'new invoices have')} just arrived and "
                 "hasn't been processed yet." if by["received"] == 1 else
                 f"{by['received']} new invoices have just arrived and haven't been processed yet.")
if m.returns_count:
    extra.append(f"{n(m.returns_count, 'payment', 'payments')} bounced and "
                 f"{'needs' if m.returns_count == 1 else 'need'} follow-up.")
html(f'<div class="pp-hero"><h3>{len(rows)} invoices came in for Monday</h3><p>{md(story)} {" ".join(extra)}</p></div>')

# --- what needs attention --------------------------------------------------------------------------------
todo = []
if by["received"]:
    todo.append(("📥", f"{n(by['received'], 'new invoice is', 'new invoices are')} ready to process.",
                 "Process them", "pages/1_Payment_Run.py"))
if waiting:
    todo.append(("✋", f"{n(waiting, 'invoice needs', 'invoices need')} a decision from you.", "Review",
                 "pages/2_Approvals.py"))
if m.exceptions_open:
    todo.append(("↩️", f"{n(m.exceptions_open, 'bank problem needs', 'bank problems need')} follow-up.",
                 "Open", "pages/6_Reconciliation.py"))
if todo:
    st.subheader("Needs your attention")
    for icon, text, label, target in todo:
        with st.container(border=True):
            c = st.columns([6, 1.4], vertical_alignment="center")
            c[0].markdown(f"**{icon} {text}**")
            if c[1].button(f"{label} →", key=target, type="primary", width="stretch"):
                st.switch_page(target)
else:
    st.success("🎉 You're all caught up. Nothing needs your attention.")

# --- headline numbers ------------------------------------------------------------------------------------
st.subheader("At a glance")
k = st.columns(4)
k[0].metric("Handled with no human touch", f"{m.stp_rate:.0%}", border=True,
            help="Straight-through processing: invoices paid without anyone having to step in.")
k[0].caption(f"{m.stp_count} of {m.processed} processed invoices")
k[1].metric("Sent to vendors", money(m.paid_cents), border=True)
k[1].caption(f"{len([r for r in rows if r['Paid by']])} payments planned or sent")
k[2].metric("Fraud stopped", money(m.fraud_prevented_cents), border=True,
            help="Payments held or blocked that were fraud attempts.")
k[2].caption(f"{n(m.fraud_prevented_count, 'fake bank-change email', 'fake bank-change emails')} · "
             f"{n(m.sanctions_blocked_count, 'sanctions block', 'sanctions blocks')}")
k[3].metric("Saved on bank fees", money(m.rail_savings_cents), border=True,
            help="Compared with sending every payment by wire transfer.")
k[3].caption(md(f"paid {money(m.fees_cents)} instead of {money(m.wire_baseline_cents)}"))
k = st.columns(4)
k[0].metric("Early-payment discounts", money(m.discounts_cents), border=True,
            help="Paying early when a vendor's discount is worth more than our cost of money.")
k[1].metric("Card rebates", money(m.rebates_cents), border=True, help="Cash back on vendors paid by virtual card.")
k[2].metric("AI reading accuracy", f"{m.extraction_accuracy:.1%}" if m.extraction_accuracy else "-", border=True,
            help="How often the AI read invoice fields correctly on our labelled test set.")
k[3].metric("Payments that bounced", m.returns_count, border=True,
            help="Sent back by the vendor's bank, for example because the account doesn't exist.")

# --- charts ------------------------------------------------------------------------------------------------
CHART = dict(showlegend=False, xaxis=dict(visible=False), plot_bgcolor="rgba(0,0,0,0)",
             paper_bgcolor="rgba(0,0,0,0)", font=dict(color="#33322f", size=13))
left, right = st.columns(2)
with left:
    st.subheader("Where the invoices ended up")
    buckets = [("⚡ Paid automatically", auto, "#0ca30c"), ("👤 Approved by a person", by_person, "#2a78d6"),
               ("✋ Waiting for a decision", waiting, "#eda100"), ("📥 Not processed yet", by["received"], "#a8a7a1"),
               ("↩️ Payment bounced", by["reopened"], "#ec835a"), ("⛔ Stopped", stopped, "#d03b3b")]
    buckets = [b for b in buckets if b[1]][::-1]
    fig = go.Figure(go.Bar(x=[b[1] for b in buckets], y=[b[0] for b in buckets], orientation="h",
                           marker=dict(color=[b[2] for b in buckets], cornerradius=4), width=0.6,
                           text=[b[1] for b in buckets], textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}: %{x} invoices<extra></extra>"))
    fig.update_layout(height=70 + 46 * len(buckets), margin=dict(l=0, r=40, t=4, b=0),
                      yaxis=dict(showgrid=False), **CHART)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
with right:
    st.subheader("How the money was sent")
    if m.paid_by_rail:
        rails = sorted(m.paid_by_rail.items(), key=lambda kv: kv[1])
        fig = go.Figure(go.Bar(x=[v / 100 for _, v in rails], y=[f"{L.rail(r)[0]} {L.rail(r)[1]}" for r, _ in rails],
                               orientation="h", marker=dict(color=SERIES_1, cornerradius=4), width=0.6,
                               text=[money(v) for _, v in rails], textposition="outside", cliponaxis=False,
                               customdata=[L.rail(r)[2] for r, _ in rails],
                               hovertemplate="%{y}: %{text}<br>%{customdata}<extra></extra>"))
        fig.update_layout(height=70 + 46 * len(rails), margin=dict(l=0, r=100, t=4, b=0),
                          yaxis=dict(showgrid=False), **CHART)
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        st.caption("The agents pick the cheapest way that still arrives on time. Instant payments are used only "
                   "when urgent and low-risk, because they can't be reversed.")
    else:
        st.caption("Nothing sent yet.")

# --- all invoices ---------------------------------------------------------------------------------------------
st.subheader("All invoices")
st.caption("Select a row to see that invoice's full story.")
df = pd.DataFrame(rows)
picked = st.dataframe(df, hide_index=True, width="stretch", column_config=COLUMNS, on_select="rerun",
                      selection_mode="single-row",
                      column_order=["Vendor", "Invoice", "Amount", "Status", "Outcome", "Red flags", "Risk",
                                    "Paid by", "Pay date"])
if picked.selection.rows:
    open_invoice(df.iloc[picked.selection.rows[0]]["id"])

with st.expander("How it works"):
    st.markdown(md("""
1. **Read**: an AI agent reads each invoice PDF or email and pulls out the vendor, amounts, dates and bank details.
   Simple code double-checks it: do the line items add up? Is the bank routing number valid?
2. **Match**: the invoice is compared with its purchase order and with proof the work was done (the "3-way match").
3. **Screen for fraud**: nine fixed checks look for warning signs, such as a changed bank account, a look-alike email
   address, a duplicate or a sanctions match. The AI then explains what it found in plain English.
4. **Decide**: written payment rules, not the AI, decide what happens: pay automatically, ask one or two people,
   hold until someone phones the vendor, or stop.
5. **Pay**: the cheapest way that still arrives on time is chosen (bank transfer, instant payment, wire or card),
   and payments go to the bank in one batch.
6. **Reconcile**: the bank statement is matched to every payment; anything that bounced comes back for follow-up.

Every step, by an agent or a person, is recorded in the **Audit trail**.
"""))
