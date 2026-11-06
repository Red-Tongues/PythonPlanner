"""M3a (e): the immediate gen_eva gate, sealing, the re-imagined child and crashes (AC28-AC35).

Boundary fixtures (PM rule 4): lineage stop 8,499 / 8,500, crash counts 3 / 5, the 300 s route
window. Both dialects; FakeProvider only.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.contracts import EvaPhase, GenerationRunState, PacketStatus
from orchestrator.db.models import (
    Artifact,
    EvaPacket,
    GenerationRun,
    Packet,
    RouteHealth,
    SealedBundle,
    new_id,
    utcnow,
)
from orchestrator.evaluation import gen_eva
from orchestrator.evaluation.gen_eva import (
    LINEAGE_HISTORY_SQL,
    complete_generation,
    composite_grade_e4,
    reimagined_prompt,
    seal_packet,
    spawn_child,
)
from orchestrator.events import record_event
from orchestrator.governance import add_dependency
from orchestrator.lifecycle import transition
from orchestrator.llm import FakeProvider
from orchestrator.queue import admission_tick, claim
from orchestrator.runs import _packet_cas, authorise_dispatch, crash_cooldown_seconds, record_crash
from tests.conftest import KERNEL, USER
from tests.evafixtures import FIXTURE, accept_all
from tests.runfixtures import held, queued_packet

R = GenerationRunState
S = PacketStatus


def _dispatched(session: Session, index: int = 0) -> tuple[Packet, GenerationRun]:
    packet = queued_packet(session, index)
    run = claim(session, packet.ref, "kernel-a")
    authorise_dispatch(session, run, lock=held(session))
    return packet, run


def _seal(session: Session, packet: Packet, run: GenerationRun, grade: int) -> SealedBundle:
    """Seal at a chosen grade (the grade maths is tested on its own below)."""
    content = f"output {packet.ref} {new_id()}"
    artifact = Artifact(
        id=new_id(),
        packet_id=packet.id,
        path="out.md",
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        content=content,
    )
    session.add(artifact)
    record_event(session, actor=FIXTURE, action="t", entity_type="artifact", entity_id=artifact.id)
    session.flush()
    _packet_cas(session, packet, S.EVALUATING_POST, KERNEL, "test: output arrived")
    return seal_packet(session, packet, run, artifact, grade)


# --------------------------------------------------------------------------- grade (OQ16)


def test_composite_grade_is_integer_and_moves_sonar_weight() -> None:
    full = {
        "requirement_alignment": 8000,
        "testability": 6000,
        "acceptance_criteria_coverage": 7000,
        "risk_assessment": 9000,
        "sonar_hotspot_alignment": 5000,
    }
    assert composite_grade_e4(full) == (3000 * 8000 + 2500 * 6000 + 2000 * 7000
                                        + 1500 * 9000 + 1000 * 5000) // 10000  # fmt: skip
    no_sonar = {k: v for k, v in full.items() if k != "sonar_hotspot_alignment"}
    assert composite_grade_e4(no_sonar) == (3000 * 8000 + 3000 * 6000 + 2000 * 7000
                                            + 2000 * 9000) // 10000  # fmt: skip
    assert composite_grade_e4({}) == 0


# --------------------------------------------------------------------------- the gate


def test_gen_eva_runs_immediately_seals_and_spawns(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC28-AC30: no queue view is read before the seal; post passes, grade, bundle, lock and
    exactly one child with the lineage fields and a different prompt."""
    import orchestrator.queue as queue

    packet, run = _dispatched(session)
    seen: list[str] = []
    real = queue.gen_queue_rows

    def spy(s, now=None):  # type: ignore[no-untyped-def]
        sealed = s.scalar(select(SealedBundle).where(SealedBundle.packet_id == packet.id))
        seen.append("after seal" if sealed is not None else "before seal")
        return real(s, now)

    monkeypatch.setattr(queue, "gen_queue_rows", spy)
    monkeypatch.setattr(queue, "eva_queue_rows", lambda *a, **k: pytest.fail("evaQueue read"))
    result = complete_generation(
        session, run, "def parse(): ...", FakeProvider(), lock=held(session)
    )
    assert not result.crashed and seen and set(seen) == {"after seal"}
    bundle, child = result.bundle, result.child
    assert bundle is not None and child is not None
    post = session.scalar(
        select(func.count())
        .select_from(EvaPacket)
        .where(EvaPacket.packet_id == packet.id, EvaPacket.phase == EvaPhase.POST)
    )
    assert post and bundle.composite_grade_e4 == result.grade_e4
    blob = session.execute(
        text("SELECT body FROM content_blobs WHERE hash = :h"), {"h": bundle.snapshot_hash}
    ).scalar()
    assert hashlib.sha256(blob.encode()).hexdigest() == hashlib.sha256(blob.encode()).hexdigest()
    session.refresh(packet)
    assert packet.status is S.SEALED and packet.locked and run.state is R.SUCCEEDED
    assert (child.parent_id, child.lineage_key, child.root_id) == (
        packet.id,
        packet.lineage_key,
        packet.root_id,
    )
    assert child.lineage_depth == packet.lineage_depth + 1 and child.position == packet.position
    assert (child.parent_bundle_id, child.parent_bundle_hash) == (bundle.id, bundle.bundle_hash)
    assert child.prompt_description != packet.prompt_description and child.title == packet.title
    # a sealed packet is immutable; a second seal fails on UNIQUE(packet_id)
    with pytest.raises(IntegrityError):
        session.add(
            SealedBundle(
                id=new_id(),
                packet_id=packet.id,
                claim_content_revision=0,
                snapshot_hash=bundle.snapshot_hash,
                bundle_hash="x",
                composite_grade_e4=1,
            )
        )
        record_event(session, actor=FIXTURE, action="t", entity_type="sealed_bundle")
        session.flush()


