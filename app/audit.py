"""Audit trail: every state change writes one row to audit_log."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel
from sqlmodel import Session, SQLModel

from app.models import AuditLog


def snapshot(row: SQLModel) -> dict[str, Any]:
    """Column values of a table row. Uses getattr so expired rows (after commit) reload;
    model_dump() would silently return {} for them."""
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def _to_json(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "__table__"):  # SQLModel table row
        value = snapshot(value)
    elif isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, default=str, sort_keys=True)


def record(
    session: Session,
    *,
    actor: str,
    entity: str,
    entity_id: int | None,
    action: str,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
) -> AuditLog:
    """Add an audit row to the caller's session (committed with the caller's change)."""
    row = AuditLog(
        actor=actor,
        entity=entity,
        entity_id=entity_id,
        action=action,
        before_json=_to_json(before),
        after_json=_to_json(after),
        reason=reason,
    )
    session.add(row)
    return row
