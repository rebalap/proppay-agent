"""Invoice sections shared by the Invoice details page and the approvals side panels."""

from __future__ import annotations

import json

import streamlit as st
from sqlmodel import Session, or_, select

import labels as L
from common import USERS, day, html, md, money
from app.models import Approval, AuditLog, BankChangeRequest, Invoice, Payment


def checks(inv: Invoice) -> list[tuple[str, bool | None]]:
    """(plain-English check, True passed / False failed / None minor)."""
    ext = json.loads(inv.extracted_json) if inv.extracted_json else None
    match = json.loads(inv.match_json) if inv.match_json else None
    codes = {i["code"] for i in json.loads(inv.intake_issues_json or "[]")}
    signals = json.loads(inv.signals_json or "[]")
    c = (match or {}).get("checks", {})
    out: list[tuple[str, bool | None]] = []
    if ext:
        out.append(("Line items add up to the total", "LINE_ITEMS_MISMATCH" not in codes))
        if ext.get("remit_to_bank"):
            out.append(("Bank routing number is a real format", "INVALID_ROUTING" not in codes))
            out.append(("Account number looks complete", "INVALID_ACCOUNT" not in codes))
    labels = [("vendor_resolved", "We know this vendor"), ("po_referenced", "Quotes a purchase order"),
              ("po_found", "Purchase order exists"), ("po_vendor_matches", "Purchase order is for this vendor"),
              ("po_open", "Purchase order hasn't been billed already"),
              ("amount_within_tolerance", "Amount within 2% of the purchase order"),
              ("work_order_completed", "The work was marked complete")]
    for key, text in labels:
        if key in c:
            out.append((text, c[key]))
    if inv.risk_score is not None:
        level, tone = L.risk_level(inv.risk_score)
        if not signals:
            out.append(("Fraud screening: no red flags", True))
        else:
            out.append((f"Fraud screening: {len(signals)} red flag(s), {level.lower()}", None if tone == "good" else False))
    return out


def render_checks(inv: Invoice) -> None:
    items = checks(inv)
    if not items:
        st.caption("Nothing checked yet.")
    for text, ok in items:
        html(f"<div class='pp-check'>{ {True: '✅', False: '❌', None: '⚠️'}[ok] } {md(text)}</div>")


def render_read(inv: Invoice, compact: bool = False) -> None:
    ext = json.loads(inv.extracted_json) if inv.extracted_json else None
    if not ext:
        st.caption("Not read yet.")
        return
    bank = ext.get("remit_to_bank")
    fields = [
        ("Vendor", ext["vendor_name"]), ("Invoice number", ext["invoice_number"]),
        ("Invoice date", day(ext["invoice_date"])), ("Due date", day(ext.get("due_date"))),
        ("Amount", money(inv.amount_cents)), ("Payment terms", ext.get("terms") or "not stated"),
        ("Purchase order", ext.get("po_number") or "none"),
        ("Pay into", f"routing {bank['routing']}, account ending {bank['account_last4']}" if bank
         else "not stated (we use the account on file)"),
        ("Arrived by", f"email from {inv.sender_email}" if inv.sender_email else "PDF upload"),
    ]
    if compact:
        cols = st.columns(3)
        for i, (k, v) in enumerate(fields):
            cols[i % 3].markdown(md(f"<span class='pp-muted'>{k}</span><br>**{v}**"), unsafe_allow_html=True)
    else:
        for k, v in fields:
            st.markdown(md(f"<span class='pp-muted'>{k}</span><br>**{v}**"), unsafe_allow_html=True)
    if ext.get("pressure_language"):
        st.warning(md(f"Pressure to pay: “{ext['pressure_language']}”"))
    items = ext.get("line_items") or []
    if items and not compact:
        with st.expander(f"{len(items)} line item(s)"):
            for li in items:
                st.markdown(md(f"- {li['description']}: ${float(li['amount']):,.2f}"))


def timeline_events(s: Session, invoice_id: int) -> list[AuditLog]:
    pay_ids = [p.id for p in s.exec(select(Payment).where(Payment.invoice_id == invoice_id)).all()]
    appr_ids = [a.id for a in s.exec(select(Approval).where(Approval.payment_id.in_(pay_ids))).all()] if pay_ids else []
    req_ids = [r.id for r in s.exec(select(BankChangeRequest).where(BankChangeRequest.invoice_id == invoice_id)).all()]
    conds = [(AuditLog.entity == "invoices") & (AuditLog.entity_id == invoice_id)]
    for ent, ids in (("payments", pay_ids), ("approvals", appr_ids), ("bank_change_requests", req_ids)):
        if ids:
            conds.append((AuditLog.entity == ent) & (AuditLog.entity_id.in_(ids)))
    return s.exec(select(AuditLog).where(or_(*conds)).order_by(AuditLog.ts, AuditLog.id)).all()


def render_timeline(s: Session, invoice_id: int) -> None:
    prev = None
    events = timeline_events(s, invoice_id)
    if not events:
        st.caption("Nothing has happened yet.")
    for e in events:
        what = L.action(e.action)
        if e.action == "decide" and prev == "plan_rail":
            what = "re-checked the decision for the chosen payment method"
        prev = e.action
        st.markdown(md(f"**{L.actor(e.actor, USERS)}** {what}  \n"
                       f"<span class='pp-muted'>{e.ts:%H:%M:%S} UTC · {L.audit_reason(e.action, e.reason)}</span>"),
                    unsafe_allow_html=True)
