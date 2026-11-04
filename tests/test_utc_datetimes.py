"""R1 deferral GH6: every datetime is timezone-aware UTC on both dialects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from orchestrator.db.models import Packet
from orchestrator.db.types import NaiveDatetimeError
from orchestrator.events import record_event
from tests.conftest import KERNEL


def test_round_trip_is_aware_utc_and_comparable(session: Session, tree) -> None:
    packet = session.get(Packet, tree.packets[0].id)
    assert packet is not None
    plus_two = timezone(timedelta(hours=2))
    lease = datetime(2026, 11, 4, 12, 0, tzinfo=plus_two)
    packet.lease_expires_at = lease
    record_event(session, actor=KERNEL, action="t", entity_type="packet", entity_id=packet.id)
    session.commit()
    session.expire_all()
    stored = session.get(Packet, packet.id)
    assert stored is not None and stored.lease_expires_at is not None
    assert stored.lease_expires_at.tzinfo is UTC
    assert stored.lease_expires_at == lease  # same instant
    assert stored.lease_expires_at.hour == 10  # stored as UTC wall time
    assert stored.created_at.tzinfo is UTC
    assert stored.created_at <= datetime.now(UTC)  # aware vs aware: comparable


def test_naive_datetimes_are_rejected(session: Session, tree) -> None:
    packet = session.get(Packet, tree.packets[0].id)
    assert packet is not None
    packet.lease_expires_at = datetime(2026, 11, 4, 12, 0)  # naive
    record_event(session, actor=KERNEL, action="t", entity_type="packet", entity_id=packet.id)
    with pytest.raises(Exception) as info:
        session.flush()
    assert isinstance(info.value.__cause__ or info.value, NaiveDatetimeError) or (
        "naive datetime" in str(info.value)
    )
