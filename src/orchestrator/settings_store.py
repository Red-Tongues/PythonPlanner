"""Typed reads of the ``settings`` table (tunables live in the database, editable and evented)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.db.models import Setting


def get_setting(session: Session, key: str, default: Any) -> Any:
    row = session.get(Setting, key)
    return default if row is None else row.value


def get_int(session: Session, key: str, default: int) -> int:
    return int(get_setting(session, key, default))


def get_decimal(session: Session, key: str, default: str) -> Decimal:
    return Decimal(str(get_setting(session, key, default)))
