"""Shared set-up for the governance tests: ready and queued packets, claim races, budgets."""

from __future__ import annotations

import threading

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.contracts import (
    PacketStatus,
    ProjectTree,
)
from orchestrator.db.models import (
    CandidateFieldDefinitionRow,
    Event,
    Packet,
)
from orchestrator.events import record_event
from orchestrator.governance import grant_waiver
from orchestrator.ingest import persist_tree
from orchestrator.lifecycle import transition
from orchestrator.llm import OllamaProvider
from orchestrator.queue import admit_next, claim
from tests.conftest import KERNEL, USER
from tests.evafixtures import Item, accept_all, seed_eva

GOV_TREE = {
    "project": {"name": "M2.1", "brief": "Review fixes."},
    "objectives": [
        {
            "name": "O",
            "tasks": [
                {"title": f"Task {i}", "prompt_description": f"Do thing number {i}."}
                for i in range(3)
            ],
        }
    ],
}


ALL_EIGHTS = {3: 8, 4: 8, 5: 8, 6: 8}


SUPPORTED = {4: (1, 0, 0), 5: (1, 0, 0)}


def ready_packet(session: Session, *, skip: frozenset[str] = frozenset()) -> Packet:
    packet = persist_tree(session, ProjectTree.model_validate(GOV_TREE), actor=USER).packets[0]
    transition(packet, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, packet, skip=skip)
    transition(packet, PacketStatus.READY, KERNEL)
    return packet


def claim_race(factory: sessionmaker[Session], ref: str, writer) -> list[str]:  # type: ignore[no-untyped-def]
    """Session A checks and builds the snapshot; session B commits ``writer``; A's guarded
    UPDATE then runs. Returns A's outcome."""
    outcome: list[str] = []
    checked, written = threading.Event(), threading.Event()

    def worker_a() -> None:
        with factory() as a:

            def pause() -> None:
                checked.set()
                written.wait(10)

            try:
                claim(a, ref, "worker-a", before_cas=pause)
                a.commit()
                outcome.append("won")
            except Exception as exc:  # ClaimError, or SQLite's snapshot conflict
                a.rollback()
                outcome.append(type(exc).__name__)

    def worker_b() -> None:
        checked.wait(10)
        with factory() as b:
            writer(b)
            b.commit()
        written.set()

    threads = [threading.Thread(target=worker_a), threading.Thread(target=worker_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return outcome


def queued_ref(factory: sessionmaker[Session], *, waived: bool) -> tuple[str, str | None]:
    with factory() as s:
        skip = frozenset({"tools_needed"}) if waived else frozenset()
        packet = ready_packet(s, skip=skip)
        waiver_id = None
        if waived:
            seed_eva(
                s,
                packet,
                "tools_needed",
                {"other": Item("on", "on", {3: 4, 4: 4, 5: 4, 6: 4}, SUPPORTED)},
            )
            waiver_id = grant_waiver(s, packet.ref, "tools_needed", actor=USER, reason="ok").id
        assert admit_next(s).ref == packet.ref
        s.commit()
        return packet.ref, waiver_id


def bump_definition(session: Session, name: str) -> None:
    row = session.scalar(
        select(CandidateFieldDefinitionRow).where(CandidateFieldDefinitionRow.name == name)
    )
    row.prompt_hint += " (clarified)"
    row.version += 1
    record_event(session, actor=USER, action="t", entity_type="candidate_field_definition")
    session.flush()


def field_status(session: Session, packet: Packet, name: str) -> str | None:
    return session.execute(
        text(
            "SELECT status FROM v_field_status WHERE packet_id = :id AND field_name = :f "
            "AND phase = 'pre'"
        ),
        {"id": packet.id, "f": name},
    ).scalar()


def ollama_with_tags(digest: str) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "eva-a", "digest": digest},
                        {"name": "eva-b", "digest": digest},
                    ]
                },
            )
        return httpx.Response(200, json={"model": "m", "message": {"content": "{}"}})

    return OllamaProvider(transport=httpx.MockTransport(handler))


def event_actions(session: Session) -> set[str]:
    return set(session.scalars(select(Event.action).where(Event.action.like("evaluation.%"))))
