"""End-to-end decision eval: live LLM intake -> match -> fraud -> rules on a fresh seed,
scored against each demo case's expected decision (SPEC §11).

    uv run python evals/run_decision_eval.py          # ~8 min on qwen3:8b
    uv run python evals/run_decision_eval.py --model qwen3:14b

Seeds into a temp directory, so the working DB is untouched. Also re-scores extraction
on the same run so a prompt change can't silently cost accuracy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--cases", nargs="*", type=int, help="only these demo cases")
    args = ap.parse_args()
    if args.model:
        os.environ["OLLAMA_MODEL"] = args.model

    from sqlmodel import Session

    from app.agents.decision import run_decision
    from app.agents.fraud import run_fraud
    from app.agents.intake import process_invoice
    from app.agents.matcher import run_match
    from app.config import get_settings
    from app.db import make_engine
    from app.models import Invoice
    from run_extraction_eval import FIELDS, predicted_fields, truth_fields
    from scripts.seed_data import seed

    settings = get_settings()
    tmp = Path(tempfile.mkdtemp(prefix="proppay-eval-"))
    engine = make_engine(f"sqlite:///{tmp / 'eval.db'}")
    seed(engine, tmp, settings.demo_run_date)
    manifest = [json.loads(line) for line in (tmp / "demo_manifest.jsonl").read_text().splitlines()]
    if args.cases:
        manifest = [m for m in manifest if m["demo_case"] in args.cases]

    rows = []
    with Session(engine) as s:
        for i, m in enumerate(manifest, 1):
            t0 = time.perf_counter()
            intake = process_invoice(s, m["invoice_id"], tmp)
            run_match(s, m["invoice_id"])
            fraud = run_fraud(s, m["invoice_id"], today=settings.demo_run_date, data_dir=tmp)
            decision = run_decision(s, m["invoice_id"])
            s.commit()
            secs = time.perf_counter() - t0

            exp = m["expected"]
            got_signals = {x.code for x in fraud.signals}
            urgency = "URGENCY_LANGUAGE" in got_signals
            want_urgency = exp.get("urgency")
            urgency_ok = want_urgency == "either" or urgency == (want_urgency == "required")
            pred, truth = predicted_fields(intake.extraction), truth_fields(m["truth"])
            row = {
                "file": m["file"], "case": m["demo_case"], "seconds": round(secs, 1),
                "expected": exp["decision"], "got": decision.outcome.value,
                "decision_ok": decision.outcome.value == exp["decision"],
                "signals_ok": got_signals - {"URGENCY_LANGUAGE"} == set(exp["signals"]) and urgency_ok,
                "signals": sorted(got_signals), "expected_signals": exp["signals"], "urgency": urgency,
                "risk": fraud.risk_score, "reasons": decision.reasons,
                "rationale": fraud.rationale.summary if fraud.rationale else fraud.rationale_error,
                "field_errors": [f for f in FIELDS if pred.get(f) != truth[f]],
                "pressure_quote": intake.extraction.pressure_language if intake.extraction else None,
            }
            rows.append(row)
            flag = "OK " if row["decision_ok"] and row["signals_ok"] else "XX "
            print(f"{flag}[{i:2}/{len(manifest)}] case {m['demo_case']} {m['file']:<30} {secs:5.1f}s  "
                  f"{row['got']:<16} risk {row['risk']:3}  {','.join(row['signals']) or '-'}", flush=True)
            if not (row["decision_ok"] and row["signals_ok"]):
                print(f"      expected {exp['decision']} with {exp['signals']} (urgency {want_urgency or 'none'})"
                      f"; reasons: {decision.reasons}; field errors: {row['field_errors']}")

    n = len(rows)
    dec_acc = sum(r["decision_ok"] for r in rows) / n
    sig_acc = sum(r["signals_ok"] for r in rows) / n
    field_acc = 1 - sum(len(r["field_errors"]) for r in rows) / (n * len(FIELDS))
    stp = sum(r["got"] == "AUTO_APPROVE" for r in rows) / n
    print(f"\nModel: {settings.ollama_model}   invoices: {n}")
    print(f"Decision accuracy:   {dec_acc:.1%}")
    print(f"Signal accuracy:     {sig_acc:.1%}")
    print(f"Extraction accuracy: {field_acc:.1%}")
    print(f"STP (auto-approved): {stp:.1%}   decisions: {dict(Counter(r['got'] for r in rows))}")
    print(f"Urgency flagged on:  {[r['file'] for r in rows if r['urgency']]}")
    print(f"Total time: {sum(r['seconds'] for r in rows) / 60:.1f} min")
    for r in rows:
        if r["rationale"]:
            print(f"\n[{r['file']}] rationale: {r['rationale']}")

    out = ROOT / "evals" / "results" / f"decisions_{settings.ollama_model.replace(':', '-')}_{datetime.now():%Y%m%d-%H%M}.json"
    out.write_text(json.dumps({"model": settings.ollama_model, "decision_accuracy": dec_acc,
                               "signal_accuracy": sig_acc, "extraction_accuracy": field_acc, "stp": stp,
                               "rows": rows}, indent=2, default=str))
    print(f"\nSaved {out.relative_to(ROOT)}")
    sys.exit(0 if dec_acc == 1.0 else 1)


if __name__ == "__main__":
    main()
