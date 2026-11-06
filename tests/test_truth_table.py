"""D2 truth table TC-01..TC-52 (amendment B10) on SQLite and PostgreSQL.

Consensus cases (TC-01..38) are seeded as exact evaPacket states. For each one the SQL view
``v_eva_item_consensus`` must equal the pure oracle ``decide()`` (D5 point 5), and the oracle
must equal the expectation written here. Expectations are derived from the PM rulings; the
fractional-score cases (TC-02, 07, 09, 12, 18, 25, 26, 30, 31, 32) are rewritten with integer
scores at the nearest reachable boundary (ruling 6, D5 point 1); the exact boundaries are
unit-tested on the comparison functions in ``test_consensus_oracle.py``. Where a rewritten
confidence differs from Gemini's table, ``D2_CONF`` records the table value and
``docs/PROGRESS.md`` lists the difference.

Readiness cases (TC-39..52) build a ready evaluation set and change one thing.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.contracts import (
    EvaPhase,
    FieldKind,
    OverrideKind,
    PacketStatus,
    ProjectTree,
)
from orchestrator.db import init_db, make_engine, make_session_factory
from orchestrator.db.models import EvaPreset, Packet, Waiver, new_id
from orchestrator.evaluation.consensus import decide, q
from orchestrator.evaluation.readiness import readiness
from orchestrator.events import record_event
from orchestrator.governance import (
    GovernanceError,
    add_dependency,
    add_override,
    grant_waiver,
    revoke,
)
from orchestrator.ingest import persist_tree
from orchestrator.lifecycle import transition
from orchestrator.queue import ClaimError, admit_next, claim, gen_queue_rows
from tests.conftest import DIALECT_PARAMS, KERNEL, POSTGRES_URL, SMALL_TREE, USER, _reset_postgres
from tests.evafixtures import (
    TEXT_FIELD,
    Item,
    accept_all,
    accept_post,
    add_artifact,
    complete_with_evidence,
    ensure_text_field,
    kind_of,
    oracle_inputs,
    seed_eva,
)
from tests.legacy import supersede_packet

SCORE, BOOL, TAGS, TEXT = "coding_intensity", "safety_clear", "styles", TEXT_FIELD
T1, T2 = "creative", "plan"


def s(p3: int | None, p4: int | None = None, p5: int | None = None, p6: int | None = None) -> dict:
    return {n: v for n, v in zip((3, 4, 5, 6), (p3, p4, p5, p6), strict=True) if v is not None}


def ev(**passes: tuple[int, int] | tuple[int, int, int]) -> dict:
    return {int(k[1:]): (*v, 0)[:3] for k, v in passes.items()}


@dataclass
class Case:
    id: str
    field: str
    items: dict[str, Item]
    status: str | dict[str, str]  # expected per item ("" or tag)
    conf: str | dict[str, str] | None
    final: str | dict[str, str | None] | None = None
    missing: set[int] = field(default_factory=set)
    d2_status: str | None = None  # Gemini's table, when it differs from ours


A, B = "a", "b"
CASES = [
    Case(
        "TC-01",
        SCORE,
        {"": Item("8", "8", s(6, 6, 6, 6), ev(p4=(1, 0), p5=(1, 0)))},
        "accepted",
        "0.6000",
        "8",
    ),
    Case(
        "TC-02",  # ruling 5 (M2.1 fix 10): disputed agreement reports max(0.60, 0.58)
        SCORE,
        {"": Item("8", "8", s(5, 6, 6, 7), ev(p4=(1, 0), p5=(1, 0)))},
        "disputed",
        "0.6000",
    ),
    Case(
        "TC-03",
        BOOL,
        {"": Item("true", "true", s(7, 5, 6, 6), ev(p4=(1, 0)))},
        "accepted",
        "0.6000",
        "true",
    ),
    Case(
        "TC-04",
        SCORE,
        {"": Item("7", "7", s(9, 9, 9, 9), ev(p4=(0, 0, 1), p5=(0, 0, 1)))},
        "disputed",
        "0.9000",
    ),
    Case(
        "TC-05",
        SCORE,
        {"": Item("7", "7", s(8, 8, 8, 8), ev(p4=(2, 1), p5=(0, 1)))},
        "accepted",
        "0.8000",
        "7",
    ),
    Case(
        "TC-06",
        SCORE,
        {"": Item("7", "7", s(8, 8, 8, 8), ev(p4=(2, 2), p5=(0, 1)))},
        "disputed",
        "0.8000",
    ),
    Case(
        "TC-07",
        SCORE,
        {"": Item("8", "5", s(5, 7, 7, 6), ev(p4=(1, 0), p5=(1, 0)))},
        "accepted",
        "0.7000",
        "8",
    ),
    Case(
        "TC-08",
        SCORE,
        {"": Item("8", "5", s(8, 5, 5, 8), ev(p3=(1, 0), p6=(1, 0)))},
        "accepted",
        "0.8000",
        "5",
    ),
    Case(
        "TC-09",
        SCORE,
        {"": Item("8", "5", s(3, 8, 5, 3), ev(p4=(1, 0), p5=(1, 0)))},
        "disputed",
        "0.6800",
    ),
    Case(
        "TC-10",
        SCORE,
        {"": Item("8", "5", s(6, 7, 7, 5), ev(p4=(1, 0), p5=(1, 0)))},
        "disputed",
        "0.7000",
    ),
    Case(
        "TC-11",
        SCORE,
        {"": Item("8", "5", s(8, 8, 8, 8), ev(p4=(1, 0), p5=(1, 0)))},
        "disputed",
        "0.8000",
    ),
    Case(
        "TC-12",
        BOOL,
        {"": Item("true", "false", s(6, 7, 7, 6), ev(p4=(1, 0), p5=(1, 0)))},
        "disputed",
        "0.7000",
    ),
    Case("TC-13", SCORE, {"": Item("9", "4", s(5, 8, 8, 5))}, "disputed", "0.8000"),
    Case(
        "TC-14",
        SCORE,
        {"": Item("9", "4", s(5, 8, 8, 5), ev(p4=(1, 1), p5=(0, 1)))},
        "disputed",
        "0.8000",
    ),
    Case(
        "TC-15",
        SCORE,
        {"": Item("9", "4", s(5, 8, 8, 5), ev(p4=(1, 0), p3=(0, 1), p6=(0, 1)))},
        "accepted",
        "0.8000",
        "9",
    ),
    Case(
        "TC-16",
        SCORE,
        {"": Item("6", None, s(None, 7, 7), ev(p4=(1, 0)))},
        "accepted",
        "0.7000",
        "6",
    ),
    Case(
        "TC-17",
        SCORE,
        {"": Item(None, "6", s(7, None, None, 7), ev(p3=(1, 0)))},
        "accepted",
        "0.7000",
        "6",
    ),
    Case("TC-18", SCORE, {"": Item("6", None, s(None, 8, 5), ev(p4=(1, 0)))}, "disputed", "0.6800"),
    Case("TC-19", SCORE, {"": Item("6", None, s(None, 8, 8))}, "disputed", "0.8000"),
    Case(
        "TC-20",
        SCORE,
        {"": Item("6", None, s(None, 8, 8), ev(p4=(1, 1), p5=(0, 1)))},
        "disputed",
        "0.8000",
    ),
    Case("TC-21", SCORE, {"": Item(None, None)}, "null", "0.0000"),
    Case("TC-22", BOOL, {"": Item(None, None)}, "null", "0.0000"),
    Case(
        "TC-23",
        TAGS,
        {T1: Item("on", "on", s(6, 6, 6, 6), ev(p4=(1, 0)))},
        {T1: "accepted"},
        {T1: "0.6000"},
        {T1: "on"},
    ),
    Case(
        "TC-24",
        TAGS,
        {T1: Item("on", None, s(None, 7, 7), ev(p4=(1, 0)))},
        {T1: "accepted"},
        {T1: "0.7000"},
        {T1: "on"},
    ),
    Case(
        "TC-25",
        TAGS,
        {T1: Item("on", None, s(None, 7, 6), ev(p4=(1, 0)))},
        {T1: "disputed"},
        {T1: "0.6600"},
        {T1: None},
    ),
    Case(
        "TC-26",
        TAGS,
        {
            T1: Item("on", "on", s(8, 8, 8, 8), ev(p4=(1, 0))),
            T2: Item("on", None, s(None, 8, 7), ev(p4=(1, 0))),
        },
        {T1: "accepted", T2: "accepted"},
        {T1: "0.8000", T2: "0.7600"},
        {T1: "on", T2: "on"},
    ),
    Case(
        "TC-27",
        TAGS,
        {
            T1: Item("on", "on", s(8, 8, 8, 8), ev(p4=(1, 0))),
            T2: Item("on", None, s(None, 6, 6), ev(p4=(1, 0))),
        },
        {T1: "accepted", T2: "disputed"},
        {T1: "0.8000", T2: "0.6000"},
        {T1: "on", T2: None},
    ),
    Case(
        "TC-28",
        TAGS,
        {T1: Item("on", "on", s(8, 8, 8, 8), ev(p4=(1, 1), p3=(0, 1)))},
        {T1: "disputed"},
        {T1: "0.8000"},
        {T1: None},
    ),
    Case(
        "TC-29",
        TEXT,
        {"": Item("alpha", "alpha", s(6, 6), ev(p4=(1, 0)))},
        "accepted",
        "0.6000",
        "alpha",
    ),
    # mean 0.55 < 0.60 → disputed; reports max(conf_a 0.50, conf_b 0.60) (ruling 5, fix 10)
    Case("TC-30", TEXT, {"": Item("alpha", "alpha", s(6, 5), ev(p4=(1, 0)))}, "disputed", "0.6000"),
    Case(
        "TC-31",
        TEXT,
        {"": Item("alpha", "beta", s(5, 7), ev(p4=(2, 0)))},
        "accepted",
        "0.7000",
        "alpha",
    ),
    Case("TC-32", TEXT, {"": Item("alpha", "beta", s(6, 7), ev(p4=(2, 0)))}, "disputed", "0.7000"),
    Case(
        "TC-33",
        TEXT,
        {"": Item("alpha", None, s(None, 7), ev(p4=(1, 0)))},
        "accepted",
        "0.7000",
        "alpha",
    ),
    Case(
        "TC-34",
        TEXT,
        {"": Item("alpha", "beta", s(8, 8), ev(p4=(1, 0), p3=(1, 0)))},
        "disputed",
        "0.8000",
    ),
    Case("TC-35", SCORE, {"": Item("8", None)}, "pending", None, missing={2, 3, 4, 5, 6}),
    Case("TC-36", SCORE, {"": Item("8", "8", s(8, 8, 8))}, "pending", None, missing={6}),
    Case("TC-37", TEXT, {"": Item("alpha", "alpha", s(8))}, "pending", None, missing={4}),
]
#: Gemini's confidences where the integer rewrite reaches a different value (ruling 6).
D2_CONF = {"TC-02": "0.59", "TC-09": "0.69", "TC-18": "0.69", "TC-25": "0.65", "TC-30": "0.59"}


# --------------------------------------------------------------------------- one DB per dialect


@pytest.fixture(scope="module", params=DIALECT_PARAMS)
def factory(request: pytest.FixtureRequest, tmp_path_factory) -> Iterator[sessionmaker[Session]]:  # type: ignore[no-untyped-def]
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp('tt') / 'tt.db'}"
    else:
        assert POSTGRES_URL is not None
        _reset_postgres(POSTGRES_URL)
        url = POSTGRES_URL
    engine: Engine = make_engine(url)
    init_db(engine)
    f = make_session_factory(engine)
    with f() as session:
        ensure_text_field(session)
        session.commit()
    yield f
    engine.dispose()


def _packet(session: Session) -> Packet:
    result = persist_tree(session, ProjectTree.model_validate(SMALL_TREE), actor=USER)
    return result.packets[0]


def _expect(value, key):  # type: ignore[no-untyped-def]
    return value.get(key) if isinstance(value, dict) else value


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_consensus_case(factory: sessionmaker[Session], case: Case) -> None:
    with factory() as session:
        packet = _packet(session)
        eva = seed_eva(session, packet, case.field, case.items, missing=case.missing)
        kind = kind_of(session, case.field)
        rows = {
            r.item_key: r
            for r in session.execute(
                text("SELECT * FROM v_eva_item_consensus WHERE eva_packet_id = :id"),
                {"id": eva.id},
            )
        }
        for key, item_input in oracle_inputs(kind, case.items, case.missing).items():
            oracle = decide(item_input)
            row = rows[key]
            # 1. the SQL view equals the oracle (parity)
            assert row.status == oracle.status.value, (case.id, key)
            assert row.confidence_e4 == oracle.confidence_e4, (case.id, key)
            assert (None if row.confidence is None else q(row.confidence)) == oracle.confidence
            assert (row.winning_value if row.status == "accepted" else None) == oracle.final_value
            assert bool(row.agreement) == oracle.agreement
            assert (row.supports, row.contradictions, row.unverified) == (
                oracle.supports,
                oracle.contradictions,
                oracle.unverified,
            )
            # 2. the oracle equals the expectation derived from the rulings
            assert oracle.status.value == _expect(case.status, key), (case.id, key)
            expected_conf = _expect(case.conf, key)
            assert oracle.confidence == (Decimal(expected_conf) if expected_conf else None)
            assert oracle.final_value == _expect(case.final, key)
        if kind is FieldKind.TAGSET:  # vocabulary tags neither model chose → null rows (D5 2)
            others = {k: r.status for k, r in rows.items() if k not in case.items}
            assert others and set(others.values()) == {"null"}
        session.rollback()


def test_tc38_version_binding(factory: sessionmaker[Session]) -> None:
    """An incomplete cycle on v2 is pending even though v1 has a complete one (ruling 7)."""
    with factory() as session:
        v1 = _packet(session)
        seed_eva(session, v1, SCORE, {"": Item("8", "8", s(8, 8, 8, 8), ev(p4=(1, 0)))})
        v1_rows = (
            session.execute(
                text(
                    "SELECT status FROM v_field_consensus WHERE packet_id = :id AND field_name = :f"
                ),
                {"id": v1.id, "f": SCORE},
            )
            .scalars()
            .all()
        )
        assert v1_rows == ["accepted"]
        v2 = supersede_packet(session, v1.ref, {"title": "Write the parser v2"}, actor=USER)
        seed_eva(session, v2, SCORE, {"": Item("8", None)}, missing={2, 3, 4, 5, 6})
        current = session.execute(
            text(
                "SELECT packet_version, status, confidence FROM v_field_consensus "
                "WHERE packet_ref = :r AND field_name = :f"
            ),
            {"r": v1.ref, "f": SCORE},
        ).all()
        assert [(r.packet_version, r.status, r.confidence) for r in current] == [
            (2, "pending", None)
        ]
        assert not readiness(session, v2).ready
        session.rollback()


# --------------------------------------------------------------------------- readiness TC-39..52


def _gate(session: Session, field_name: str, op: str, value: str, tier: int = 2) -> None:
    """A new preset revision with a value gate (presets are append-only)."""
    session.add(
        EvaPreset(
            id=new_id(),
            field_name=field_name,
            phase="pre",
            revision=2,
            tier=tier,
            required=True,
            hard=False,
            gate_operator=op,
            gate_value=value,
            plan="combined",
            prompt_template="quickfire.v1",
            schema_version="1.0",
            policy_version="1.0",
            enabled=True,
        )
    )
    record_event(session, actor=USER, action="test.preset", entity_type="eva_preset")
    session.flush()


def _ready_packet(session: Session, *, skip: frozenset[str] = frozenset()) -> Packet:
    packet = _packet(session)
    transition(packet, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, packet, skip=skip)
    return packet


EIGHTS, SUP = s(8, 8, 8, 8), ev(p4=(1, 0), p5=(1, 0))


def test_tc39_to_tc44_soft_waivers(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        _gate(session, "reasoning_intensity", ">=", "7")
        # TC-39: accepted 5, gate >= 7 → blocks
        p = _ready_packet(session, skip=frozenset({"reasoning_intensity"}))
        seed_eva(session, p, "reasoning_intensity", {"": Item("5", "5", EIGHTS, SUP)})
        report = readiness(session, p)
        assert not report.ready and report.waivable == ["reasoning_intensity"]
        # TC-40: a valid soft waiver → ready
        waiver = grant_waiver(session, p.ref, "reasoning_intensity", actor=USER, reason="fine")
        assert readiness(session, p).ready and readiness(session, p).waived == [
            "reasoning_intensity"
        ]
        # TC-44: the waiver is revoked → blocks again
        revoke(session, "waiver", waiver.id, actor=USER, reason="changed my mind")
        assert not readiness(session, p).ready
        # TC-43: a waiver bound to v1 lapses on v2
        grant_waiver(session, p.ref, "reasoning_intensity", actor=USER, reason="v1 only")
        transition(p, PacketStatus.NEEDS_REVIEW, KERNEL)
        v2 = supersede_packet(session, p.ref, {"title": "Write the parser again"}, actor=USER)
        accept_all(session, v2, skip=frozenset({"reasoning_intensity"}))
        seed_eva(session, v2, "reasoning_intensity", {"": Item("5", "5", EIGHTS, SUP)})
        assert not readiness(session, v2).ready
        # TC-41 (disputed) and TC-42 (null) soft fields with a waiver → ready
        p3 = _ready_packet(
            session, skip=frozenset({"tools_needed", "context_volume", "reasoning_intensity"})
        )
        seed_eva(session, p3, "reasoning_intensity", {"": Item("8", "8", EIGHTS, SUP)})
        seed_eva(session, p3, "tools_needed", {"other": Item("on", "on", s(4, 4, 4, 4), SUP)})
        seed_eva(session, p3, "context_volume", {"": Item(None, None)})
        assert sorted(readiness(session, p3).waivable) == ["context_volume", "tools_needed"]
        grant_waiver(session, p3.ref, "tools_needed", actor=USER, reason="ok")
        grant_waiver(session, p3.ref, "context_volume", actor=USER, reason="ok")
        assert readiness(session, p3).ready
        session.rollback()


def test_tc45_tc46_hard_gate_never_waived_or_forced(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        p = _ready_packet(session, skip=frozenset({BOOL}))
        seed_eva(session, p, BOOL, {"": Item("false", "false", s(9, 9, 9, 9), SUP)})
        report = readiness(session, p)
        assert not report.ready and report.needs_review and "hard gate" in report.blockers[0]
        with pytest.raises(GovernanceError, match="hard gate"):
            grant_waiver(session, p.ref, BOOL, actor=USER, reason="please")
        # even a waiver row written behind the API's back is ignored for a hard field
        session.add(
            Waiver(
                id=new_id(),
                packet_id=p.id,
                packet_ref=p.ref,
                target_version=p.version,
                field_name=BOOL,
                phase="pre",
                actor="user:x",
                reason="sneaky",
            )
        )
        record_event(session, actor=USER, action="t", entity_type="waiver")
        session.flush()
        assert not readiness(session, p).ready
        # TC-46: FORCE changes priority only — not admitted, cannot be claimed
        transition(p, PacketStatus.READY, KERNEL, "test: force a status")
        add_override(session, p.ref, OverrideKind.FORCE, actor=USER, reason="urgent")
        assert admit_next(session) is None or admit_next(session).ref != p.ref
        transition(p, PacketStatus.QUEUED, KERNEL, "test: bypass admission")
        with pytest.raises(ClaimError, match="no longer ready"):
            claim(session, p.ref, "worker-1")
        session.rollback()


def test_tc47_force_is_not_a_waiver(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        p = _ready_packet(session, skip=frozenset({"ambiguity_quotient"}))
        seed_eva(session, p, "ambiguity_quotient", {"": Item("8", "4", s(6, 6, 6, 6), SUP)})
        add_override(session, p.ref, OverrideKind.FORCE, actor=USER, reason="urgent")
        report = readiness(session, p)
        assert not report.ready and report.waivable == ["ambiguity_quotient"]
        session.rollback()


def test_tc48_tc49_dependencies_and_approvals(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        p = _ready_packet(session)
        assert readiness(session, p).ready
        other = _packet(session)
        add_dependency(session, p.ref, other.ref, actor=USER)
        assert any("dependency" in b for b in readiness(session, p).blockers)
        accept_all(session, p)  # R6 OQ10: a dependency makes two more fields mandatory
        complete_with_evidence(session, other, USER)
        assert readiness(session, p).ready
        from orchestrator.db.models import Confirmation

        session.add(
            Confirmation(
                id=new_id(),
                stage="routing",
                entity_type="packet",
                entity_id=p.id,
                entity_ref=p.ref,
                summary="approve me",
            )
        )
        record_event(session, actor=USER, action="t", entity_type="confirmation")
        session.flush()
        assert any("approval" in b for b in readiness(session, p).blockers)
        session.rollback()


@pytest.mark.parametrize("kind", [OverrideKind.HOLD, OverrideKind.BLOCK])
def test_tc50_hold_or_block_stops_admission(factory: sessionmaker[Session], kind) -> None:  # type: ignore[no-untyped-def]
    with factory() as session:
        p = _ready_packet(session)
        transition(p, PacketStatus.READY, KERNEL)
        assert readiness(session, p).ready  # readiness itself is unaffected
        add_override(session, p.ref, kind, actor=USER, reason="stop", scope="lineage")
        assert p.id not in {r.packet_id for r in gen_queue_rows(session)}  # R6 OQ2: held
        admitted = admit_next(session)
        assert admitted is None or admitted.ref != p.ref
        session.rollback()


def test_tc51_tier3_never_gates(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        p = _ready_packet(session)
        seed_eva(session, p, "web_independence", {"": Item("8", "3", s(6, 6, 6, 6), SUP)})
        status = session.execute(
            text("SELECT status FROM v_field_status WHERE packet_id = :id AND field_name = :f"),
            {"id": p.id, "f": "web_independence"},
        ).scalar_one()
        assert status == "disputed" and readiness(session, p).ready
        session.rollback()


def _to_post(session: Session) -> Packet:
    p = _ready_packet(session)
    for status in (
        PacketStatus.READY,
        PacketStatus.QUEUED,
        PacketStatus.RUNNING,
        PacketStatus.EVALUATING_POST,
    ):
        transition(p, status, KERNEL)
    return p


def _post_blockers(session, packet):  # type: ignore[no-untyped-def]
    """M3a (R6 OQ13): ``evaluating_post → completed`` is retired (a genPacket seals after
    ``gen_eva``); the post-phase gate logic of TC-52 and M2.1 fixes 3/5 is still checked."""
    from orchestrator.evaluation.readiness import completion_blockers

    return completion_blockers(session, packet, PacketStatus.EVALUATING_POST)


def test_tc52_post_evaluation_gates_completion(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        p = _to_post(session)
        art = add_artifact(session, p)
        for name in ("requirement_alignment", "security_risk"):
            seed_eva(
                session,
                p,
                name,
                {"": Item("3", "3", EIGHTS, SUP)},
                phase="post",
                target_hash=art.sha256,
            )
        seed_eva(
            session,
            p,
            "correctness",
            {"": Item("8", "4", s(6, 6, 6, 6), SUP)},
            phase="post",
            target_hash=art.sha256,
        )
        assert _post_blockers(session, p)
        assert _post_blockers(session, p)
        session.rollback()
        # all post fields accepted and within their gates → the kernel may complete and lock
        p = _to_post(session)
        accept_post(session, p, add_artifact(session, p))
        assert not _post_blockers(session, p)
        session.rollback()


def test_post_consensus_binds_the_exact_newest_artifact(factory: sessionmaker[Session]) -> None:
    """M2.1 fix 3: accepted artifact A, then an incomplete evaluation of artifact B, must not
    complete; nor may B complete on A's evaluation alone."""
    with factory() as session:
        p = _to_post(session)
        accept_post(session, p, add_artifact(session, p, "artifact A"))
        b = add_artifact(session, p, "artifact B")  # the newest artifact
        seed_eva(
            session,
            p,
            "correctness",
            {"": Item("8", None)},
            phase="post",
            target_hash=b.sha256,
            missing={2, 3, 4, 5, 6},
        )
        statuses = dict(
            session.execute(
                text(
                    "SELECT field_name, status FROM v_field_status WHERE packet_id = :id "
                    "AND phase = 'post'"
                ),
                {"id": p.id},
            ).all()
        )
        assert statuses["correctness"] == "pending"
        assert statuses["requirement_alignment"] == "pending"  # A's result is not B's
        assert _post_blockers(session, p)
        accept_post(session, p, b)  # B fully evaluated and accepted → completes
        assert not _post_blockers(session, p)
        session.rollback()


