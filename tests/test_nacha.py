from datetime import date, datetime

import pytest

from app.payments import nacha
from app.payments.positive_pay import IssuedCheck, build_csv

ORIGIN = nacha.Originator("BLUESTEM RESIDENTIAL", "1870000001", "099999999", "PROPPAY MOCK BANK")
CREATED = datetime(2026, 10, 19, 9, 5)


def entry(routing="021000021", amount=125_050, account_type="checking", n=1):
    return nacha.Entry(routing=routing, account_number=f"12345678{n:02d}", account_type=account_type,
                       amount_cents=amount, individual_id=f"PAY{n}", receiver_name=f"Vendor {n} LLC",
                       remittance=nacha.remittance_rmr(f"INV-{n}", amount))


def build(batches):
    return nacha.build_file(ORIGIN, batches, CREATED)


def test_every_record_is_94_chars_and_blocked_by_10():
    text = build([nacha.Batch(date(2026, 10, 20), [entry(n=i) for i in range(1, 4)])])
    lines = text.rstrip("\n").split("\n")
    assert all(len(ln) == 94 for ln in lines)
    # 1 header + 1 batch header + 3*(entry+addenda) + 1 batch control + 1 file control = 10
    assert len(lines) == 10 and "9" * 94 not in lines


def test_padding_with_nines():
    text = build([nacha.Batch(date(2026, 10, 20), [entry()])])  # 6 records -> padded to 10
    lines = text.rstrip("\n").split("\n")
    assert len(lines) == 10 and lines[-4:] == ["9" * 94] * 4


def test_record_layout_fields():
    text = build([nacha.Batch(date(2026, 10, 20), [entry(account_type="savings")])])
    hdr, bh, e6, a7, bc, fc = text.split("\n")[:6]
    assert hdr[:3] == "101" and hdr[3:13] == " 099999999" and hdr[34:37] == "094" and hdr[37:39] == "10"
    assert bh[:4] == "5220" and bh[50:53] == "CCD" and bh[69:75] == "261020"
    assert e6[:3] == "632" and e6[3:12] == "021000021" and e6[29:39] == "0000125050" and e6[78] == "1"
    assert a7[:3] == "705" and a7[3:83].strip() == "RMR*IV*INV-1*PI*1250.50\\" and a7[87:94] == e6[-7:]
    assert bc[:4] == "8220" and fc[0] == "9"


def test_control_totals_and_entry_hash():
    routings = ["021000021", "011000015", "111000025"]
    amounts = [100_00, 2_500_00, 33_33]
    text = build([nacha.Batch(date(2026, 10, 20), [entry(r, a, n=i) for i, (r, a) in enumerate(zip(routings, amounts))])])
    lines = text.split("\n")
    bc = next(ln for ln in lines if ln.startswith("8"))
    fc = next(ln for ln in lines if ln.startswith("9") and ln != "9" * 94)
    expected_hash = (2100002 + 1100001 + 11100002) % 10**10
    assert int(bc[4:10]) == 6 and int(bc[10:20]) == expected_hash and int(bc[32:44]) == sum(amounts)
    assert int(fc[1:7]) == 1 and int(fc[7:13]) == 1 and int(fc[13:21]) == 6
    assert int(fc[21:31]) == expected_hash and int(fc[43:55]) == sum(amounts)


def test_entry_hash_keeps_rightmost_10_digits():
    assert nacha.entry_hash(["999999999"] * 2000) == (99999999 * 2000) % 10**10


def test_multiple_batches_parse_back_valid():
    text = build([nacha.Batch(date(2026, 10, 20), [entry(n=1), entry(n=2)]),
                  nacha.Batch(date(2026, 10, 23), [entry("011000015", 99, n=3)])])
    entries, errors = nacha.parse_and_validate(text)
    assert errors == []
    assert [(e.batch_number, e.amount_cents) for e in entries] == [(1, 125_050), (1, 125_050), (2, 99)]
    assert entries[2].effective_date == date(2026, 10, 23) and entries[2].remittance.startswith("RMR*IV*INV-3")
    assert entries[0].account_number == "1234567801"


def test_names_are_sanitized_and_truncated():
    e = nacha.Entry("021000021", "1", "checking", 1, "P1", "Hilltop Roofing & Gütters — Extra Long Name Inc", "x")
    text = build([nacha.Batch(date(2026, 10, 20), [e])])
    e6 = text.split("\n")[2]
    assert e6[54:76] == "HILLTOP ROOFING & GTTE"


@pytest.mark.parametrize("corrupt,msg", [
    (lambda ls: ls[:2] + [ls[2][:29] + "0000999999" + ls[2][39:]] + ls[3:], "total credit"),
    (lambda ls: ls[:2] + [ls[2][:3] + "011000015" + ls[2][12:]] + ls[3:], "entry hash"),
    (lambda ls: ls[:-1], "blocking factor"),
    (lambda ls: [ls[0] + " "] + ls[1:], "length"),
])
def test_validator_catches_corruption(corrupt, msg):
    lines = build([nacha.Batch(date(2026, 10, 20), [entry()])]).rstrip("\n").split("\n")
    _, errors = nacha.parse_and_validate("\n".join(corrupt(lines)) + "\n")
    assert any(msg in e for e in errors), errors


def test_amount_overflow_rejected():
    with pytest.raises(ValueError):
        build([nacha.Batch(date(2026, 10, 20), [entry(amount=10**10)])])


def test_positive_pay_csv():
    out = build_csv("000123450001", [IssuedCheck(10001, 460_012, date(2026, 10, 19), "Paloma Water District")])
    assert out.splitlines() == ["account_number,check_number,amount,issue_date,payee",
                                "000123450001,10001,4600.12,2026-10-19,Paloma Water District"]
