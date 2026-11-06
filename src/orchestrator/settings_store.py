"""Typed reads of the ``settings`` table (tunables live in the database, editable and evented).

M3a.1: a reader passes only a key; the fallback is that key's default in
``orchestrator.tunables`` — the same value ``init-db`` seeds — so no literal default exists
anywhere else.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.db.models import Setting
from orchestrator.tunables import default


def get_setting(session: Session, key: str) -> Any:
    row = session.get(Setting, key)
    return default(key) if row is None else row.value


def get_int(session: Session, key: str) -> int:
    return int(get_setting(session, key))


def get_decimal(session: Session, key: str) -> Decimal:
    return Decimal(str(get_setting(session, key)))
