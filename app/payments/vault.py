"""Mock account tokenization. Agents and rules only ever see account_hash + last4;
the full number is read back only when building bank files."""

from __future__ import annotations

import hashlib

from sqlmodel import Session

from app.models import AccountVault


def hash_account(account_number: str) -> str:
    return hashlib.sha256(account_number.encode()).hexdigest()


def tokenize(session: Session, account_number: str) -> str:
    token = hash_account(account_number)
    if session.get(AccountVault, token) is None:
        session.add(AccountVault(account_hash=token, account_number=account_number))
    return token


def detokenize(session: Session, account_hash: str) -> str:
    row = session.get(AccountVault, account_hash)
    if row is None:
        raise KeyError("unknown account token")
    return row.account_number
