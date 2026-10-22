"""Prepare the 5-minute demo: process the "overnight" batch ahead of time, keep a few invoices
for the live run, and snapshot everything so rehearsals reset in seconds.

    uv run python scripts/prepare_demo.py              # seed + overnight batch (~7 min) + snapshot
    uv run python scripts/prepare_demo.py --restore    # back to the demo starting point (seconds)

Default live invoices ("arrived Monday morning"): the BEC email (case 2), the emergency HVAC
repair paid by RTP (case 4) and the sanctions hit (case 9). Needs Ollama + the mock bank.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
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

DATA = ROOT / "data"
SNAP = DATA / "demo_snapshot"
SQLITE_FILES = ["proppay.db", "checkpoints.db"]


def sqlite_copy(src: Path, dst: Path) -> None:
    """Online backup: safe even while the UI holds the destination open (same file, same inode)."""
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)


def snapshot() -> None:
    SNAP.mkdir(exist_ok=True)
    for f in SQLITE_FILES:
        sqlite_copy(DATA / f, SNAP / f)
    shutil.copy(DATA / "mock_bank_state.json", SNAP / "mock_bank_state.json")
    shutil.rmtree(SNAP / "outbox", ignore_errors=True)
    if (DATA / "outbox").exists():
        shutil.copytree(DATA / "outbox", SNAP / "outbox")


def restore(bank_url: str) -> None:
    if not SNAP.exists():
        sys.exit("no snapshot yet: run scripts/prepare_demo.py first")
    for f in SQLITE_FILES:
        sqlite_copy(SNAP / f, DATA / f)
    shutil.copy(SNAP / "mock_bank_state.json", DATA / "mock_bank_state.json")
    shutil.rmtree(DATA / "outbox", ignore_errors=True)
    if (SNAP / "outbox").exists():
        shutil.copytree(SNAP / "outbox", DATA / "outbox")
    try:
        httpx.post(f"{bank_url}/admin/reload", timeout=3).raise_for_status()
        print("restored; mock bank reloaded")
    except httpx.HTTPError:
        print("restored; start the mock bank (it will load the restored state)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--live-cases", default="2,4,9", help="demo cases left for the live run")
    args = ap.parse_args()
    settings = get_settings()
    if args.restore:
        restore(settings.mock_bank_url)
        return

    try:
        httpx.get(f"{settings.mock_bank_url}/health", timeout=3).raise_for_status()
    except httpx.HTTPError:
        sys.exit("mock bank is not running: uv run uvicorn mock_bank.main:app --port 8001")

    import subprocess

    shutil.rmtree(DATA / "outbox", ignore_errors=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_data.py")], check=True, stdout=subprocess.DEVNULL)
    live = {int(c) for c in args.live_cases.split(",") if c}
    engine = get_engine()
    with Session(engine) as s:
        batch = [i.id for i in s.exec(select(Invoice).where(Invoice.status == "received").order_by(Invoice.id)).all()
                 if i.demo_case not in live]
    started = time.perf_counter()
    with httpx.Client(base_url=settings.mock_bank_url, timeout=30) as bank:
        deps = G.Deps(engine=engine, data_dir=DATA, now=demo_now(settings.demo_run_date, settings.demo_run_time),
                      bank=bank, outbox=DATA / "outbox")
        graph = G.build_graph(deps, G.open_checkpointer(DATA / "checkpoints.db"))
        summary = G.run_ap(deps, graph, invoice_ids=batch,
                           on_progress=lambda iid, node: node == "finish" and print(f"  invoice {iid} done", flush=True))
    waiting = [o.invoice_id for o in summary.outcomes if o.waiting_for]
    print(f"overnight batch: {len(batch)} invoices in {time.perf_counter() - started:.0f}s, "
          f"{len(summary.execution.sent)} sent, waiting for humans: {waiting}; live cases left: {sorted(live)}")
    snapshot()
    print(f"snapshot saved to {SNAP.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
