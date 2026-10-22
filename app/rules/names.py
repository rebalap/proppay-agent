"""Name normalization shared by vendor resolution, look-alike and OFAC screening."""

from __future__ import annotations

import re

LEGAL_SUFFIXES = {"LLC", "L L C", "INC", "INCORPORATED", "CO", "COMPANY", "CORP", "CORPORATION", "LTD", "LP", "LLP",
                  "PLLC", "PC", "FZE", "FZCO", "SA", "GMBH", "THE"}


def normalize_name(name: str | None, *, drop_suffixes: bool = True) -> str:
    """Uppercase, '&' -> AND, strip punctuation, optionally drop legal-entity suffixes."""
    if not name:
        return ""
    s = re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("&", " AND "))
    tokens = s.split()
    if drop_suffixes:
        tokens = [t for t in tokens if t not in LEGAL_SUFFIXES]
    return " ".join(tokens)
