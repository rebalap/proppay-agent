"""Process new invoices: watch the agents work through each one, live."""

import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlmodel import select

import labels as L
from common import COLUMNS, decision_pill, html, invoice_rows, md, money, open_invoice, page, runtime, session

from app import graph as G
from app.models import Invoice

STEP = {"intake": "📖 read", "match": "🧾 matched", "fraud": "🛡️ fraud-checked", "decide": "⚖️ decided",
        "plan": "💸 payment planned", "callback": "📞 needs a call-back", "approval": "✋ needs approval",
        "finish": "🏁 done", "__interrupt__": "⏸ waiting for a person"}

page("Process new invoices", "New invoices arrive by email or upload. Start the run and watch the agents read, "
                             "check and decide on each one.")
deps, graph = runtime()

with session() as s:
    new = s.exec(select(Invoice).where(Invoice.status == "received").order_by(Invoice.id)).all()
    queued = {i.id: (Path(i.source_file).name, i.channel or ("email" if i.source_file.endswith(".eml") else "pdf"))
              for i in new}

if queued:
    st.markdown(f"#### 📥 {len(queued)} new invoice(s) waiting")
    for name, ch in queued.values():
        st.markdown(f"- {'✉️ Email' if ch == 'email' else '📄 PDF'}: `{name}`")
    st.caption("Each one takes about 20-30 seconds: the AI model runs locally on this laptop.")
else:
    st.success("✅ No new invoices. Everything that arrived has been processed.")

if st.button(f"▶ Start processing ({len(queued)})", type="primary", disabled=not queued):
    started = time.perf_counter()
    with st.status("The agents are working…", expanded=True) as status:
        done: dict[int, list[str]] = {}
        slots = {iid: st.empty() for iid in queued}
        for iid, (name, _) in queued.items():
            slots[iid].markdown(f"⏳ `{name}`: waiting its turn")

        def progress(iid: int, node: str) -> None:
            done.setdefault(iid, []).append(STEP.get(node, node))
            mark = "✅" if node == "finish" else "⚙️"
            slots[iid].markdown(f"{mark} `{queued[iid][0]}`: " + " → ".join(done[iid]))

        summary = G.run_ap(deps, graph, invoice_ids=list(queued), on_progress=progress)
        status.update(label=f"Done: {len(summary.outcomes)} invoice(s) in {time.perf_counter() - started:.0f} s",
                      state="complete", expanded=False)
    with session() as s:
        results = []
        for o in summary.outcomes:
            inv = s.get(Invoice, o.invoice_id)
            ext = json.loads(inv.extracted_json) if inv.extracted_json else {}
            results.append({"id": inv.id, "vendor": ext.get("vendor_name", queued[inv.id][0]),
                            "number": inv.invoice_number, "amount": inv.amount_cents, "decision": inv.decision,
                            "status": inv.status.value,
                            "signals": [x["code"] for x in json.loads(inv.signals_json or "[]")]})
    ex = summary.execution
    st.session_state["run_result"] = {"elapsed": time.perf_counter() - started, "results": results,
                                      "sent": len(ex.sent) if ex else 0}
    st.rerun()

if "run_result" in st.session_state:
    rr = st.session_state["run_result"]
    st.markdown(f"### Results ({rr['elapsed']:.0f} s)")
    for r in rr["results"]:
        with st.container(border=True):
            c = st.columns([4, 1.3], vertical_alignment="center")
            c[0].markdown(md(f"**{r['vendor']}** · {r['number'] or ''} · {money(r['amount'])}"))
            flags = ", ".join(L.signal(x)[1] for x in r["signals"])
            what = L.decision(r["decision"])[3] + (f" Red flags: {flags}." if flags else "")
            with c[0]:
                html(decision_pill(r["decision"]))
                st.caption(md(what))
            if c[1].button("See full story", key=f"story-{r['id']}", width="stretch"):
                open_invoice(r["id"])
    if rr["sent"]:
        st.info(f"🏦 {rr['sent']} payment(s) went to the bank straight away.")
    if any(r["status"] in ("pending_approval", "on_hold") for r in rr["results"]):
        if st.button("Go to the approvals inbox →", type="primary"):
            st.switch_page("pages/2_Approvals.py")

st.markdown("### Everything in this run")
with session() as s:
    df = pd.DataFrame(invoice_rows(s))
if not df.empty:
    picked = st.dataframe(df, hide_index=True, width="stretch", column_config=COLUMNS, on_select="rerun",
                          selection_mode="single-row",
                          column_order=["Vendor", "Invoice", "Amount", "Status", "Outcome", "Red flags", "Paid by",
                                        "Pay date"])
    if picked.selection.rows:
        open_invoice(df.iloc[picked.selection.rows[0]]["id"])

files = sorted(deps.outbox.glob("ach_*.txt")) if deps.outbox.exists() else []
if files:
    st.markdown("### Files sent to the bank")
    st.caption("Bank transfers (ACH) go to the bank as one fixed-format file per run, in the US banking standard "
               "(NACHA). Each line is exactly 94 characters; the invoice number travels with each payment.")
    for f in files:
        entries = sum(1 for ln in f.read_text().splitlines() if ln.startswith("6"))
        with st.expander(f"📄 {f.name}: {entries} payment(s)"):
            st.code(f.read_text(), language=None)
