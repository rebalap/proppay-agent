"""Simplified BAI2 statement parser (SPEC §6.8).

Supports 01 file header, 02 group header, 03 account identifier, 16 transaction detail,
88 continuation, 49 account trailer, 98 group trailer, 99 file trailer. Control totals
and record counts are checked so a truncated or edited statement is caught before it is
reconciled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class StatementLine:
    as_of: date
    account: str
    type_code: str
    amount_cents: int
    bank_reference: str
    customer_reference: str
    detail: str = ""  # from 88 continuation records

    @property
    def return_code(self) -> str | None:
        m = re.search(r"\bRETURN (R\d{2})\b", self.detail)
        return m.group(1) if m else None


def _fields(record: str) -> list[str]:
    return record.rstrip().rstrip("/").split(",")


def parse(text: str) -> tuple[list[StatementLine], list[str]]:
    records = [r for r in text.splitlines() if r.strip()]
    lines: list[StatementLine] = []
    errors: list[str] = []
    if not records or not records[0].startswith("01,"):
        return [], ["missing 01 file header"]
    as_of: date | None = None
    account = ""
    acct_records = acct_total = group_total = 0

    for rec in records:
        f = _fields(rec)
        code = f[0]
        if code == "01":
            continue
        if code == "02":
            as_of = datetime.strptime(f[4], "%y%m%d").date()
        elif code == "03":
            account, acct_records, acct_total = f[1], 1, 0
        elif code == "16":
            acct_records += 1
            amount = int(f[2])
            acct_total += amount
            lines.append(StatementLine(as_of, account, f[1], amount, f[4], f[5] if len(f) > 5 else ""))
        elif code == "88":
            acct_records += 1
            if lines:
                lines[-1].detail = (lines[-1].detail + " " + ",".join(f[1:])).strip()
        elif code == "49":
            acct_records += 1
            if int(f[1]) != acct_total:
                errors.append(f"account {account}: 49 control total {f[1]} != {acct_total}")
            if int(f[2]) != acct_records:
                errors.append(f"account {account}: 49 record count {f[2]} != {acct_records}")
            group_total += acct_total
        elif code == "98":
            if int(f[1]) != group_total:
                errors.append(f"98 group total {f[1]} != {group_total}")
        elif code == "99":
            if int(f[1]) != group_total:
                errors.append(f"99 file total {f[1]} != {group_total}")
            if int(f[3]) != len(records):
                errors.append(f"99 record count {f[3]} != {len(records)}")
        else:
            errors.append(f"unknown record type {code}")
    if not records[-1].startswith("99,"):
        errors.append("missing 99 file trailer (truncated statement?)")
    return lines, errors
