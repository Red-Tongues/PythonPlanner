"""Evented writes only (R1 fix 7, golden rule 5): an unaudited ORM write is rejected at flush."""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from orchestrator.db.models import (
    Packet,
    Setting,
    Threshold,
)
from orchestrator.errors import UnauditedWriteError
from orchestrator.events import record_event
from orchestrator.repository import (
    get_packet,
    update_packet,
)
from tests.conftest import USER

# --------------------------------------------------------------------------- evented writes


def test_unevented_orm_writes_are_rejected(session: Session, tree) -> None:
    packet = get_packet(session, tree.packets[0].ref)
    packet.title = "edited without an event"
    with pytest.raises(UnauditedWriteError):
        session.flush()
    session.rollback()

    session.add(Threshold(name="x", field_name="styles", operator=">=", value="1", action="split"))
    with pytest.raises(UnauditedWriteError):
        session.flush()
    session.rollback()

    with pytest.raises(UnauditedWriteError):
        session.execute(update(Packet).values(title="bulk"))


def test_settings_must_bump_their_version(session: Session) -> None:
    row = session.get(Setting, "aggregation.dispute_margin")
    row.value = 0.3
    record_event(
        session, actor=USER, action="setting.changed", entity_type="setting", entity_id=row.key
    )
    with pytest.raises(UnauditedWriteError, match="version"):
        session.flush()


def test_evented_writes_pass(session: Session, tree) -> None:
    packet = get_packet(session, tree.packets[0].ref)
    update_packet(session, packet, {"title": "evented"}, actor=USER)
    session.add(Threshold(name="y", field_name="styles", operator=">=", value="1", action="split"))
    record_event(session, actor=USER, action="threshold.created", entity_type="threshold")
    session.commit()