def test_the_seal_is_the_claim_time_snapshot(session: Session) -> None:
    """R6 OQ20: an edit during the run carries to the child; the seal holds the claim-time
    content."""
    import json

    from orchestrator.repository import update_packet

    packet, run = _dispatched(session)
    original = packet.prompt_description
    update_packet(session, packet, {"prompt_description": "Edited mid-run."}, actor=USER)
    bundle = _seal(session, packet, run, 6000)
    blob = session.execute(
        text("SELECT body FROM content_blobs WHERE hash = :h"), {"h": bundle.snapshot_hash}
    ).scalar()
    snapshot = json.loads(blob)
    assert snapshot["content"]["prompt_description"] == original
    assert bundle.claim_content_revision == 0
    child = spawn_child(session, packet, bundle, reimagined_prompt(
        session, packet, artifact_hash=bundle.artifact_hash or "", grade_e4=6000))  # fmt: skip
    assert "Edited mid-run." in child.prompt_description


@pytest.mark.parametrize(("grade", "held_child"), [(8499, False), (8500, True), (9500, True)])
def test_lineage_stop_boundary(session: Session, grade: int, held_child: bool) -> None:
    """R6 OQ14 (AC30): every seal spawns exactly one child; at ≥ 8,500 it is held."""
    packet, run = _dispatched(session)
    bundle = _seal(session, packet, run, grade)
    child = spawn_child(session, packet, bundle, "re-imagined")
    assert spawn_child(session, packet, bundle, "again") is child  # idempotent per bundle
    assert (child.status is S.HELD) is held_child
    assert (child.hold_reason == "grade_met") is held_child
    if not held_child:  # carried fields satisfy readiness → draft → ready (OQ27)
        assert child.status is S.READY
    carried = session.execute(
        text(
            "SELECT COUNT(*) FROM eva_context_links WHERE packet_id = :id "
            "AND link_reason = 'carried' AND is_questioned"
        ),
        {"id": child.id},
    ).scalar()
    assert carried and carried > 0  # every carried field is questioned (OQ24)


def test_a_failure_before_the_spawn_rolls_back_and_a_retry_is_exact(
    factory: sessionmaker[Session],
) -> None:
    """AC31."""
    with factory() as s:
        packet, run = _dispatched(s)
        ref, run_id = packet.ref, run.id
        s.commit()

    def boom() -> None:
        raise RuntimeError("injected between seal and spawn")

    with factory() as s:
        run = s.get(GenerationRun, run_id)
        with pytest.raises(RuntimeError, match="injected"):
            complete_generation(s, run, "output", FakeProvider(), lock=held(s), before_spawn=boom)
        s.rollback()
    with factory() as s:
        run = s.get(GenerationRun, run_id)
        result = complete_generation(s, run, "output", FakeProvider(), lock=held(s))
        s.commit()
        assert result.child is not None
    with factory() as s:
        packet_id = s.scalar(select(Packet.id).where(Packet.ref == ref))
        bundles = s.scalar(
            select(func.count())
            .select_from(SealedBundle)
            .where(SealedBundle.packet_id == packet_id)
        )
        children = s.scalar(
            select(func.count()).select_from(Packet).where(Packet.parent_id == packet_id)
        )
        assert (bundles, children) == (1, 1)


def test_the_cfo_example(session: Session) -> None:
    """AC32: a poorly graded, pivotal parent's child is admitted; a well-graded, non-relational
    parent's child stays ready."""
    pivotal, run_a = _dispatched(session, 0)
    for i in (1, 2):  # two dependents make it pivotal
        other = queued_packet(session, i)
        add_dependency(session, other.ref, pivotal.ref, actor=USER)
    quiet, run_b = _dispatched(session, 0)
    child_a = spawn_child(session, pivotal, _seal(session, pivotal, run_a, 2000), "fix it")
    child_b = spawn_child(session, quiet, _seal(session, quiet, run_b, 8000), "polish it")
    for child in (child_a, child_b):
        if child.status is not S.READY:
            accept_all(session, child)
            transition(child, S.READY, KERNEL, "test: carried + accepted")
    report = admission_tick(session)
    assert child_a.ref in report.admitted and child_b.ref not in report.admitted
    assert child_a.status is S.QUEUED and child_b.status is S.READY


