"""Positive-pay issue file for checks (SPEC §6.6): the bank only clears checks listed here."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

HEADER = ["account_number", "check_number", "amount", "issue_date", "payee"]


@dataclass(frozen=True)
class IssuedCheck:
    check_number: int
    amount_cents: int
    issue_date: date
    payee: str


def build_csv(account_number: str, checks: list[IssuedCheck]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADER)
    for c in checks:
        w.writerow([account_number, c.check_number, f"{c.amount_cents // 100}.{c.amount_cents % 100:02d}",
                    c.issue_date.isoformat(), c.payee])
    return buf.getvalue()
