"""M3a.1 (Argon ruling): a sealed parent's active user HOLD/BLOCK overrides are inherited by its
re-imagined child in the seal transaction. Both dialects."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.contracts import OverrideKind, PacketStatus
from orchestrator.db.models import GenerationRun, Packet, QueueOverride, Revocation, utcnow
from orchestrator.errors import OrchestratorError
from orchestrator.evaluation.gen_eva import complete_generation
from orchestrator.governance import (
    GovernanceError,
    active_overrides,
    add_dependency,
    add_override,
    revoke,
)
from orchestrator.llm import FakeProvider
from orchestrator.queue import ClaimError, claim, gen_queue_rows
from orchestrator.runs import authorise_dispatch
from tests.conftest import USER
from tests.runfixtures import held, queued_packet

S = PacketStatus


def _dispatched(session: Session, index: int = 0) -> tuple[Packet, GenerationRun]:
    packet = queued_packet(session, index)
    run = claim(session, packet.ref, "kernel-a")
    authorise_dispatch(session, run, lock=held(session))
    return packet, run


def _copies(session: Session, child: Packet) -> list[QueueOverride]:
    return list(session.scalars(select(QueueOverride).where(QueueOverride.packet_id == child.id)))


def _seal(session: Session, run: GenerationRun) -> Packet:
    result = complete_generation(session, run, "the output", FakeProvider(), lock=held(session))
    assert result.child is not None
    return result.child


def test_a_hold_set_mid_run_holds_the_child(session: Session) -> None:
    packet, run = _dispatched(session)
    expires = utcnow() + timedelta(days=2)
    hold = add_override(
        session, packet.ref, OverrideKind.HOLD, actor=USER, reason="wait", expires_at=expires
    )
    pin = add_override(session, packet.ref, OverrideKind.PIN, actor=USER, reason="soon")
    child = _seal(session, run)
    inherited = active_overrides(session, child)
    assert len(inherited) == 1 and pin.id not in {o.inherited_from_id for o in inherited}
    copy = inherited[0]
    assert OverrideKind(copy.kind) is OverrideKind.HOLD
    assert (copy.actor, copy.reason, copy.inherited_from_id) == (str(USER), "wait", hold.id)
    assert copy.expires_at == expires and copy.note == f"inherited from {packet.ref}"
    assert child.id not in {r.packet_id for r in gen_queue_rows(session)}  # held: not queued
    # the parent's hold now lives on the child: release it there, as a normal user action
    with pytest.raises(GovernanceError, match="inherited by"):
        revoke(session, "override", hold.id, actor=USER, reason="too late")
    revoke(session, "override", copy.id, actor=USER, reason="go")
    assert active_overrides(session, child) == []


def test_released_expired_and_system_holds_are_not_copied(session: Session) -> None:
    packet, run = _dispatched(session)
    released = add_override(session, packet.ref, OverrideKind.HOLD, actor=USER, reason="a")
    revoke(session, "override", released.id, actor=USER, reason="released before the seal")
    add_override(
        session,
        packet.ref,
        OverrideKind.BLOCK,
        actor=USER,
        reason="expired",
        expires_at=utcnow() - timedelta(seconds=1),
    )
    child = _seal(session, run)
    assert (
        session.scalars(select(QueueOverride).where(QueueOverride.packet_id == child.id)).all()
        == []
    )


def test_a_dependency_block_is_not_copied(session: Session) -> None:
    packet, run = _dispatched(session)
    other = queued_packet(session, 1)
    add_dependency(session, packet.ref, other.ref, actor=USER)  # unmet: a dependency block
    child = _seal(session, run)
    assert (
        session.scalars(select(QueueOverride).where(QueueOverride.packet_id == child.id)).all()
        == []
    )


def test_a_release_committed_before_the_seal_is_respected(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        packet, run = _dispatched(s)
        hold = add_override(s, packet.ref, OverrideKind.HOLD, actor=USER, reason="wait")
        s.commit()
        run_id, hold_id = run.id, hold.id
    with factory() as s:
        revoke(s, "override", hold_id, actor=USER, reason="released")
        s.commit()
    with factory() as s:
        child = _seal(s, s.get(GenerationRun, run_id))
        s.commit()
        assert active_overrides(s, child) == []


def test_a_release_racing_the_seal_ends_consistent(factory: sessionmaker[Session]) -> None:
    """The seal holds the parent row; a concurrent release either lands before the seal reads
    the overrides (child not held) or fails (child held, parent hold unrevoked). Never both a
    revoked parent hold and an inherited child hold."""
    with factory() as s:
        packet, run = _dispatched(s)
        hold = add_override(s, packet.ref, OverrideKind.HOLD, actor=USER, reason="wait")
        s.commit()
        run_id, hold_id = run.id, hold.id
    released: list[str] = []
    started = threading.Event()

    def releaser() -> None:
        with factory() as b:
            started.set()
            try:
                revoke(b, "override", hold_id, actor=USER, reason="racing release")
                b.commit()
                released.append("committed")
            except (DBAPIError, OperationalError, OrchestratorError):
                b.rollback()
                released.append("refused")

    a = factory()
    try:
        child = _seal(a, a.get(GenerationRun, run_id))
        child_id = child.id
        thread = threading.Thread(target=releaser)
        thread.start()
        started.wait(5)
        time.sleep(1.0)  # let the release reach the parent row the seal holds
        a.commit()
    finally:
        a.close()
    thread.join(30)
    assert released, "the releaser never finished"
    with factory() as s:
        child = s.get(Packet, child_id)
        child_held = bool(active_overrides(s, child))
        parent_revoked = (
            s.scalar(select(Revocation).where(Revocation.target_id == hold_id)) is not None
        )
        assert child_held != parent_revoked, (released, child_held, parent_revoked)
        if released == ["refused"]:
            assert child_held and not parent_revoked
        with pytest.raises(ClaimError):  # whatever happened, a held child is never claimed
            if child_held:
                claim(s, child.ref, "kernel-b")
            else:
                raise ClaimError("not held: nothing to check")
