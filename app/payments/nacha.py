"""NACHA CCD+ file builder and validator (SPEC §6.6).

Records are 94-character fixed width:
  1 file header, 5 batch header (SEC CCD), 6 entry detail (22 checking / 32 savings credit),
  7 addenda type 05 (CCD+ remittance), 8 batch control, 9 file control, then 9-filled
  padding lines up to a multiple of 10 records (blocking factor 10).
Entry hash = sum of the first 8 digits of every RDFI routing number, rightmost 10 digits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

RECORD_LEN = 94
BLOCKING_FACTOR = 10
SERVICE_CLASS_CREDITS = "220"
TXN_CODES = {"checking": "22", "savings": "32"}
_ALLOWED = re.compile(r"[^A-Z0-9 .,/&*()'\-+#\\]")  # incl. the X12 segment terminator \


def _alpha(value: str, width: int) -> str:
    """Left-justified, uppercase, NACHA-safe characters, space padded."""
    return _ALLOWED.sub("", (value or "").upper())[:width].ljust(width)


def _num(value: int, width: int) -> str:
    s = str(value)
    if value < 0 or len(s) > width:
        raise ValueError(f"{value} does not fit in {width} digits")
    return s.zfill(width)


@dataclass(frozen=True)
class Entry:
    routing: str  # 9-digit RDFI routing number
    account_number: str  # full DFI account number (from the vault)
    account_type: str  # checking / savings
    amount_cents: int
    individual_id: str  # our payment id
    receiver_name: str  # vendor
    remittance: str  # CCD+ addenda payment-related information


@dataclass(frozen=True)
class Batch:
    effective_date: date
    entries: list[Entry]
    description: str = "VENDOR PAY"


@dataclass(frozen=True)
class Originator:
    company_name: str
    company_id: str
    odfi_routing: str
    odfi_name: str


def remittance_rmr(invoice_number: str, amount_cents: int) -> str:
    """ANSI X12 RMR segment, the common CCD+ addenda format."""
    return f"RMR*IV*{invoice_number}*PI*{amount_cents // 100}.{amount_cents % 100:02d}\\"


def entry_hash(routings: list[str]) -> int:
    return sum(int(r[:8]) for r in routings) % 10**10


def build_file(origin: Originator, batches: list[Batch], created: datetime, file_id_modifier: str = "A") -> str:
    odfi8 = origin.odfi_routing[:8]
    lines = [
        "1" + "01" + f" {origin.odfi_routing}" + f" {origin.odfi_routing}"
        + created.strftime("%y%m%d") + created.strftime("%H%M") + file_id_modifier + "094" + "10" + "1"
        + _alpha(origin.odfi_name, 23) + _alpha(origin.company_name, 23) + " " * 8
    ]
    total_entries = total_credit = 0
    all_routings: list[str] = []
    for b_no, batch in enumerate(batches, start=1):
        lines.append(
            "5" + SERVICE_CLASS_CREDITS + _alpha(origin.company_name, 16) + " " * 20 + _alpha(origin.company_id, 10)
            + "CCD" + _alpha(batch.description, 10) + created.strftime("%y%m%d")
            + batch.effective_date.strftime("%y%m%d") + "   " + "1" + odfi8 + _num(b_no, 7)
        )
        routings, credit = [], 0
        for seq, e in enumerate(batch.entries, start=1):
            trace = odfi8 + _num(seq, 7)
            lines.append(
                "6" + TXN_CODES[e.account_type] + e.routing[:8] + e.routing[8] + _alpha(e.account_number, 17)
                + _num(e.amount_cents, 10) + _alpha(e.individual_id, 15) + _alpha(e.receiver_name, 22) + "  "
                + "1" + trace
            )
            lines.append("7" + "05" + _alpha(e.remittance, 80) + "0001" + trace[-7:])
            routings.append(e.routing)
            credit += e.amount_cents
        lines.append(
            "8" + SERVICE_CLASS_CREDITS + _num(len(batch.entries) * 2, 6) + _num(entry_hash(routings), 10)
            + _num(0, 12) + _num(credit, 12) + _alpha(origin.company_id, 10) + " " * 19 + " " * 6
            + odfi8 + _num(b_no, 7)
        )
        total_entries += len(batch.entries) * 2
        total_credit += credit
        all_routings += routings

    records = len(lines) + 1
    blocks = -(-records // BLOCKING_FACTOR)
    lines.append(
        "9" + _num(len(batches), 6) + _num(blocks, 6) + _num(total_entries, 8)
        + _num(entry_hash(all_routings), 10) + _num(0, 12) + _num(total_credit, 12) + " " * 39
    )
    lines += ["9" * RECORD_LEN] * (blocks * BLOCKING_FACTOR - records)
    for i, line in enumerate(lines):
        if len(line) != RECORD_LEN:
            raise AssertionError(f"record {i + 1} is {len(line)} chars")
    return "\n".join(lines) + "\n"


# --- parsing / validation ------------------------------------------------------------------


@dataclass
class ParsedEntry:
    batch_number: int
    effective_date: date
    transaction_code: str
    routing: str
    account_number: str
    amount_cents: int
    individual_id: str
    receiver_name: str
    trace: str
    remittance: str | None = None


def parse_and_validate(text: str) -> tuple[list[ParsedEntry], list[str]]:
    """Parse a NACHA file. Returns entries and a list of structural errors (empty = valid)."""
    lines = text.rstrip("\n").split("\n")
    errors: list[str] = []
    entries: list[ParsedEntry] = []
    for i, line in enumerate(lines, 1):
        if len(line) != RECORD_LEN:
            errors.append(f"record {i}: length {len(line)} != 94")
    if errors:
        return [], errors
    if len(lines) % BLOCKING_FACTOR:
        errors.append(f"{len(lines)} records is not a multiple of the blocking factor 10")
    if lines[0][0] != "1":
        return [], errors + ["first record is not a file header"]

    body = [ln for ln in lines if ln != "9" * RECORD_LEN]
    if body[-1][0] != "9":
        return [], errors + ["missing file control record"]

    batch_count = file_entries = file_credit = 0
    file_routings: list[str] = []
    i = 1
    while i < len(body) - 1:
        hdr = body[i]
        if hdr[0] != "5":
            errors.append(f"expected batch header, got record type {hdr[0]!r}")
            break
        batch_count += 1
        b_no = int(hdr[87:94])
        if hdr[50:53] != "CCD":
            errors.append(f"batch {b_no}: SEC code {hdr[50:53]!r} is not CCD")
        eff = datetime.strptime(hdr[69:75], "%y%m%d").date()
        i += 1
        routings, credit, count = [], 0, 0
        while i < len(body) and body[i][0] in "67":
            rec = body[i]
            count += 1
            if rec[0] == "6":
                if rec[1:3] not in TXN_CODES.values():
                    errors.append(f"batch {b_no}: transaction code {rec[1:3]} is not a credit (22/32)")
                routing = rec[3:12]
                routings.append(routing)
                credit += int(rec[29:39])
                entries.append(ParsedEntry(b_no, eff, rec[1:3], routing, rec[12:29].strip(), int(rec[29:39]),
                                           rec[39:54].strip(), rec[54:76].strip(), rec[79:94]))
            else:
                if rec[1:3] != "05":
                    errors.append(f"batch {b_no}: addenda type {rec[1:3]} is not 05")
                if not entries or entries[-1].trace[-7:] != rec[87:94]:
                    errors.append(f"batch {b_no}: addenda does not follow its entry")
                else:
                    entries[-1].remittance = rec[3:83].strip()
            i += 1
        ctl = body[i] if i < len(body) else ""
        if not ctl.startswith("8"):
            errors.append(f"batch {b_no}: missing batch control")
            break
        if int(ctl[4:10]) != count:
            errors.append(f"batch {b_no}: entry/addenda count {int(ctl[4:10])} != {count}")
        if int(ctl[10:20]) != entry_hash(routings):
            errors.append(f"batch {b_no}: entry hash {int(ctl[10:20])} != {entry_hash(routings)}")
        if int(ctl[32:44]) != credit:
            errors.append(f"batch {b_no}: total credit {int(ctl[32:44])} != {credit}")
        if int(ctl[20:32]) != 0:
            errors.append(f"batch {b_no}: debits are not allowed in a vendor payment file")
        file_entries += count
        file_credit += credit
        file_routings += routings
        i += 1

    fc = body[-1]
    if int(fc[1:7]) != batch_count:
        errors.append(f"file control batch count {int(fc[1:7])} != {batch_count}")
    if int(fc[7:13]) != len(lines) // BLOCKING_FACTOR:
        errors.append(f"file control block count {int(fc[7:13])} != {len(lines) // BLOCKING_FACTOR}")
    if int(fc[13:21]) != file_entries:
        errors.append(f"file control entry count {int(fc[13:21])} != {file_entries}")
    if int(fc[21:31]) != entry_hash(file_routings):
        errors.append("file control entry hash mismatch")
    if int(fc[43:55]) != file_credit:
        errors.append(f"file control total credit {int(fc[43:55])} != {file_credit}")
    return entries, errors
