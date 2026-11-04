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
    candidate_confidence,
    decide,
    margin_ok,
    meets,
    q,
)
from tests.evafixtures import TEXT_FIELD, Item, ensure_text_field, oracle_inputs, seed_eva

D = Decimal


@pytest.mark.parametrize(
    ("conf", "threshold", "ok"),
    [
        ("0.7", "0.70", True),
        ("0.69995", "0.70", True),
        ("0.69994", "0.70", False),
        ("0.6", "0.60", True),
        ("0.5999", "0.60", False),
    ],
)
def test_meets_compares_at_four_places(conf: str, threshold: str, ok: bool) -> None:
    assert meets(D(conf), D(threshold)) is ok


@pytest.mark.parametrize(
    ("winner", "loser", "ok"),
    [
        ("0.70", "0.55", True),
        ("0.70", "0.5501", False),
        ("0.85", "0.70", True),
        ("0.7000", "0.54999", True),
    ],
)
def test_margin_is_inclusive_at_the_boundary(winner: str, loser: str, ok: bool) -> None:
    assert margin_ok(D(winner), D(loser), D("0.15")) is ok


def test_float_noise_does_not_move_a_boundary() -> None:
    noisy = 0.7 - 0.55  # 0.1499999999...
    assert q(noisy) == D("0.1500") and margin_ok(D("0.7"), D(str(0.55)), D("0.15"))


def test_candidate_confidence_formulas() -> None:
    settings = ConsensusSettings()
    assert candidate_confidence(FieldKind.SCORE, 7, 6, settings) == D("0.6600")
    assert candidate_confidence(FieldKind.TEXT, 7, None, settings) == D("0.7000")
    assert candidate_confidence(FieldKind.SCORE, None, 6, settings) is None


def test_tie_has_no_winning_evidence_scope() -> None:
    item = ItemInput(FieldKind.SCORE, "8", "5", {3: 8, 4: 8, 5: 8, 6: 8})
    decision = decide(item)
    assert decision.status is ConsensusStatus.DISPUTED and decision.supports == 0


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


def test_sql_matches_the_oracle_on_random_states(session: Session, tree) -> None:
    ensure_text_field(session)
    rng = random.Random(20261104)
    packet = tree.packets[0]
    checked = 0
    for _ in range(80):
        name = rng.choice(list(FIELDS))
        kind = FIELDS[name]
        keys = (
            rng.sample(["creative", "plan", "technical"], 2) if kind is FieldKind.TAGSET else [""]
        )
        items = {k: _item(rng, kind) for k in keys}
        missing = {rng.choice([2, 3, 4, 5, 6])} if rng.random() < 0.15 else set()
        eva = seed_eva(session, packet, name, items, missing=missing)
        rows = {
            r.item_key: r
            for r in session.execute(
                text("SELECT * FROM v_eva_item_consensus WHERE eva_packet_id = :id"), {"id": eva.id}
            )
        }
        for key, item_input in oracle_inputs(kind, items, missing).items():
            oracle, row = decide(item_input), rows[key]
            sql_conf = None if row.confidence is None else q(row.confidence)
            assert (row.status, sql_conf) == (oracle.status.value, oracle.confidence), (name, items)
            assert (row.winning_value if row.status == "accepted" else None) == oracle.final_value
            assert (row.supports, row.contradictions, row.unverified) == (
                oracle.supports,
                oracle.contradictions,
                oracle.unverified,
            )
            checked += 1
    assert checked >= 80
