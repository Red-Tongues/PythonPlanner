"""Shared set-up for the M3 run tests: a queued genPacket, claimed into a ``planned`` run."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.contracts import Channel, PacketStatus, ProjectTree
from orchestrator.db.models import ChannelRun, GenerationRun, Packet
from orchestrator.ingest import persist_tree
from orchestrator.lifecycle import transition
from orchestrator.queue import admit_next, claim
from tests.conftest import KERNEL, USER
from tests.evafixtures import accept_all

TREE = {
    "project": {"name": "M3", "brief": "Runs."},
    "objectives": [
        {
            "name": "Build",
            "tasks": [
                {"title": f"Task {i}", "prompt_description": f"Do thing number {i}."}
                for i in range(3)
            ],
        }
    ],
}


def queued_packet(session: Session, index: int = 0) -> Packet:
    """A packet with an accepted evaluation set, routed to Local Generation and queued."""
    from orchestrator.branching import apply_channel

    packets = persist_tree(session, ProjectTree.model_validate(TREE), actor=USER).packets
    packet = packets[index]
    apply_channel(session, packet, Channel.LOCAL_GENERATION, actor=USER, reason="test route")
    transition(packet, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, packet)
    transition(packet, PacketStatus.READY, KERNEL)
    admitted = admit_next(session)
    assert admitted is not None and admitted.ref == packet.ref
    return packet


def claimed_run(session: Session) -> tuple[Packet, GenerationRun]:
    packet = queued_packet(session)
    run = claim(session, packet.ref, "kernel-a")
    return packet, run


def attempts(session: Session) -> int:
    """Committed dispatch evidence: one immutable attempt row per authorised effect."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(ChannelRun)
            .where(ChannelRun.generation_run_id.is_not(None))
        )
        or 0
    )
