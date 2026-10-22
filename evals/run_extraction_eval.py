"""Invoice-extraction eval: run the intake agent over the gold set and score field by field.

    uv run python evals/run_extraction_eval.py                 # model from OLLAMA_MODEL
    uv run python evals/run_extraction_eval.py --model qwen3:14b
    uv run python evals/run_extraction_eval.py --limit 5 --verbose

Per field: accuracy (exact match, null == null counts), precision and recall (a prediction
is "positive" when non-null / true). Results are written to evals/results/.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGET_ACCURACY = 0.90


def _norm_text(s):
    return " ".join(str(s).lower().split()) if s is not None else None


def predicted_fields(ext) -> dict:
    from app.rules.names import normalize_name

    if ext is None:
        return {}
    rb = ext.remit_to_bank
    return {
        "vendor_name": normalize_name(ext.vendor_name, drop_suffixes=False) or None,
        "invoice_number": ext.invoice_number.strip().upper() or None,
        "invoice_date": ext.invoice_date.isoformat(),
        "due_date": ext.due_date.isoformat() if ext.due_date else None,
        "amount": ext.amount_cents,
        "terms": _norm_text(ext.terms),
        "po_number": ext.po_number.strip().upper() if ext.po_number else None,
        "remit_routing": rb.routing if rb else None,
        "remit_last4": rb.account_last4 if rb else None,
        "email_mentions_bank_change": ext.email_mentions_bank_change,
        "line_items_sum": ext.line_items_total_cents if ext.line_items else None,
    }


def truth_fields(t: dict) -> dict:
    from app.rules.names import normalize_name
    from app.schemas import to_cents

    rb = t["remit_to_bank"]
    return {
        "vendor_name": normalize_name(t["vendor_name"], drop_suffixes=False),
        "invoice_number": t["invoice_number"].upper(),
        "invoice_date": t["invoice_date"],
        "due_date": t["due_date"],
        "amount": to_cents(t["amount"]),
        "terms": _norm_text(t["terms"]),
        "po_number": t["po_number"].upper() if t["po_number"] else None,
        "remit_routing": rb["routing"] if rb else None,
        "remit_last4": rb["account_last4"] if rb else None,
        "email_mentions_bank_change": t["email_mentions_bank_change"],
        "line_items_sum": to_cents(t["amount"]),  # extracted line items must add up to the true total
    }


FIELDS = list(truth_fields({"vendor_name": "x", "invoice_number": "x", "invoice_date": "", "due_date": None,
                             "amount": "0", "terms": None, "po_number": None, "remit_to_bank": None,
                             "email_mentions_bank_change": False}))


def _positive(v) -> bool:
    return v is not None and v is not False


def score(rows: list[dict]) -> dict:
    out = {}
    for f in FIELDS:
        tp = fp = fn = correct = 0
        for r in rows:
            p, t = r["pred"].get(f), r["truth"][f]
            ok = p == t
            correct += ok
            if _positive(p) and ok and _positive(t):
                tp += 1
            else:
                fp += _positive(p) and not (ok and _positive(t))
                fn += _positive(t) and not ok
        out[f] = {
            "accuracy": correct / len(rows),
            "precision": tp / (tp + fp) if tp + fp else 1.0,
            "recall": tp / (tp + fn) if tp + fn else 1.0,
            "errors": [r["id"] for r in rows if r["pred"].get(f) != r["truth"][f]],
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="override OLLAMA_MODEL")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", nargs="*", help="gold ids to run")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if args.model:
        os.environ["OLLAMA_MODEL"] = args.model

    from app.agents.intake import run_intake
    from app.config import get_settings

    model = get_settings().ollama_model
    gold = [json.loads(line) for line in (ROOT / "evals" / "gold_invoices.jsonl").read_text().splitlines()]
    if args.only:
        gold = [g for g in gold if g["id"] in args.only]
    gold = gold[: args.limit] if args.limit else gold

    rows = []
    for i, g in enumerate(gold, 1):
        t0 = time.perf_counter()
        res = run_intake(ROOT / "data" / g["source"])
        secs = time.perf_counter() - t0
        row = {
            "id": g["id"], "template": g["template"], "channel": g["channel"], "seconds": round(secs, 1),
            "attempts": res.attempts, "needs_review": res.needs_review,
            "issues": [i.code for i in res.issues],
            "pred": predicted_fields(res.extraction), "truth": truth_fields(g["truth"]),
        }
        rows.append(row)
        wrong = [f for f in FIELDS if row["pred"].get(f) != row["truth"][f]]
        print(f"[{i:2}/{len(gold)}] {g['id']:<32} {secs:5.1f}s  attempts={res.attempts}  "
              f"{'OK' if not wrong else 'wrong: ' + ', '.join(wrong)}", flush=True)
        if args.verbose and wrong:
            for f in wrong:
                print(f"        {f}: got {row['pred'].get(f)!r}  want {row['truth'][f]!r}")

    per_field = score(rows)
    cells = len(rows) * len(FIELDS)
    field_acc = sum(per_field[f]["accuracy"] for f in FIELDS) * len(rows) / cells
    doc_exact = sum(all(r["pred"].get(f) == r["truth"][f] for f in FIELDS) for r in rows) / len(rows)
    secs = [r["seconds"] for r in rows]

    print(f"\nModel: {model}   docs: {len(rows)}   fields/doc: {len(FIELDS)}\n")
    print(f"| {'field':<27} | accuracy | precision | recall |")
    print(f"|{'-' * 29}|----------|-----------|--------|")
    for f in FIELDS:
        m = per_field[f]
        print(f"| {f:<27} | {m['accuracy']:8.1%} | {m['precision']:9.1%} | {m['recall']:6.1%} |")
    print(f"\nOverall field accuracy: {field_acc:.1%}  (target {TARGET_ACCURACY:.0%})")
    print(f"Documents fully correct: {doc_exact:.1%}")
    print(f"Routed to human review: {sum(r['needs_review'] for r in rows)}/{len(rows)}   "
          f"retries: {sum(r['attempts'] > 1 for r in rows)}")
    print(f"Latency: median {statistics.median(secs):.1f}s, max {max(secs):.1f}s, total {sum(secs) / 60:.1f} min")
    print("End-to-end decision accuracy: see evals/run_decision_eval.py")

    out = ROOT / "evals" / "results" / f"extraction_{model.replace(':', '-')}_{datetime.now():%Y%m%d-%H%M}.json"
    out.write_text(json.dumps({"model": model, "field_accuracy": field_acc, "doc_exact": doc_exact,
                               "per_field": per_field, "rows": rows}, indent=2, default=str))
    print(f"Saved {out.relative_to(ROOT)}")
    sys.exit(0 if field_acc >= TARGET_ACCURACY else 1)


if __name__ == "__main__":
    main()
