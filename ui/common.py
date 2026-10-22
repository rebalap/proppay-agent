"""Shared Streamlit plumbing: path setup, cached graph/deps, layout, styles, shared components."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

import labels as L  # noqa: E402
from app import graph as G  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine  # noqa: E402
from app.metrics import run_invoices  # noqa: E402
from app.models import Invoice, Payment, Vendor  # noqa: E402
from app.rules.rails import demo_now  # noqa: E402

USERS = {"alice": "Alice Chen - AP specialist", "bob": "Bob Ortiz - Controller", "dana": "Dana Price - AP manager"}
SERIES_1 = "#2a78d6"  # single-series charts (reference palette slot 1)
money = L.money

CSS = """
<style>
.block-container {padding-top: 2.2rem; max-width: 1280px;}
h1 {letter-spacing: -0.02em;}
.pp-pill {display:inline-block; padding:2px 10px; margin:0 6px 4px 0; border-radius:999px; border:1px solid;
          font-size:0.82rem; font-weight:600; white-space:nowrap;}
.pp-hero {background:linear-gradient(135deg,#eaf2fc 0%,#f6f9fd 60%,#fcfcfb 100%); border:1px solid #d6e4f5;
          border-radius:14px; padding:20px 24px; margin:4px 0 18px 0;}
.pp-hero h3 {margin:0 0 6px 0; font-size:1.25rem;}
.pp-hero p {margin:0; color:#33322f; font-size:1.02rem; line-height:1.55;}
.pp-muted {color:#6b6a66; font-size:0.88rem;}
.pp-steps {display:flex; flex-wrap:wrap; gap:0; margin:6px 0 14px 0;}
.pp-step {flex:1 1 0; min-width:84px; text-align:center; position:relative; padding-top:30px; font-size:0.78rem;
          color:#8a8984;}
.pp-step:before {content:""; position:absolute; top:12px; left:-50%; width:100%; height:3px; background:#dcdad4;
                 z-index:0;}
.pp-step:first-child:before {display:none;}
.pp-step .dot {position:absolute; top:2px; left:50%; transform:translateX(-50%); width:24px; height:24px;
               border-radius:50%; background:#efeeea; border:2px solid #dcdad4; z-index:1; line-height:20px;
               font-size:0.75rem;}
.pp-step.done {color:#0b0b0b;} .pp-step.done .dot {background:#0ca30c; border-color:#0ca30c; color:#fff;}
.pp-step.done:before, .pp-step.now:before, .pp-step.stop:before {background:#0ca30c;}
.pp-step.now {color:#0b0b0b; font-weight:600;} .pp-step.now .dot {background:#fdf3dc; border-color:#eda100;}
.pp-step.stop {color:#b02a2a; font-weight:600;}
.pp-step.stop .dot {background:#d03b3b; border-color:#d03b3b; color:#fff;}
.pp-find {border-left:4px solid #ec835a; background:#fff8f4; padding:8px 12px; border-radius:6px; margin:6px 0;}
.pp-find.critical {border-color:#d03b3b; background:#fdf2f2;}
.pp-find b {display:block; margin-bottom:2px;}
.pp-find .ev {color:#6b6a66; font-size:0.84rem; margin-top:3px;}
.pp-check {margin:3px 0;}
</style>
"""


@st.cache_resource
def runtime():
    s = get_settings()
    data_dir = s.db_path.parent
    bank = httpx.Client(base_url=s.mock_bank_url, timeout=30)
    deps = G.Deps(engine=get_engine(), data_dir=data_dir, now=demo_now(s.demo_run_date, s.demo_run_time),
                  bank=bank, outbox=data_dir / "outbox")
    graph = G.build_graph(deps, G.open_checkpointer(data_dir / "checkpoints.db"))
    return deps, graph


def page(title: str, subtitle: str | None = None) -> None:
    st.set_page_config(page_title=f"PropPay · {title}", page_icon="🏢", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    deps, _ = runtime()
    # Widget state is dropped on page change, so the acting user lives in its own session key.
    st.session_state.setdefault("acting_user", "alice")
    with st.sidebar:
        st.markdown("### 🏢 PropPay")
        st.caption("Bluestem Residential · Accounts payable")
        st.selectbox("You are acting as", list(USERS), format_func=USERS.get, key="_acting_user_widget",
                     index=list(USERS).index(st.session_state["acting_user"]),
                     on_change=lambda: st.session_state.update(acting_user=st.session_state["_acting_user_widget"]),
                     help="Approvals are recorded under this name. Large payments need two different people.")
        st.caption(f"🗓️ Today in the demo: **{deps.now:%A %d %B %Y, %-I:%M %p}**")
        try:
            deps.bank.get("/health", timeout=2).raise_for_status()
            st.caption("🟢 Connected to the (mock) bank")
        except httpx.HTTPError:
            st.error("The mock bank isn't running, so payments can't be sent.\n\n"
                     "`uv run uvicorn mock_bank.main:app --port 8001`")
        with st.expander("Demo controls"):
            st.caption("Puts everything back to the start of the demo: 22 invoices processed overnight, 3 new.")
            if st.button("↺ Reset the demo"):
                reset_demo()
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def reset_demo() -> None:
    from scripts.prepare_demo import restore

    try:
        restore(get_settings().mock_bank_url)
    except SystemExit as exc:
        st.sidebar.error(str(exc))
        return
    for k in [k for k in st.session_state if k != "acting_user"]:
        del st.session_state[k]
    st.toast("Demo reset to Monday 9 AM", icon="↺")
    st.rerun()


def user() -> str:
    return st.session_state.get("acting_user", "alice")


def user_name(code: str | None = None) -> str:
    return USERS.get(code or user(), code or "").split(" - ")[0]


def md(text: str) -> str:
    """Escape '$' so Streamlit markdown doesn't render "$480 ... $50" as LaTeX."""
    return str(text).replace("$", "\\$")


def h(text) -> str:
    """Text placed inside raw HTML: escape it, and use an entity for '$' (a backslash would show)."""
    import html as _html

    return _html.escape(str(text)).replace("$", "&#36;")


def day(value) -> str:
    """'2026-10-29' or a date -> 'Thu 29 Oct'."""
    from datetime import date

    if not value:
        return "not stated"
    d = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{d:%a %d %b}"


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def session() -> Session:
    return Session(runtime()[0].engine)


# --- shared components -------------------------------------------------------------------------------

STEPS = ["Received", "Read by AI", "Matched", "Fraud check", "Decision", "Approved", "Sent to bank", "Settled"]


def journey_html(inv: Invoice, pay: Payment | None) -> str:
    """Stepper: green for steps done, amber for where it's waiting, red ✕ where it stopped."""
    status = inv.status.value
    pay_status = pay.status.value if pay else None
    done = 1 + bool(inv.extracted_json) + bool(inv.match_status) + (inv.risk_score is not None) + bool(inv.decision)
    if status in ("approved", "scheduled", "paid", "reopened") or pay_status in ("sent", "settled", "returned"):
        done = 6
    if pay_status in ("sent", "settled", "returned"):
        done = 7
    if pay_status == "settled":
        done = 8
    stop = {"blocked": min(done + 1, 6), "reopened": 8}.get(status)
    waiting = None if stop else (done + 1 if done < 8 else None)
    parts = []
    for i, label in enumerate(STEPS, start=1):
        if stop == i:
            cls, dot = "stop", "✕"
        elif i <= done and not (stop and i >= stop):
            cls, dot = "done", "✓"
        elif waiting == i and status != "received":
            cls, dot = "now", "…"
        else:
            cls, dot = "", ""
        parts.append(f'<div class="pp-step {cls}"><span class="dot">{dot}</span>{label}</div>')
    return '<div class="pp-steps">' + "".join(parts) + "</div>"


def status_pill(code: str) -> str:
    icon, label, tone, _ = L.status(code)
    return L.pill(label, tone, icon)


def decision_pill(code: str | None) -> str:
    icon, label, tone, _ = L.decision(code)
    return L.pill(label, tone, icon)


def finding_html(sig: dict) -> str:
    icon, title, why = L.signal(sig["code"])
    cls = "pp-find critical" if sig["code"] == "OFAC_HIT" else "pp-find"
    return (f'<div class="{cls}"><b>{icon} {h(title)}</b>{h(why)}'
            f'<div class="ev">Evidence: {h(sig["evidence"])}</div></div>')


def vendor_name(s: Session, inv: Invoice) -> str:
    if inv.vendor_id:
        v = s.get(Vendor, inv.vendor_id)
        if v:
            return v.legal_name
    ext = json.loads(inv.extracted_json) if inv.extracted_json else {}
    return ext.get("vendor_name") or Path(inv.source_file).name


def latest_payment(s: Session, invoice_id: int) -> Payment | None:
    return s.exec(select(Payment).where(Payment.invoice_id == invoice_id).order_by(Payment.id.desc())).first()


def invoice_rows(s: Session) -> list[dict]:
    rows = []
    for inv in run_invoices(s):
        pay = latest_payment(s, inv.id)
        icon, label, _, _ = L.status(inv.status.value)
        r_icon, r_name, _ = L.rail(pay.rail.value if pay else None)
        rows.append({
            "id": inv.id, "Vendor": vendor_name(s, inv), "Invoice": inv.invoice_number or "(not read yet)",
            "Amount": (inv.amount_cents or 0) / 100, "Status": f"{icon} {label}",
            "Outcome": L.decision(inv.decision)[1] if inv.decision else "",
            "Risk": inv.risk_score,
            "Red flags": ", ".join(L.signal(x["code"])[1] for x in json.loads(inv.signals_json or "[]")),
            "Paid by": f"{r_icon} {r_name}" if pay else "", "Pay date": pay.scheduled_date if pay else None,
            "_status": inv.status.value,
        })
    return rows


def invoice_frame(s: Session) -> pd.DataFrame:
    return pd.DataFrame(invoice_rows(s))


COLUMNS = {
    "id": None, "_status": None,
    "Amount": st.column_config.NumberColumn("Amount", format="$%,.2f"),
    "Risk": st.column_config.ProgressColumn("Fraud risk", min_value=0, max_value=100, format="%d",
                                            help="0-20 low · 21-59 medium · 60+ high"),
    "Pay date": st.column_config.DateColumn("Pay date", format="ddd D MMM"),
}


def open_invoice(invoice_id: int) -> None:
    st.session_state["selected_invoice"] = int(invoice_id)
    st.switch_page("pages/3_Invoice_Details.py")
