import pytest

from app.payments.routing import aba_check_digit, is_valid_aba


@pytest.mark.parametrize("routing", ["021000021", "011000015", "111000025"])
def test_valid(routing):
    assert is_valid_aba(routing)


@pytest.mark.parametrize("routing", ["021000022", "12345678", "0210000210", "02100002a", "", None])
def test_invalid(routing):
    assert not is_valid_aba(routing)


def test_check_digit_roundtrip():
    for first8 in ["02100002", "11100002", "31107443"]:
        assert is_valid_aba(first8 + aba_check_digit(first8))
