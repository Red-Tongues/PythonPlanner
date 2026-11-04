"""The consensus decision table as a pure function — the reference oracle (D5 point 5).

``v_eva_item_consensus`` (SQL) is the implementation the kernel uses; this module states the same
rules in plain Python so they can be read, unit-tested at their boundaries and compared with the
SQL row for row on both dialects (``tests/test_consensus_parity.py``).

**Arithmetic is integer fixed point scaled by 10⁴** (M2.1 fix 7, review finding MC5), exactly as
in the SQL:

* settings are quantised once: ``e4(0.6) = 6000`` (half up);
* ``conf_x = (cw·cross + sw·self + 5) // 10`` for each non-null candidate (half up); text fields
  use the cross score only, ``conf = cross · 1000``;
* the agreement mean is ``(conf_a + conf_b + 1) // 2`` (half up);
* every comparison is between integers. Decimal confidences (``confidence``, ``conf_a``,
  ``conf_b``) are those integers / 10000, for display only.

Rules (PROJECT_PLAN §7, B2, B3, B10 rulings 1–6, D5 clarifications):

* Incomplete (an applicable pass is missing) → ``pending``, confidence NULL.
* Both NULL → ``null``, confidence 0.
* Agreement → mean(conf_a, conf_b); accepted at ≥ ``agreement_min``.
* One NULL → the other's confidence; accepted at ≥ ``one_sided_min``.
* Disagreement → the higher confidence wins; accepted at ≥ ``winner_min`` with a margin
  ≥ ``dispute_margin``; an exact tie has no winner and is disputed.
* Evidence counts only from passes that evaluate the *winning* value (P4+P5 for A, P3+P6 for B,
  all on agreement, none on a tie; text fields have no P5/P6). Acceptance needs
  ≥ ``min_supports`` verified SUPPORT quotes and contradictions ≤ supports.
* **Reported confidence (ruling 5, M2.1 fix 10):** an accepted agreement reports the mean; every
  other non-pending, non-null row — every disputed row, *including a disputed agreement* —
  reports ``max(conf_a, conf_b)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from orchestrator.contracts import ConsensusStatus, FieldKind

SCALE = 10_000
Q = Decimal("0.0001")


def e4(value: Decimal | float | int | str) -> int:
    """A setting as an integer scaled by 10⁴, rounded half up (``0.6`` → ``6000``)."""
    return int((Decimal(str(value)) * SCALE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_e4(value: int | None) -> Decimal | None:
    return None if value is None else (Decimal(value) / SCALE).quantize(Q)


def q(value: Decimal | float | int) -> Decimal:
    """Round to 4 dp for display (``confidence`` columns are e4 integers / 10000)."""
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
    confidence_e4: int | None
    conf_a_e4: int | None
    conf_b_e4: int | None
    agreement: bool
    final_value: str | None
    supports: int
    contradictions: int
    unverified: int

    @property
    def confidence(self) -> Decimal | None:
        return from_e4(self.confidence_e4)

    @property
    def conf_a(self) -> Decimal | None:
        return from_e4(self.conf_a_e4)

    @property
    def conf_b(self) -> Decimal | None:
        return from_e4(self.conf_b_e4)


def meets(conf_e4: int, threshold: Decimal | float | str) -> bool:
    """``conf_e4 >= e4(threshold)`` — the comparison used everywhere."""
    return conf_e4 >= e4(threshold)


def margin_ok(winner_e4: int, loser_e4: int, margin: Decimal | float | str) -> bool:
    """``winner_e4 - loser_e4 >= e4(margin)`` (exact integer difference)."""
    return winner_e4 - loser_e4 >= e4(margin)


def candidate_confidence(
    kind: FieldKind, cross: int | None, self_: int | None, s: ConsensusSettings
) -> int | None:
    """A candidate's confidence as an e4 integer (``None`` when a needed score is missing)."""
    if cross is None:
        return None
    if kind is FieldKind.TEXT:
        return cross * 1000
    if self_ is None:
        return None
    return (e4(s.cross_weight) * cross + e4(s.self_weight) * self_ + 5) // 10


def decide(item: ItemInput, s: ConsensusSettings | None = None) -> ItemDecision:
    s = s or ConsensusSettings()
    va, vb = item.value_a, item.value_b
    ca = (
        candidate_confidence(item.kind, item.scores.get(4), item.scores.get(5), s)
        if va is not None
        else None
    )
    cb = (
        candidate_confidence(item.kind, item.scores.get(3), item.scores.get(6), s)
        if vb is not None
        else None
    )
    agreement = va is not None and va == vb
    if not item.complete:
        return ItemDecision(ConsensusStatus.PENDING, None, ca, cb, agreement, None, 0, 0, 0)
    if va is None and vb is None:
        return ItemDecision(ConsensusStatus.NULL, 0, None, None, False, None, 0, 0, 0)

    winner: str | None
    if agreement:
        assert ca is not None and cb is not None
        winner, decision = "both", (ca + cb + 1) // 2
        threshold_ok = meets(decision, s.agreement_min)
    elif vb is None:
        assert ca is not None
        winner, decision = "a", ca
        threshold_ok = meets(decision, s.one_sided_min)
    elif va is None:
        assert cb is not None
        winner, decision = "b", cb
        threshold_ok = meets(decision, s.one_sided_min)
    else:
        assert ca is not None and cb is not None
        decision = max(ca, cb)
        if ca == cb:
            winner, threshold_ok = None, False
        else:
            winner = "a" if ca > cb else "b"
            low = min(ca, cb)
            threshold_ok = meets(decision, s.winner_min) and margin_ok(
                decision, low, s.dispute_margin
            )

    scope = {"a": (4, 5), "b": (3, 6), "both": (3, 4, 5, 6), None: ()}[winner]
    if item.kind is FieldKind.TEXT:  # B3: text fields have no self-check passes
        scope = tuple(n for n in scope if n in (3, 4))
    evidence = [item.evidence.get(n, PassEvidence()) for n in scope]
    supports = sum(e.supports for e in evidence)
    contradictions = sum(e.contradictions for e in evidence)
    unverified = sum(e.unverified for e in evidence)
    evidence_ok = supports >= s.min_supports and contradictions <= supports

    accepted = threshold_ok and evidence_ok
    # an accepted agreement reports the mean (ruling 1); every other row the higher candidate
    # confidence (ruling 5, M2.1 fix 10)
    highest = max(c for c in (ca, cb) if c is not None)
    reported = decision if accepted and agreement else highest
    final = {"a": va, "b": vb, "both": va}.get(winner) if accepted else None  # type: ignore[arg-type]
    return ItemDecision(
        ConsensusStatus.ACCEPTED if accepted else ConsensusStatus.DISPUTED,
        reported,
        ca,
        cb,
        agreement,
        final,
        supports,
        contradictions,
        unverified,
    )