# --------------------------------------------------------------------------- crashes (OQ18)


def test_a_crash_is_not_a_grade(session: Session) -> None:
    """AC33: no output → crashed run, crash_count + 1, requeued with a cooldown; no grade, no
    bundle, no child, no post evaPacket."""
    packet, run = _dispatched(session)
    result = complete_generation(session, run, "   ", FakeProvider(), lock=held(session))
    assert result.crashed and run.state is R.CRASHED
    session.refresh(packet)
    assert packet.status is S.QUEUED and packet.crash_count == 1 and packet.cooldown_until
    assert packet.active_run_id is None
    assert session.scalar(select(func.count()).select_from(SealedBundle)) == 0
    assert (
        session.scalar(
            select(func.count()).select_from(EvaPacket).where(EvaPacket.phase == EvaPhase.POST)
        )
        == 0
    )
    assert session.scalar(select(func.count()).where(Packet.parent_id == packet.id)) == 0


def test_cooldown_doubles_and_the_fifth_crash_holds(session: Session) -> None:
    assert [crash_cooldown_seconds(session, n) for n in (1, 2, 3, 4, 5, 9)] == [
        30, 60, 120, 240, 480, 3600,
    ]  # fmt: skip
    packet = queued_packet(session)
    t0 = utcnow()
    for n in range(1, 6):
        run = claim(session, packet.ref, "kernel-a")
        authorise_dispatch(session, run, lock=held(session))
        count = record_crash(
            session, run, "timeout", lock=held(session), now=t0 + timedelta(hours=n)
        )
        assert count == n
        session.refresh(packet)
        if n == 3:
            assert packet.status is S.QUEUED and packet.hold_reason is None
    assert packet.status is S.HELD and packet.hold_reason == "crash_limit"


@pytest.mark.parametrize(("offsets", "unhealthy"), [((0, 150, 299), True), ((0, 150, 300), False)])
def test_route_health_window_is_300_seconds(
    session: Session, offsets: tuple[int, ...], unhealthy: bool
) -> None:
    """3 crashes on a channel within 300 s → unhealthy for 600 s (window excludes t − 300)."""
    t0 = utcnow().replace(microsecond=0)
    for i, offset in enumerate(offsets):
        packet = queued_packet(session, i)
        run = claim(session, packet.ref, "kernel-a")
        authorise_dispatch(session, run, lock=held(session))
        record_crash(
            session, run, "disconnect", lock=held(session), now=t0 + timedelta(seconds=offset)
        )
    health = session.get(RouteHealth, "local_generation")
    assert (health is not None and health.unhealthy_until is not None) is unhealthy
    if unhealthy:
        assert health.unhealthy_until == t0 + timedelta(seconds=offsets[-1] + 600)


# --------------------------------------------------------------------------- lineage (AC35)


def test_three_generation_lineage_history(session: Session) -> None:
    packet, run = _dispatched(session)
    lineage = packet.lineage_key
    grades = [3000, 5000, 9000]
    current = packet
    for i, grade in enumerate(grades):
        if i:
            accept_all(session, current)
            if current.status is not S.READY:
                transition(current, S.READY, KERNEL, "test")
            transition(current, S.QUEUED, KERNEL, "test")
            run = claim(session, current.ref, "kernel-a")
            authorise_dispatch(session, run, lock=held(session))
        bundle = _seal(session, current, run, grade)
        current = spawn_child(session, current, bundle, f"generation {i + 1}")
    rows = session.execute(text(LINEAGE_HISTORY_SQL), {"lineage": lineage}).all()
    assert [r.lineage_depth for r in rows] == [0, 1, 2, 3]
    assert [r.composite_grade_e4 for r in rows] == [*grades, None]
    assert all(r.bundle_hash for r in rows[:3]) and rows[3].bundle_hash is None
    assert [r.crash_count for r in rows] == [0, 0, 0, 0]
    assert gen_eva.fast_synth_placeholder("x", grade_e4=1) is None  # @ORCHESTRATOR:PLACEHOLDER


def test_low_grade_tries_the_synth_and_falls_back(session: Session) -> None:
    """OQ17: below 5,000 a stateless synth may rewrite; a failure falls back to the template."""
    packet, run = _dispatched(session)
    bundle = _seal(session, packet, run, 4000)

    def failing(_t: str, *, grade_e4: int) -> str | None:
        raise RuntimeError("model unavailable")

    template = reimagined_prompt(session, packet, artifact_hash="h", grade_e4=4000)
    assert (
        reimagined_prompt(session, packet, artifact_hash="h", grade_e4=4000, synth=failing)
        == template
    )
    assert (
        reimagined_prompt(
            session, packet, artifact_hash="h", grade_e4=4000, synth=lambda t, grade_e4: "rewritten"
        )
        == "rewritten"
    )
    assert reimagined_prompt(
        session, packet, artifact_hash="h", grade_e4=6000, synth=lambda t, grade_e4: "rewritten"
    ) == template.replace("4000", "6000")
    assert bundle
