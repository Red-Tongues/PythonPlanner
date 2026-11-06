"""M3a.1: every setting moved out of the code changes behaviour when changed (both dialects).

One test per moved setting: the default behaviour, then ``set_setting`` (validated, evented),
then the changed behaviour.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from orchestrator.contracts import PacketStatus
from orchestrator.evaluation.gen_eva import composite_grade_e4, composite_weights
from orchestrator.evaluation.readiness import readiness
from orchestrator.governance import add_dependency
from orchestrator.lifecycle import transition
from orchestrator.queue import gen_queue_rows, is_locked_out, write_epoch_snapshot
from orchestrator.repository import set_setting
from orchestrator.runs import crash_cooldown_seconds
from tests.conftest import KERNEL, USER
from tests.evafixtures import Item, accept_all, seed_eva
from tests.test_m3a_queue import NOW, _feedback, _packets, _ready, _score

EIGHTS = {3: 8, 4: 8, 5: 8, 6: 8}
SUP = {4: (1, 0, 0), 5: (1, 0, 0)}


def _set(session: Session, key: str, value: object) -> None:
    set_setting(session, key, value, actor=USER, reason="tunability test")


def test_out_of_range_values_are_refused(session: Session) -> None:
    with pytest.raises(ValueError):
        _set(session, "lockout.max_position", -1)


# --------------------------------------------------------------------------- feedback decay


@pytest.mark.parametrize(
    ("key", "value", "age", "default_e4", "changed_e4"),
    [
        ("feedback.full_window_sec", 3600, timedelta(hours=2), 1500, 750),
        ("feedback.half_window_sec", 100_000, timedelta(hours=30), 750, 0),
        ("feedback.decay_mid_e4", 2500, timedelta(hours=30), 750, 375),
    ],
)
def test_feedback_decay_is_tunable(
    session: Session, key: str, value: int, age: timedelta, default_e4: int, changed_e4: int
) -> None:
    p = _ready(session, _packets(session, 1)[0])
    _feedback(session, p, "boost", "u", at=NOW - age)
    assert _score(session, p)["feedback_e4"] == default_e4
    _set(session, key, value)
    assert _score(session, p)["feedback_e4"] == changed_e4


# --------------------------------------------------------------------------- lockout depth


def test_lockout_depth_is_tunable(session: Session) -> None:
    packets = [_ready(session, p) for p in _packets(session, 4)]
    third = next(r for r in gen_queue_rows(session, NOW) if r.gen_position == 3).packet_id
    write_epoch_snapshot(session, "test", now=NOW)
    assert not is_locked_out(session, third)  # positions 0-2 only
    _set(session, "lockout.max_position", 3)
    write_epoch_snapshot(session, "test", now=NOW)
    assert is_locked_out(session, third)
    assert packets


# --------------------------------------------------------------------------- composite grade


def test_composite_weights_and_sonar_shift_are_tunable(session: Session) -> None:
    scores = {"requirement_alignment": 9000, "testability": 1000}
    before = composite_grade_e4(scores, composite_weights(session), 500)
    _set(session, "gen_eva.w_requirement_alignment_e4", 6000)
    assert composite_weights(session)["requirement_alignment"] == 6000
    assert composite_grade_e4(scores, composite_weights(session), 500) > before
    from orchestrator.settings_store import get_int

    _set(session, "gen_eva.sonar_na_shift_e4", 0)
    assert get_int(session, "gen_eva.sonar_na_shift_e4") == 0
    assert composite_grade_e4(scores, composite_weights(session), 0) < composite_grade_e4(
        scores, composite_weights(session), 500
    )


# --------------------------------------------------------------------------- consensus (SQL view)


def _status(session: Session, packet_id: str, field: str = "coding_intensity") -> str:
    return session.execute(
        text(
            "SELECT status FROM v_field_status WHERE packet_id = :id AND field_name = :f "
            "AND phase = 'pre'"
        ),
        {"id": packet_id, "f": field},
    ).scalar()


@pytest.mark.parametrize(
    ("key", "value", "item"),
    [
        ("consensus.agreement_min", 0.85, Item("5", "5", EIGHTS, SUP)),  # agree at 0.80
        ("consensus.one_sided_min", 0.9, Item("5", None, EIGHTS, SUP)),  # a only, 0.80
        ("consensus.winner_min", 0.85, Item("5", "6", {3: 5, 4: 8, 5: 8, 6: 5}, SUP)),
        ("aggregation.dispute_margin", 0.35, Item("5", "6", {3: 5, 4: 8, 5: 8, 6: 5}, SUP)),
        ("consensus.min_supports", 3, Item("5", "5", EIGHTS, SUP)),  # two supports
    ],
)
def test_consensus_thresholds_are_tunable(
    session: Session, key: str, value: object, item: Item
) -> None:
    p = _packets(session, 1)[0]
    transition(p, PacketStatus.EVALUATING_PRE, KERNEL)
    seed_eva(session, p, "coding_intensity", {"": item})
    assert _status(session, p.id) == "accepted"
    _set(session, key, value)
    assert _status(session, p.id) == "disputed"


# --------------------------------------------------------------------------- readiness


def test_the_ambiguity_bound_is_tunable(session: Session) -> None:
    p = _packets(session, 1)[0]
    transition(p, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, p, skip=frozenset({"ambiguity_quotient"}))
    seed_eva(session, p, "ambiguity_quotient", {"": Item("5", "5", EIGHTS, SUP)})
    assert not readiness(session, p).ready
    _set(session, "readiness.max_ambiguity_e4", 6000)
    assert readiness(session, p).ready


def test_the_gating_tier_is_tunable(session: Session) -> None:
    p = _packets(session, 1)[0]
    transition(p, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, p, skip=frozenset({"tools_needed"}))  # a required tier-2 field
    assert any("tools_needed" in b for b in readiness(session, p).blockers)
    _set(session, "readiness.max_gating_tier", 1)
    assert not any("tools_needed" in b for b in readiness(session, p).blockers)
    mandatory = session.execute(
        text("SELECT field_name FROM v_packet_mandatory_fields WHERE packet_id = :id"),
        {"id": p.id},
    ).scalars()
    assert "tools_needed" not in set(mandatory)  # the SQL mirror follows the same setting


# --------------------------------------------------------------------------- queue maths


def test_the_centrality_cap_is_tunable(session: Session) -> None:
    a, b, c, d = _packets(session, 4)
    for dependent in (b, c, d):
        add_dependency(session, dependent.ref, a.ref, actor=USER)

    def centrality() -> int:
        return int(
            session.execute(
                text(
                    "SELECT centrality_e4 FROM v_packet_relational_centrality WHERE packet_id = :id"
                ),
                {"id": a.id},
            ).scalar()
        )

    assert centrality() == 9000
    _set(session, "centrality.cap_e4", 4000)
    assert centrality() == 4000


def test_the_eva_question_multiplier_is_tunable(session: Session) -> None:
    from orchestrator.db.models import EvaContextLink, new_id

    p = _ready(session, _packets(session, 1)[0])
    eva = seed_eva(session, p, "testability", {"": Item("3", "3", EIGHTS, SUP)})
    session.add(
        EvaContextLink(
            id=new_id(), eva_packet_id=eva.id, packet_id=p.id, field_name="testability",
            source_type="project", source_id=p.project_id, source_hash="h", source_revision=0,
            link_strength="direct", link_reason="carried", is_questioned=True,
        )
    )  # fmt: skip
    session.flush()
    row = _score(session, p)
    assert row["eva_priority_e4"] == row["base_score_e4"] + 2 * 750
    _set(session, "gen_queue.eva_question_multiplier", 3)
    row = _score(session, p)
    assert row["eva_priority_e4"] == row["base_score_e4"] + 3 * 750


def test_the_crash_cooldown_factor_is_tunable(session: Session) -> None:
    assert crash_cooldown_seconds(session, 3) == 120
    _set(session, "crash.cooldown_factor", 3)
    assert crash_cooldown_seconds(session, 3) == 270


# --------------------------------------------------------------------------- prompt, branching


def test_prompt_limits_are_tunable(session: Session) -> None:
    from orchestrator.evaluation.gen_eva import complete_generation, reimagined_prompt
    from orchestrator.llm import FakeProvider
    from orchestrator.queue import claim
    from orchestrator.runs import authorise_dispatch
    from tests.runfixtures import held, queued_packet

    packet = queued_packet(session)
    run = claim(session, packet.ref, "kernel-a")
    authorise_dispatch(session, run, lock=held(session))
    complete_generation(session, run, "output", FakeProvider(), lock=held(session))

    def critiques() -> int:
        prompt = reimagined_prompt(session, packet, artifact_hash="h", grade_e4=6000)
        return sum(1 for line in prompt.splitlines() if "/10:" in line)

    assert critiques() == 3
    _set(session, "gen_eva.critique_count", 1)
    assert critiques() == 1


def test_branching_limits_are_tunable(session: Session, tree) -> None:  # type: ignore[no-untyped-def]
    from orchestrator.branching import apply_branch_triggers, children_of
    from orchestrator.contracts import AutonomyMode, Stage
    from orchestrator.llm import FakeProvider
    from orchestrator.repository import set_autonomy_mode

    _set(session, "branching.position_stride", 10)
    set_autonomy_mode(session, Stage.BRANCHING, AutonomyMode.AUTO, actor=USER)
    trunk = tree.packets[0]
    transition(trunk, PacketStatus.EVALUATING_PRE, KERNEL)
    accept_all(session, trunk, skip=frozenset({"task_parsability"}))
    seed_eva(session, trunk, "task_parsability", {"": Item("8", "8", EIGHTS, SUP)})
    transition(trunk, PacketStatus.READY, KERNEL)
    apply_branch_triggers(session, FakeProvider())
    positions = [c.position for c in children_of(session, trunk)]
    assert positions and all(pos // 10 == trunk.position for pos in positions)
