"""Build evals/gold_invoices.jsonl from the seeded manifest (ground truth is known by construction).

    uv run python scripts/seed_data.py && uv run python evals/build_gold.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    manifest = ROOT / "data" / "demo_manifest.jsonl"
    gold = ROOT / "evals" / "gold_invoices.jsonl"
    with gold.open("w") as out:
        for line in manifest.read_text().splitlines():
            m = json.loads(line)
            out.write(json.dumps({
                "id": Path(m["file"]).stem,
                "source": m["email_file"] or f"invoices/{m['file']}",  # relative to data/
                "template": m["doc"]["template"],
                "channel": m["channel"],
                "demo_case": m["demo_case"],
                "truth": m["truth"],
            }, sort_keys=True) + "\n")
    print(f"wrote {gold.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
