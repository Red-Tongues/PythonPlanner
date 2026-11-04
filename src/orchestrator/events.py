"""The audit log. Every state change writes exactly one ``events`` row (golden rule 5)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.db.models import Event

log = logging.getLogger(__name__)


def record_event(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    entity_ref: str | None = None,
    entity_version: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
) -> Event:
    event = Event(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_ref=entity_ref,
        entity_version=entity_version,
        before=before,
        after=after,
        reason=reason,
    )
    session.add(event)
    log.debug("event %s %s %s by %s", action, entity_type, entity_ref or entity_id, actor)
    return event


def recent_events(session: Session, limit: int = 20, entity_id: str | None = None) -> list[Event]:
    stmt = select(Event).order_by(Event.created_at.desc(), Event.id.desc()).limit(limit)
    if entity_id is not None:
        stmt = stmt.where(Event.entity_id == entity_id)
    return list(session.scalars(stmt))