def test_soft_post_field_passes_only_with_a_post_phase_waiver(factory) -> None:  # type: ignore[no-untyped-def]
    """M2.1 fix 5: a disputed soft post field needs a version- and phase-bound waiver; a hard
    post gate (security_risk) is never waivable, not even for a user."""
    with factory() as session:
        p = _to_post(session)
        art = add_artifact(session, p)
        accept_post(session, p, art)
        seed_eva(
            session,
            p,
            "correctness",
            {"": Item("8", "4", s(6, 6, 6, 6), SUP)},
            phase="post",
            target_hash=art.sha256,
        )
        assert _post_blockers(session, p)
        grant_waiver(session, p.ref, "correctness", actor=USER, reason="fine", phase=EvaPhase.POST)
        seed_eva(
            session,
            p,
            "security_risk",
            {"": Item("9", "9", EIGHTS, SUP)},
            phase="post",
            target_hash=art.sha256,
        )
        with pytest.raises(GovernanceError, match="hard gate"):
            grant_waiver(
                session, p.ref, "security_risk", actor=USER, reason="x", phase=EvaPhase.POST
            )
        assert any("hard gate" in b for b in _post_blockers(session, p))
        seed_eva(
            session,
            p,
            "security_risk",
            {"": Item("2", "2", EIGHTS, SUP)},
            phase="post",
            target_hash=art.sha256,
        )
        assert not _post_blockers(session, p)
        session.rollback()


def test_phase_constants() -> None:
    assert EvaPhase.PRE.value == "pre" and len(CASES) == 37  # + TC-38 and TC-39..52 above
