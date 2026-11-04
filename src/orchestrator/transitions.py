"""The packet and run transition tables (M3 C23) — the single guard every status change passes.

This module is the **authority**. ``docs/ARCHITECTURE.md`` §7 publishes the same tables, and
migration 0007 freezes the union of allowed (from, to) pairs into database triggers on both
dialects (``orchestrator.db.ddl.m3_statements``), so a status change that is not listed here is
rejected by the application *and* by the database. Tests assert that every pair not in the
tables is rejected (``tests/test_transitions.py``).

Starting point: the reviewed reference ``docs/reviews/D8_state_machines.md``. Every deviation
from it is marked ``deviation=`` below and listed in ARCHITECTURE §7.3 with its reason.

Actors: D8's *kernel* and *governor* are ``system`` actors; *user* is a ``user`` actor. ``llm``
actors never move a packet or a run. Guards that need database state (fingerprints, gate
revisions, fencing tokens, leases, pauses) are enforced by the callers named in ``guard``; this
module checks the pair and the actor kind, which no caller can skip.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from orchestrator.actors import Actor, ActorKind
from orchestrator.contracts import GenerationRunState, PacketStatus
from orchestrator.errors import InvalidTransitionError

S = PacketStatus
R = GenerationRunState
K = frozenset({ActorKind.SYSTEM})
U = frozenset({ActorKind.USER})
KU = K | U


@dataclass(frozen=True)
class Rule:
    source: str
    target: str
    actors: frozenset[ActorKind]
    trigger: str
    guard: str = ""
    deviation: str = ""


# --------------------------------------------------------------------------- packets

#: D8 notation
OVERRIDABLE: frozenset[PacketStatus] = frozenset(
    {
        S.DRAFT,
        S.EVALUATING_PRE,
        S.READY,
        S.QUEUED,
        S.NEEDS_REVIEW,
        S.FAILED,
        S.AWAITING_CONFIRMATION,
    }
)
#: terminal packet statuses: locked, no exits (``archived`` is legacy, see deviation P4)
TERMINAL: frozenset[PacketStatus] = frozenset({S.COMPLETED, S.CLOSED_UNVERIFIED, S.ARCHIVED})
NON_TERMINAL: frozenset[PacketStatus] = frozenset(PacketStatus) - TERMINAL
#: M1 statuses kept so old rows stay valid; they only have exits (deviation P3)
LEGACY: frozenset[PacketStatus] = frozenset({S.EVALUATING, S.IN_PROGRESS, S.PROCESSED})


def _p(
    sources: Iterable[PacketStatus] | PacketStatus,
    target: PacketStatus,
    actors: frozenset[ActorKind],
    trigger: str,
    guard: str = "",
    deviation: str = "",
) -> list[Rule]:
    if isinstance(sources, PacketStatus):
        sources = [sources]
    return [Rule(s.value, target.value, actors, trigger, guard, deviation) for s in sources]


_PACKET_RULES: list[Rule] = [
    *_p(S.DRAFT, S.EVALUATING_PRE, K, "tick step 2", "H = current fingerprint (B9, C25)"),
    *_p(
        S.EVALUATING_PRE,
        S.READY,
        K,
        "readiness",
        "required tiers accepted or soft-waived; no hard failure; dependencies; approvals; "
        "ambiguity ≤ 4,000; H current",
    ),
    *_p(S.EVALUATING_PRE, S.NEEDS_REVIEW, K, "evaluation", "tier-1 dispute, B12, ambiguity"),
    *_p(S.EVALUATING_PRE, S.AWAITING_CONFIRMATION, K, "promotion/branch", "scope = confirm"),
    *_p(
        [S.EVALUATING_PRE, S.READY],
        S.WAITING_FOR_CHILDREN,
        K,
        "split",
        "E10: depth < 2; descendant and fan-out caps; atomic capacity reservation (C36); B5 "
        "idempotency on (packet id + version, rule id, rule version); branching scope auto",
    ),
    *_p(
        S.READY,
        S.QUEUED,
        K,
        "admit_next",
        "E9: H recomputed, then the full readiness predicate (C12); guarded UPDATE on status, "
        "version, H, G; no BLOCK/HOLD; within failure budget",
    ),
    *_p(
        [S.READY, S.QUEUED, S.FAILED],
        S.EVALUATING_PRE,
        K,
        "fingerprint or readiness changed",
        "H ≠ current or not ready; the kernel commits (M2.1 ruling 4)",
    ),
    *_p(S.QUEUED, S.AWAITING_CONFIRMATION, K, "failure budget / 48 h touch", "one confirmation"),
    *_p(
        S.READY,
        S.AWAITING_CONFIRMATION,
        K,
        "failure budget",
        "one confirmation",
        deviation="P1",
    ),
    *_p(S.QUEUED, S.BLOCKED, K, "WP routing with no consumer (C5)"),
    *_p(
        S.QUEUED,
        S.RUNNING,
        K,
        "claim",
        "E9: H recomputed, then the full readiness predicate; guarded UPDATE (status, version, "
        "H, G, lease); not paused (C26); budget reserved (C36); snapshot written and F "
        "incremented in the same transaction",
    ),
    *_p(
        S.RUNNING,
        S.EVALUATING_POST,
        K,
        "write + commit",
        "publication guard: run accepted; run F = packet F; G, H match; expected HEAD; C32 "
        "ownership and base hash; write authorised; idempotent write + commit; M and commit "
        "SHA recorded first",
    ),
    *_p(S.RUNNING, S.AWAITING_CONFIRMATION, K, "exit 3 / repo_write = confirm"),
    *_p(S.RUNNING, S.BLOCKED, K, "execution check under NoRunner (C31)"),
    *_p(S.RUNNING, S.FAILED, K, "run failed", "F matches"),
    *_p(
        S.RUNNING,
        S.EVALUATING_PRE,
        K,
        "stale run",
        "E7: only if the packet's active run ID and F match this run, it is still running, and "
        "no hold, block, pause or confirmation applies (V2)",
    ),
    *_p(S.FAILED, S.QUEUED, K, "retry", "within budget and backoff, or config changed; H current"),
    *_p(S.FAILED, S.AWAITING_CONFIRMATION, K, "failure budget exhausted", "one confirmation"),
    *_p(
        S.EVALUATING_POST,
        S.COMPLETED,
        KU,
        "completion",
        "E5: every required post field complete, accepted and current for M from C29-verified "
        "evaluators; scan bound to the commit SHA; quality policy (C30); all hard gates; no "
        "hold or block; locks",
        deviation="P2",
    ),
    *_p(
        S.EVALUATING_POST,
        S.NEEDS_REVIEW,
        K,
        "post-eval failed / returned required-scan gate ERROR",
        "E6: a gate ERROR bound to the commit SHA also raises a confirmation",
    ),
    *_p(
        S.EVALUATING_POST,
        S.BLOCKED,
        K,
        "required scanner unavailable, or execution/report retrieval failed",
        "E6",
    ),
    *_p(S.EVALUATING_POST, S.AWAITING_CONFIRMATION, K, "optional-scan ERROR"),
    *_p(S.EVALUATING_POST, S.HELD, K, "alignment < 7,500 (governor)"),
    *_p(
        S.WAITING_FOR_CHILDREN,
        S.COMPLETED,
        KU,
        "roll-up",
        "≥ 1 child and all children completed (closed_unverified does not count); locks",
        deviation="P2",
    ),
    *_p(
        S.AWAITING_CONFIRMATION,
        S.RUNNING,
        U,
        "exit-3 approval",
        "R1; then the shared dispatch guard (E3); resumes the same conversation exactly once",
    ),
    *_p(S.AWAITING_CONFIRMATION, S.QUEUED, U, "failure-budget or 48 h approval", "H current"),
    *_p(
        S.AWAITING_CONFIRMATION,
        S.EVALUATING_POST,
        U,
        "repo_write / optional-scan approval",
        "E4: repo_write needs the full publication guard of running → evaluating_post "
        "(R1 for the targeted run); a mismatch stales the run",
    ),
    *_p(
        S.AWAITING_CONFIRMATION,
        S.COMPLETED,
        U,
        "optional-scan approval",
        "E5 completion guard; the approval covers only that gate ERROR, never a hard "
        "constraint; locks",
    ),
    *_p(
        S.AWAITING_CONFIRMATION,
        S.WAITING_FOR_CHILDREN,
        U,
        "split approval",
        "E10 split guard; the confirmation is validated and C25 applied",
    ),
    *_p(
        S.AWAITING_CONFIRMATION,
        S.HELD,
        U,
        "rejection (any type)",
        "reason required; E2: an exit-3 rejection keeps the run parked",
    ),
    *_p(S.AWAITING_CONFIRMATION, S.EVALUATING_PRE, K, "approval but H stale (C25)"),
    *_p(
        S.AWAITING_CONFIRMATION,
        S.EVALUATING_PRE,
        U,
        "candidate-promotion approval (E1)",
        "targets the current version and context; promotion and C25 gate bump atomic; "
        "readiness recomputed; a stale approval does not apply",
    ),
    # Q → blocked is also a kernel move (no consumer, above): merged into KU here
    *_p(
        (OVERRIDABLE | {S.HELD}) - {S.QUEUED},
        S.BLOCKED,
        U,
        "BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible)",
        "G bumped in scope (C35)",
    ),
    *_p(
        S.QUEUED,
        S.BLOCKED,
        U,
        "BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible)",
        "G bumped in scope (C35)",
    ),
    *_p(OVERRIDABLE, S.HELD, KU, "HOLD (user) or alignment hold (governor)", "no active BLOCK"),
    *_p(S.BLOCKED, S.HELD, U, "last BLOCK revoked, HOLD remains"),
    *_p(S.BLOCKED, S.EVALUATING_PRE, KU, "last BLOCK revoked / blocker cleared; no HOLD"),
    *_p(S.HELD, S.EVALUATING_PRE, U, "last HOLD revoked / owner clears hold", "never automatic"),
    *_p(
        S.NEEDS_REVIEW,
        S.EVALUATING_PRE,
        U,
        "soft waiver (pre) or definition/exemplar edit",
        "G bumped",
    ),
    *_p(S.NEEDS_REVIEW, S.EVALUATING_POST, U, "soft waiver (post)", "G bumped"),
    *_p(
        NON_TERMINAL,
        S.CLOSED_UNVERIFIED,
        U,
        "explicit owner close (branch kill is a BLOCK, not a close: D9-R1)",
        "reason required; locks; in-flight runs staled",
    ),
    *_p(LEGACY, S.EVALUATING_PRE, K, "legacy M1 row re-enters evaluation", deviation="P3"),
]


def _index(rules: list[Rule]) -> dict[tuple[str, str], Rule]:
    table: dict[tuple[str, str], Rule] = {}
    for rule in rules:
        key = (rule.source, rule.target)
        if key in table:  # the same pair listed twice: merge actor kinds
            old = table[key]
            rule = Rule(
                rule.source,
                rule.target,
                old.actors | rule.actors,
                f"{old.trigger}; {rule.trigger}",
                old.guard or rule.guard,
                old.deviation or rule.deviation,
            )
        table[key] = rule
    return table


PACKET_TRANSITIONS: dict[tuple[str, str], Rule] = _index(_PACKET_RULES)


# --------------------------------------------------------------------------- runs

#: run states after which a ``generation_runs`` row never changes (DB-enforced; ``accepted``
#: also becomes final once its commit is recorded — D8 R4)
RUN_TERMINAL: frozenset[GenerationRunState] = frozenset(
    {R.FAILED, R.STALE, R.SUCCEEDED, R.ABANDONED}
)
#: run states holding a live claim on their packet
RUN_ACTIVE: frozenset[GenerationRunState] = frozenset(
    {R.PLANNED, R.DISPATCHED, R.AWAITING_RESULT, R.VALIDATING, R.ACCEPTED, R.AWAITING_CONFIRMATION}
)


def _r(
    sources: Iterable[GenerationRunState] | GenerationRunState,
    target: GenerationRunState,
    actors: frozenset[ActorKind],
    trigger: str,
    guard: str = "",
    deviation: str = "",
) -> list[Rule]:
    if isinstance(sources, GenerationRunState):
        sources = [sources]
    return [Rule(s.value, target.value, actors, trigger, guard, deviation) for s in sources]


_RUN_RULES: list[Rule] = [
    *_r(
        R.PLANNED,
        R.DISPATCHED,
        K,
        "adapter dispatch",
        "dispatch guard (E3): version, F, G, H and lease revalidated; authorised atomically "
        "against pauses (C26); capacity reserved (C36); operation ID and first "
        "channel_runs row before any effect",
    ),
    *_r(
        [R.PLANNED, R.DISPATCHED, R.AWAITING_RESULT, R.VALIDATING],
        R.STALE,
        K,
        "F superseded, G or H mismatch, packet closed/held/blocked",
        "releases unused reservations",
    ),
    *_r(R.DISPATCHED, R.AWAITING_RESULT, K, "payload written / process launched", "deadline"),
    *_r(
        [R.DISPATCHED, R.AWAITING_RESULT],
        R.FAILED,
        K,
        "exit 2; or exit 1/4 / deadline expiry (E11) on the last attempt",
        "exit 2: channel paused, no charge; E11: failed_runs + 1 exactly once per operation ID",
    ),
    *_r(
        R.DISPATCHED,
        R.DISPATCHED,
        K,
        "deadline expiry (E11) with attempts left",
        "timeout recorded once per operation ID; the retry goes only through the E3 guard",
    ),
    *_r(R.AWAITING_RESULT, R.VALIDATING, K, "exit 0 before the deadline", "F and lease valid"),
    *_r(
        R.AWAITING_RESULT,
        R.AWAITING_RESULT,
        K,
        "exit 1/4 or deadline expiry (E11) with attempts left",
        "E3 dispatch guard before the retry effect; new channel_runs row",
    ),
    *_r(R.AWAITING_RESULT, R.AWAITING_CONFIRMATION, K, "exit 3", "resume identity; no counters"),
    *_r(
        R.AWAITING_CONFIRMATION,
        R.DISPATCHED,
        U,
        "approval",
        "R1 (run G = packet G, both + 1), then the E3 dispatch guard; resumes once",
    ),
    *_r(R.AWAITING_CONFIRMATION, R.FAILED, U, "cancellation after rejection", "no budget charge"),
    *_r(
        R.AWAITING_CONFIRMATION,
        R.STALE,
        K,
        "non-approval governance change or H change",
        "E2: the rejection of this run's own exit-3 confirmation is excluded (the run stays "
        "parked); later unrelated changes still stale it",
    ),
    *_r(R.VALIDATING, R.ACCEPTED, K, "checks pass", "F, G, H match; compute M"),
    *_r(
        R.VALIDATING,
        R.AWAITING_RESULT,
        K,
        "validation failed, attempts left",
        "error fed back; E3 dispatch guard before the retry effect",
    ),
    *_r(R.VALIDATING, R.FAILED, K, "last attempt failed, or NoRunner gate"),
    *_r(
        R.ACCEPTED,
        R.STALE,
        K,
        "F/G/H change (E8), superseded version (E8), HEAD moved, modifies base mismatch",
        "only before the commit is recorded (R4); publication prohibited; only unused "
        "reservations released",
    ),
    *_r(R.CLAIMED, R.STALE, K, "legacy M2 claim (no fencing token)", deviation="R1"),
]

RUN_TRANSITIONS: dict[tuple[str, str], Rule] = _index(_RUN_RULES)


# --------------------------------------------------------------------------- checks


def packet_rule(source: PacketStatus | str, target: PacketStatus | str) -> Rule | None:
    return PACKET_TRANSITIONS.get((PacketStatus(source).value, PacketStatus(target).value))


def run_rule(source: GenerationRunState | str, target: GenerationRunState | str) -> Rule | None:
    return RUN_TRANSITIONS.get((GenerationRunState(source).value, GenerationRunState(target).value))


def can_move_packet(source: PacketStatus | str, target: PacketStatus | str, actor: Actor) -> bool:
    rule = packet_rule(source, target)
    return rule is not None and actor.kind in rule.actors


def check_packet(source: PacketStatus | str, target: PacketStatus | str, actor: Actor) -> Rule:
    """The rule for this move, or ``InvalidTransitionError``."""
    if not isinstance(actor, Actor):
        raise TypeError("a transition needs a typed Actor (see orchestrator.actors)")
    src, dst = PacketStatus(source), PacketStatus(target)
    rule = packet_rule(src, dst)
    if rule is None or actor.kind not in rule.actors:
        allowed = sorted(
            t
            for (s, t), r in PACKET_TRANSITIONS.items()
            if s == src.value and actor.kind in r.actors
        )
        why = "not in the packet table" if rule is None else f"not allowed for a {actor.kind} actor"
        raise InvalidTransitionError(
            f"{src.value} → {dst.value} is {why} ({actor}; allowed: {allowed or 'none'})"
        )
    return rule


def check_run(
    source: GenerationRunState | str, target: GenerationRunState | str, actor: Actor
) -> Rule:
    if not isinstance(actor, Actor):
        raise TypeError("a transition needs a typed Actor (see orchestrator.actors)")
    src, dst = GenerationRunState(source), GenerationRunState(target)
    rule = run_rule(src, dst)
    if rule is None or actor.kind not in rule.actors:
        why = "not in the run table" if rule is None else f"not allowed for a {actor.kind} actor"
        raise InvalidTransitionError(f"run {src.value} → {dst.value} is {why} ({actor})")
    return rule


def packet_pairs() -> list[tuple[str, str]]:
    """Every allowed packet (from, to) pair, sorted: what the 0007 trigger freezes."""
    return sorted(PACKET_TRANSITIONS)


def run_pairs() -> list[tuple[str, str]]:
    """Every allowed run state change (from ≠ to), sorted: what the 0007 trigger freezes."""
    return sorted(k for k in RUN_TRANSITIONS if k[0] != k[1])
