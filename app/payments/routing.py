"""ABA routing number validation (checksum weights 3, 7, 1)."""

from __future__ import annotations

WEIGHTS = (3, 7, 1, 3, 7, 1, 3, 7, 1)


def is_valid_aba(routing: str | None) -> bool:
    if not routing or len(routing) != 9 or not routing.isdigit():
        return False
    return sum(int(d) * w for d, w in zip(routing, WEIGHTS)) % 10 == 0


def aba_check_digit(first8: str) -> str:
    """Check digit that makes `first8 + digit` a valid ABA number."""
    if len(first8) != 8 or not first8.isdigit():
        raise ValueError("need exactly 8 digits")
    total = sum(int(d) * w for d, w in zip(first8, WEIGHTS))
    return str((10 - total % 10) % 10)
