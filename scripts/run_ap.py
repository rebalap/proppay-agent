"""Command-line driver for Monday's AP run (the Streamlit UI uses the same functions).

    uv run python scripts/run_ap.py run                       # process every received invoice, send what's released
    uv run python scripts/run_ap.py pending                   # what is waiting for a human
    uv run python scripts/run_ap.py approve 168 --as alice    # approve (run again --as bob for dual control)
    uv run python scripts/run_ap.py reject 168 --as alice --reason "duplicate"
    uv run python scripts/run_ap.py callback 160 --as dana --rejected --reason "vendor never changed banks"
    uv run python scripts/run_ap.py callback 160 --as dana --verified --account 851656049451
    uv run python scripts/run_ap.py reconcile --through 2026-11-20   # pull statements, match, handle returns

Needs Ollama and the mock bank (uv run uvicorn mock_bank.main:app --port 8001).
State lives in data/proppay.db and data/checkpoints.db, so every command can run in a new process.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import graph as G  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine  # noqa: E402
from app.models import Invoice  # noqa: E402
from app.rules.rails import demo_now  # noqa: E402


def deps_and_graph(bank: httpx.Client):
    s = get_settings()
    deps = G.Deps(engine=get_engine(), data_dir=ROOT / "data", now=demo_now(s.demo_run_date, s.demo_run_time),
                  bank=bank, outbox=ROOT / "data" / "outbox")
    return deps, G.build_graph(deps, G.open_checkpointer(ROOT / "data" / "checkpoints.db"))


def money(cents) -> str:
    return f"${cents / 100:,.2f}" if cents is not None else "?"


def print_report(summary: G.RunSummary) -> None:
    ex = summary.execution
    if ex:
        print(f"\nsent to bank: {len(ex.sent)}   failed: {ex.failed or 'none'}")
        if ex.ach_file:
            print(f"NACHA file: {Path(ex.ach_file).relative_to(ROOT)}")


def cmd_run(deps, graph, _args) -> None:
    started, current = time.perf_counter(), {}

    def progress(iid: int, node: str) -> None:
        if current.get("id") != iid:
            current.update(id=iid, t=time.perf_counter())
            print(f"\ninvoice {iid}: ", end="", flush=True)
        print(f"{node} ", end="", flush=True)

    summary = G.run_ap(deps, graph, on_progress=progress)
    print("\n")
    for o in summary.outcomes:
        print(f"  invoice {o.invoice_id:>4}  {o.decision or '-':<16} {o.waiting_for or o.status}")
    print_report(summary)
    print(f"total {time.perf_counter() - started:.0f}s")


def all_ids(deps) -> list[int]:
    with Session(deps.engine) as s:
        return [i.id for i in s.exec(select(Invoice)).all()]


def cmd_pending(deps, graph, _args) -> None:
    actions = G.pending_actions(graph, all_ids(deps))
    if not actions:
        print("nothing waiting")
    for a in actions:
        print(f"\n[{a['kind'].upper()}] invoice {a['invoice_id']}: {a['vendor']} {a['invoice_number']} "
              f"{money(a['amount_cents'])}  risk {a['risk_score']}")
        print(f"  decision {a['decision']}: {'; '.join(a['decision_reasons'])}")
        for sig in a["signals"]:
            print(f"  ! {sig['code']}: {sig['evidence']}")
        if a.get("rationale"):
            print(f"  analyst: {a['rationale']['summary']}")
        if a["kind"] == "approval":
            print(f"  pay {money(a['pay_amount_cents'])} by {a['rail']} on {a['send_date']} "
                  f"({a['required_level']} approval; so far {[x['approver'] for x in a['approvals_so_far']]})")
            if a.get("rail_explanation"):
                print(f"  rail: {a['rail_explanation']}")
        else:
            print(f"  requested bank: {a['new_routing']} / ****{a['new_account_last4']}\n  {a['instructions']}")
        if a.get("note"):
            print(f"  note: {a['note']}")


def answer(deps, graph, invoice_id: int, payload: dict) -> None:
    summary = G.resume(deps, graph, invoice_id, payload)
    o = summary.outcomes[0]
    print(f"invoice {o.invoice_id}: {o.waiting_for and 'still waiting for ' + o.waiting_for or o.status}"
          + (f" ({o.note})" if o.note else ""))
    print_report(summary)


def cmd_reconcile(deps, _graph, args) -> None:
    from datetime import date

    from app.agents.reconciler import open_exceptions

    r = G.reconcile_through(deps, date.fromisoformat(args.through))
    print(f"statements: {r.days} days, {r.lines_new} new lines, {r.matched} matched, {len(r.settled)} settled, "
          f"{len(r.returned)} returned, unmatched lines: {r.unmatched_lines}")
    if r.statement_errors:
        print(f"statement errors: {r.statement_errors}")
    if r.trusted_now:
        print(f"now trusted: {', '.join(r.trusted_now)}")
    with Session(deps.engine) as s:
        for e in open_exceptions(s):
            print(f"\n[EXCEPTION {e['id']}] {e['kind']}: {e['detail']}")
            if e["draft_notice"]:
                print(f"  draft notice (not sent): {e['draft_notice']['subject']}\n  "
                      + e["draft_notice"]["body"].replace("\n", "\n  "))
    print("\nfully matched" if r.fully_matched else "\nNOT fully matched")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("pending")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("invoice_id", type=int)
        p.add_argument("--as", dest="who", required=True)
        p.add_argument("--reason")
    p = sub.add_parser("callback")
    p.add_argument("invoice_id", type=int)
    p.add_argument("--as", dest="who", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--verified", action="store_true")
    g.add_argument("--rejected", action="store_true")
    p.add_argument("--account", help="full account number confirmed on the call")
    p.add_argument("--reason")
    p = sub.add_parser("reconcile")
    p.add_argument("--through", required=True, help="last statement date, YYYY-MM-DD")
    args = ap.parse_args()

    with httpx.Client(base_url=get_settings().mock_bank_url, timeout=30) as bank:
        deps, graph = deps_and_graph(bank)
        if args.cmd == "run":
            cmd_run(deps, graph, args)
        elif args.cmd == "pending":
            cmd_pending(deps, graph, args)
        elif args.cmd == "reconcile":
            cmd_reconcile(deps, graph, args)
        elif args.cmd in ("approve", "reject"):
            answer(deps, graph, args.invoice_id, {"approver": args.who, "decision": args.cmd, "reason": args.reason})
        else:
            answer(deps, graph, args.invoice_id, {"verified": args.verified, "by": args.who,
                                                  "account_number": args.account, "note": args.reason})


if __name__ == "__main__":
    main()
