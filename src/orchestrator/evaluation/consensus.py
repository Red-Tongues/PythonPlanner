"""The consensus decision table as a pure function — the reference oracle (D5 point 5).

``v_eva_item_consensus`` (SQL) is the implementation the kernel uses; this module states the same
rules in plain Python so they can be read, unit-tested at their boundaries and compared with the
SQL row for row on both dialects (``tests/test_consensus_parity.py``).

Rules (PROJECT_PLAN §7, B2, B3, B10 rulings 1–6, D5 clarifications):

* ``conf_x = (cross_w · cross + self_w · self) / 10`` for each non-null candidate; text fields use
  the cross score only (``conf_a = P4 / 10``, ``conf_b = P3 / 10``).
* Incomplete (an applicable pass is missing) → ``pending``, confidence NULL.
* Both NULL → ``null``, confidence 0.
* Agreement → confidence ``mean(conf_a, conf_b)``; accepted at ≥ ``agreement_min``.
* One NULL → the other's confidence; accepted at ≥ ``one_sided_min``.
* Disagreement → the higher confidence wins; accepted at ≥ ``winner_min`` with a margin
  ≥ ``dispute_margin``; an exact tie has no winner and is disputed. Reported confidence is the
  higher one.
* Evidence counts only from passes that evaluate the *winning* value (P4+P5 for A, P3+P6 for B,
  all on agreement, none on a tie). Acceptance needs ≥ ``min_supports`` verified SUPPORT quotes
  and contradictions ≤ supports.
* Every confidence is rounded to 4 decimal places before any comparison (ruling 6).

Judgement call (recorded in PROGRESS): ruling 5 says a disputed row reports
``max(conf_a, conf_b)``; ruling 1 defines the agreement confidence as the mean. For a *disputed
agreement* this module (and the SQL) reports the mean — the confidence the decision was made on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from orchestrator.contracts import ConsensusStatus, FieldKind

Q = Decimal("0.0001")


def q(value: Decimal | float | int) -> Decimal:
    """Round to 4 dp, half away from zero (SQLite ``ROUND`` and PostgreSQL ``numeric`` agree)."""
    return Decimal(str(value)).quantize(Q, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ConsensusSettings:
    cross_weight: Decimal = Decimal("0.6")
    self_weight: Decimal = Decimal("0.4")
    agreement_min: Decimal = Decimal("0.60")
    winner_min: Decimal = Decimal("0.70")
    one_sided_min: Decimal = Decimal("0.70")
    dispute_margin: Decimal = Decimal("0.15")
    min_supports: int = 1


@dataclass(frozen=True)
class PassEvidence:
    supports: int = 0
    contradictions: int = 0
    unverified: int = 0


@dataclass(frozen=True)
class ItemInput:
    """One consensus item: a scalar field, or one tag of a tag set."""

    kind: FieldKind
    value_a: str | None
    value_b: str | None
    scores: dict[int, int | None] = field(default_factory=dict)  # pass_no 3..6 → score
    evidence: dict[int, PassEvidence] = field(default_factory=dict)  # pass_no 3..6
    complete: bool = True


@dataclass(frozen=True)
class ItemDecision:
    status: ConsensusStatus
    confidence: Decimal | None
    conf_a: Decimal | None
    conf_b: Decimal | None
    agreement: bool
    final_value: str | None
    supports: int
    contradictions: int
    unverified: int


def meets(confidence: Decimal, threshold: Decimal) -> bool:
    """``ROUND(conf, 4) >= ROUND(threshold, 4)`` — the comparison used everywhere."""
    return q(confidence) >= q(threshold)


def margin_ok(winner: Decimal, loser: Decimal, margin: Decimal) -> bool:
    """``ROUND(ROUND(w, 4) - ROUND(l, 4), 4) >= ROUND(margin, 4)``."""
    return q(q(winner) - q(loser)) >= q(margin)


def candidate_confidence(
    kind: FieldKind, cross: int | None, self_: int | None, s: ConsensusSettings
) -> Decimal | None:
    if cross is None:
        return None
    if kind is FieldKind.TEXT:
        return q(Decimal(cross) / 10)
    if self_ is None:
        return None
    return q((s.cross_weight * cross + s.self_weight * self_) / 10)


def decide(item: ItemInput, s: ConsensusSettings | None = None) -> ItemDecision:
    s = s or ConsensusSettings()
    va, vb = item.value_a, item.value_b
    conf_a = (
        candidate_confidence(item.kind, item.scores.get(4), item.scores.get(5), s)
        if va is not None
        else None
    )
    conf_b = (
        candidate_confidence(item.kind, item.scores.get(3), item.scores.get(6), s)
        if vb is not None
        else None
    )
    agreement = va is not None and va == vb
    if not item.complete:
        return ItemDecision(ConsensusStatus.PENDING, None, conf_a, conf_b, agreement, None, 0, 0, 0)
    if va is None and vb is None:
        return ItemDecision(ConsensusStatus.NULL, q(0), None, None, False, None, 0, 0, 0)

    winner: str | None
    if agreement:
        winner, confidence = "both", q((conf_a + conf_b) / 2)  # type: ignore[operator]
        threshold_ok = meets(confidence, s.agreement_min)
    elif vb is None:
        winner, confidence = "a", conf_a
        threshold_ok = meets(confidence, s.one_sided_min)  # type: ignore[arg-type]
    elif va is None:
        winner, confidence = "b", conf_b
        threshold_ok = meets(confidence, s.one_sided_min)  # type: ignore[arg-type]
    else:
        assert conf_a is not None and conf_b is not None
        confidence = max(conf_a, conf_b)
        if conf_a == conf_b:
            winner, threshold_ok = None, False
        else:
            winner = "a" if conf_a > conf_b else "b"
            high, low = (conf_a, conf_b) if winner == "a" else (conf_b, conf_a)
            threshold_ok = meets(high, s.winner_min) and margin_ok(high, low, s.dispute_margin)

    scope = {"a": (4, 5), "b": (3, 6), "both": (3, 4, 5, 6), None: ()}[winner]
    evidence = [item.evidence.get(n, PassEvidence()) for n in scope]
    supports = sum(e.supports for e in evidence)
    contradictions = sum(e.contradictions for e in evidence)
    unverified = sum(e.unverified for e in evidence)
    evidence_ok = supports >= s.min_supports and contradictions <= supports

    accepted = threshold_ok and evidence_ok
    final = {"a": va, "b": vb, "both": va}.get(winner) if accepted else None  # type: ignore[arg-type]
    return ItemDecision(
        ConsensusStatus.ACCEPTED if accepted else ConsensusStatus.DISPUTED,
        confidence,
        conf_a,
        conf_b,
        agreement,
        final,
        supports,
        contradictions,
        unverified,
    )
