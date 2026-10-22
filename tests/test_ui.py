"""Smoke-test every Streamlit page against a real demo state (pending approvals, a bounced payment)."""

import os
import sys
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from app import graph as G
from tests.test_graph import env  # noqa: F401  (fixture)
from tests.test_reconcile import THROUGH, full_run

UI = Path(__file__).resolve().parent.parent / "ui"
PAGES = ["Home.py", "pages/1_Payment_Run.py", "pages/2_Approvals.py", "pages/3_Invoice_Details.py",
         "pages/4_Vendors.py", "pages/5_Audit_Trail.py", "pages/6_Reconciliation.py"]


@pytest.fixture
def ui_env(env, monkeypatch):
    from app import config, db

    monkeypatch.setenv("DB_PATH", str(env["deps"].data_dir / "seed.db"))
    monkeypatch.setenv("MOCK_BANK_URL", "http://127.0.0.1:9")  # unreachable: pages must cope
    monkeypatch.syspath_prepend(str(UI))
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    st.cache_resource.clear()
    yield env
    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    st.cache_resource.clear()
    for mod in ("common", "labels"):
        sys.modules.pop(mod, None)


def render(page: str, **state) -> AppTest:
    at = AppTest.from_file(str(UI / page), default_timeout=30)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    assert not at.exception, f"{page}: {[e.message for e in at.exception]}"
    return at


def text(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown) + " " + " ".join(str(c.value) for c in at.caption)


def test_every_page_renders_mid_run(ui_env):
    G.run_ap(ui_env["deps"], ui_env["graph"])
    for page in PAGES:
        render(page)
    inbox = text(render("pages/2_Approvals.py"))
    assert "New bank details by email" in inbox and "Possible duplicate" in inbox and "Large payment by wire" in inbox
    assert "auto_pay." not in inbox and "BANK_DETAILS_CHANGED" not in inbox  # no raw codes for humans
    home = text(render("Home.py"))
    assert "3 invoices need a decision from you" in home


def test_pages_after_approvals_and_reconciliation(ui_env):
    full_run(ui_env)
    G.reconcile_through(ui_env["deps"], THROUGH)
    for page in PAGES:
        render(page)
    keystone = ui_env["case"][7][0]
    story = text(render("pages/3_Invoice_Details.py", selected_invoice=keystone))
    assert "bounced" in story and "No account / unable to locate account" in story
    assert "You're all caught up" in " ".join(s.value for s in render("pages/2_Approvals.py").success)


def test_ui_modules_import_like_streamlit_does(tmp_path):
    """Streamlit only puts ui/ on sys.path; every module must find the project root itself."""
    import subprocess

    code = f"import sys; sys.path.insert(0, {str(UI)!r}); import labels, common; print('ok')"
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr


# --- driving the approvals inbox -----------------------------------------------------------------------------------


def inbox(**state) -> AppTest:
    return render("pages/2_Approvals.py", **state)


def press(at: AppTest, key: str | None = None, label: str | None = None) -> AppTest:
    btn = at.button(key) if key else next(b for b in at.button if b.label == label)
    btn.click().run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def test_dual_approval_through_the_table(ui_env):
    G.run_ap(ui_env["deps"], ui_env["graph"])
    gc = ui_env["case"][5][0]
    at = press(inbox(acting_user="alice"), key=f"ok-{gc}")  # row ✓ opens the confirmation
    assert any("approval 1 of 2" in i.value for i in at.info)
    press(at, label="Approve $480,000.00")
    assert any("Approval 1 of 2 recorded" in s.value for s in at.success)
    assert at.button(f"ok-{gc}").disabled  # alice can't approve twice
    at = press(inbox(acting_user="bob"), key=f"ok-{gc}")
    press(at, label="Approve $480,000.00")
    # the test points the UI at an unreachable bank: the approval stands, and the message says what happened
    assert any("Approved, but the bank couldn't be reached" in w.value for w in at.warning)


def test_reject_needs_a_reason(ui_env):
    G.run_ap(ui_env["deps"], ui_env["graph"])
    dup = ui_env["case"][3][0]
    at = press(inbox(), key=f"no-{dup}")
    assert at.radio[0].value == "Duplicate of an invoice we already paid"  # preselected for duplicates
    at.radio[0].set_value("Other (explain below)").run()
    assert next(b for b in at.button if b.label == "Reject payment").disabled  # "Other" needs details
    at.text_input[0].set_value("Vendor sent this twice by mistake").run()
    press(at, label="Reject payment")
    assert any("Rejected." in w.value for w in at.warning)


def test_callback_from_the_table_stops_bec(ui_env):
    G.run_ap(ui_env["deps"], ui_env["graph"])
    bec = ui_env["case"][2][0]
    at = press(inbox(acting_user="dana"), key=f"cb-{bec}")
    at.radio[0].set_value("They did NOT change banks").run()
    press(at, label="⛔ Stop this payment")
    assert any("Payment stopped" in w.value for w in at.warning)


def test_details_panel_opens(ui_env):
    G.run_ap(ui_env["deps"], ui_env["graph"])
    at = press(inbox(), key=f"det-{ui_env['case'][2][0]}")
    body = text(at)
    assert "Why it's here" in body and "Red flags" in body and "AI analyst" in body
