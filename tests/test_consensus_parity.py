"""The pure consensus oracle at its boundaries (D5 point 1) and randomised SQL parity (D5 point 5).

Integer scores cannot reach some exact boundaries (e.g. a 0.15 margin), so the comparison
functions are tested directly; then several hundred random evaPacket states are seeded and every
``v_eva_item_consensus`` row must equal ``decide()`` on SQLite and PostgreSQL.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from orchestrator.contracts import ConsensusStatus, FieldKind
from orchestrator.evaluation.consensus import (
    ConsensusSettings,
    ItemInput,
    PassEvidence,
    candidate_confidence,
    decide,
    e4,
    margin_ok,
    meets,
)
from orchestrator.repository import set_setting
from tests.conftest import USER
from tests.evafixtures import TEXT_FIELD, Item, ensure_text_field, oracle_inputs, seed_eva

D = Decimal


@pytest.mark.parametrize(
    ("conf_e4", "threshold", "ok"),
    [
        (7000, "0.70", True),
        (6999, "0.70", False),
        (6000, "0.6", True),
        (5999, "0.60", False),
        (4500, "0.45", True),
        (4499, "0.45", False),
    ],
)
def test_meets_compares_integers(conf_e4: int, threshold: str, ok: bool) -> None:
    assert meets(conf_e4, threshold) is ok


@pytest.mark.parametrize(
    ("winner", "loser", "ok"),
    [(7000, 5500, True), (7000, 5501, False), (8500, 7000, True), (7000, 5499, True)],
)
def test_margin_is_inclusive_at_the_boundary(winner: int, loser: int, ok: bool) -> None:
    assert margin_ok(winner, loser, "0.15") is ok


def test_settings_are_quantised_once_half_up() -> None:
    assert (e4("0.6"), e4(0.15), e4("0.5001"), e4("0.00005"), e4(0.7)) == (
        6000,
        1500,
        5001,
        1,
        7000,
    )


def test_candidate_confidence_formulas() -> None:
    settings = ConsensusSettings()
    assert candidate_confidence(FieldKind.SCORE, 7, 6, settings) == 6600
    assert candidate_confidence(FieldKind.TEXT, 7, None, settings) == 7000
    assert candidate_confidence(FieldKind.SCORE, None, 6, settings) is None
    odd = ConsensusSettings(cross_weight=D("0.5001"), self_weight=D("0.4999"))
    assert candidate_confidence(FieldKind.SCORE, 2, 7, odd) == 4500  # 44995 → half up


def test_mc5_boundary_case() -> None:
    """R2 MC5: weights 0.5001/0.4999, one-sided, P4=2, P5=7, one support, one_sided_min 0.45."""
    settings = ConsensusSettings(
        cross_weight=D("0.5001"), self_weight=D("0.4999"), one_sided_min=D("0.45")
    )
    item = ItemInput(FieldKind.SCORE, "6", None, {4: 2, 5: 7}, {4: PassEvidence(1, 0, 0)})
    decision = decide(item, settings)
    assert decision.status is ConsensusStatus.ACCEPTED and decision.confidence_e4 == 4500


def test_tie_has_no_winning_evidence_scope() -> None:
    item = ItemInput(FieldKind.SCORE, "8", "5", {3: 8, 4: 8, 5: 8, 6: 8})
    decision = decide(item)
    assert decision.status is ConsensusStatus.DISPUTED and decision.supports == 0


def test_ruling5_disputed_agreement_reports_the_higher_confidence() -> None:
    item = ItemInput(
        FieldKind.SCORE, "8", "8", {3: 5, 4: 6, 5: 6, 6: 7}, {4: PassEvidence(1, 0, 0)}
    )
    decision = decide(item)  # conf_a 0.60, conf_b 0.58, mean 0.59 < 0.60 → disputed
    assert decision.status is ConsensusStatus.DISPUTED and decision.confidence == D("0.6000")
    accepted = decide(
        ItemInput(FieldKind.SCORE, "8", "8", {3: 7, 4: 5, 5: 6, 6: 6}, {4: PassEvidence(1, 0, 0)})
    )
    assert accepted.status is ConsensusStatus.ACCEPTED and accepted.confidence == D("0.6000")


def test_thresholds_come_from_settings() -> None:
    item = ItemInput(FieldKind.SCORE, "6", None, {4: 6, 5: 6}, {})
    strict = ConsensusSettings(one_sided_min=D("0.5"), min_supports=0)
    assert decide(item, strict).status is ConsensusStatus.ACCEPTED
    assert decide(item).status is ConsensusStatus.DISPUTED


# --------------------------------------------------------------------------- randomised parity

FIELDS = {
    "coding_intensity": FieldKind.SCORE,
    "safety_clear": FieldKind.BOOL,
    "styles": FieldKind.TAGSET,
    TEXT_FIELD: FieldKind.TEXT,
}


def _value(rng: random.Random, kind: FieldKind) -> str | None:
    if rng.random() < 0.2:
        return None
    if kind is FieldKind.SCORE:
        return str(rng.choice([3, 5, 8]))
    if kind is FieldKind.BOOL:
        return rng.choice(["true", "false"])
    if kind is FieldKind.TEXT:
        return rng.choice(["alpha", "beta"])
    return "on"


def _item(rng: random.Random, kind: FieldKind) -> Item:
    va, vb = _value(rng, kind), _value(rng, kind)
    scores = {n: rng.randint(0, 10) for n in (3, 4, 5, 6)}
    evidence = {n: (rng.randint(0, 2), rng.randint(0, 2), rng.randint(0, 1)) for n in (3, 4, 5, 6)}
    return Item(va, vb, scores, evidence)


SETTING_KEYS = {
    "cross_weight": "aggregation.cross_weight",
    "self_weight": "aggregation.self_weight",
    "agreement_min": "consensus.agreement_min",
    "winner_min": "consensus.winner_min",
    "one_sided_min": "consensus.one_sided_min",
    "dispute_margin": "aggregation.dispute_margin",
}
WEIGHTINGS = {
    "defaults": ConsensusSettings(),
    "mc5": ConsensusSettings(
        cross_weight=D("0.5001"), self_weight=D("0.4999"), one_sided_min=D("0.45")
    ),
    "odd": ConsensusSettings(
        cross_weight=D("0.7333"),
        self_weight=D("0.2667"),
        agreement_min=D("0.5551"),
        winner_min=D("0.6649"),
        one_sided_min=D("0.6667"),
        dispute_margin=D("0.1001"),
    ),
}


def _apply(session: Session, settings: ConsensusSettings) -> None:
    for attr, key in SETTING_KEYS.items():
        set_setting(session, key, float(getattr(settings, attr)), actor=USER, reason="parity")


def _compare(session: Session, eva_id: str, kind: FieldKind, items, missing, settings) -> int:  # type: ignore[no-untyped-def]
    rows = {
        r.item_key: r
        for r in session.execute(
            text("SELECT * FROM v_eva_item_consensus WHERE eva_packet_id = :id"), {"id": eva_id}
        )
    }
    for key, item_input in oracle_inputs(kind, items, missing).items():
        oracle, row = decide(item_input, settings), rows[key]
        assert (row.status, row.confidence_e4) == (oracle.status.value, oracle.confidence_e4), (
            kind,
            items,
            missing,
        )
        if oracle.status.value != "pending":
            assert (row.conf_a_e4, row.conf_b_e4) == (oracle.conf_a_e4, oracle.conf_b_e4)
        assert (row.winning_value if row.status == "accepted" else None) == oracle.final_value
        assert (row.supports, row.contradictions, row.unverified) == (
            oracle.supports,
            oracle.contradictions,
            oracle.unverified,
        )
    return len(items)


def test_mc5_case_agrees_in_sql(session: Session, tree) -> None:
    settings = WEIGHTINGS["mc5"]
    _apply(session, settings)
    items = {"": Item("6", None, {4: 2, 5: 7}, {4: (1, 0, 0)})}
    eva = seed_eva(session, tree.packets[0], "coding_intensity", items)
    _compare(session, eva.id, FieldKind.SCORE, items, set(), settings)
    row = session.execute(
        text("SELECT status, confidence_e4 FROM v_eva_item_consensus WHERE eva_packet_id = :id"),
        {"id": eva.id},
    ).one()
    assert tuple(row) == ("accepted", 4500)


@pytest.mark.parametrize("weighting", list(WEIGHTINGS))
def test_sql_matches_the_oracle_on_random_states(session: Session, tree, weighting: str) -> None:
    ensure_text_field(session)
    settings = WEIGHTINGS[weighting]
    _apply(session, settings)
    rng = random.Random(f"20261104-{weighting}")
    packet = tree.packets[0]
    checked = 0
    for _ in range(60):
        name = rng.choice(list(FIELDS))
        kind = FIELDS[name]
        keys = (
            rng.sample(["creative", "plan", "technical"], 2) if kind is FieldKind.TAGSET else [""]
        )
        items = {k: _item(rng, kind) for k in keys}
        missing = {rng.choice([2, 3, 4, 5, 6])} if rng.random() < 0.15 else set()
        eva = seed_eva(session, packet, name, items, missing=missing)
        checked += _compare(session, eva.id, kind, items, missing, settings)
    assert checked >= 60
