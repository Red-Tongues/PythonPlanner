"""Enumerations shared by contracts, the database schema and the CLI.

All enums are ``StrEnum`` so their values are what is stored in the database and shown to users.
"""

from __future__ import annotations

from enum import StrEnum


class PacketStatus(StrEnum):
    """genPacket lifecycle (B9): draft → evaluating_pre → ready → queued → running →
    evaluating_post → completed (locked), with side states needs_review, held, blocked, failed,
    awaiting_confirmation and waiting_for_children (B4/B5).

    ``evaluating``, ``in_progress`` and ``processed`` are M1 values kept so legacy rows stay
    valid; they only have exits into the B9 lifecycle.
    """

    DRAFT = "draft"
    EVALUATING_PRE = "evaluating_pre"
    READY = "ready"
    QUEUED = "queued"
    RUNNING = "running"
    EVALUATING_POST = "evaluating_post"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    HELD = "held"
    BLOCKED = "blocked"
    FAILED = "failed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    WAITING_FOR_CHILDREN = "waiting_for_children"
    ARCHIVED = "archived"
    #: M2.1 fix 5: closed by a user without a passing evaluation — terminal, locked, and never
    #: counted as a success (``completed`` always means the completion gate passed).
    CLOSED_UNVERIFIED = "closed_unverified"
    #: M3a (R6 OQ13): a genPacket whose generation was graded by ``gen_eva`` and frozen into
    #: ``sealed_bundles``. Terminal and locked; further work is a child genPacket. Whether it
    #: satisfies a dependency depends on its grade (``dep_satisfaction_grade_e4``).
    SEALED = "sealed"
    # legacy (M1)
    EVALUATING = "evaluating"
    IN_PROGRESS = "in_progress"
    PROCESSED = "processed"


#: Statuses that lock a packet (Requirement F). Kept in sync with ``orchestrator.db.ddl``.
LOCKING_STATUSES: frozenset[PacketStatus] = frozenset(
    {
        PacketStatus.COMPLETED,
        PacketStatus.ARCHIVED,
        PacketStatus.CLOSED_UNVERIFIED,
        PacketStatus.SEALED,
    }
)


class PacketKind(StrEnum):
    TASK = "task"
    CONTENT_EVAL = "content_eval"


class Channel(StrEnum):
    QUICKFIRE_EVALUATION = "quickfire_evaluation"
    LOCAL_GENERATION = "local_generation"
    WORKBENCH_PRODUCTION = "workbench_production"


class AutonomyMode(StrEnum):
    MANUAL = "manual"
    CONFIRM = "confirm"
    AUTO = "auto"


class Stage(StrEnum):
    """The nine key stages that carry an autonomy scope (PROJECT_PLAN §11)."""

    INGESTION = "ingestion"
    OBJECTIVE_LLM_VALUES = "objective_llm_values"
    CANDIDATE_PROMOTION = "candidate_promotion"
    BRANCHING = "branching"
    ROUTING = "routing"
    LOCAL_GENERATION = "local_generation"
    WORKBENCH_PRODUCTION = "workbench_production"
    REPO_WRITE = "repo_write"
    QUALITY_SCAN = "quality_scan"


class FieldKind(StrEnum):
    TAGSET = "tagset"
    SCORE = "score"
    TEXT = "text"
    BOOL = "bool"


class FieldLevel(StrEnum):
    TASK = "task"
    CONTENT = "content"


class ThresholdOperator(StrEnum):
    GTE = ">="
    GT = ">"
    LTE = "<="
    LT = "<"
    EQ = "="
    NEQ = "!="
    HAS_TAG = "has_tag"
    STATUS_IS = "status_is"


class ThresholdAction(StrEnum):
    REQUIRE_APPROVAL = "require_approval"
    SPLIT = "split"
    ROUTE_WORKBENCH_PRODUCTION = "route_workbench_production"
    ROUTE_LOCAL_GENERATION = "route_local_generation"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    INVALID = "invalid"
    FAILED = "failed"


class ConfirmationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETIRED = "retired"


class EvaPhase(StrEnum):
    PRE = "pre"  # evaluates the genPacket before generation
    POST = "post"  # evaluates the generated output before the genPacket locks


class EvaState(StrEnum):
    """evaPacket lifecycle (B9)."""

    PLANNED = "planned"
    EXTRACTING = "extracting"  # P1–P2
    CROSS_SCORING = "cross_scoring"  # P3–P6
    CONSENSUS = "consensus"
    DISPUTED = "disputed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


#: evaPacket states after which the row never changes (DB-enforced).
EVA_TERMINAL_STATES: frozenset[EvaState] = frozenset(
    {EvaState.CONSENSUS, EvaState.DISPUTED, EvaState.FAILED, EvaState.SUPERSEDED}
)


class CycleState(StrEnum):
    OPEN = "open"
    COMPLETE = "complete"
    SUPERSEDED = "superseded"
    LEGACY = "legacy"  # backfilled by migration 0005 for pre-M2 pass rows


class PassPlanName(StrEnum):
    COMBINED = "combined"  # 6 calls: 2 extractions + 4 combined evaluations (D3/B12 canonical)
    SPLIT = "split"  # 10 calls: 2 extractions + 4 accuracy + 4 evidence prompts


class ConsensusStatus(StrEnum):
    ACCEPTED = "accepted"
    DISPUTED = "disputed"
    NULL = "null"
    PENDING = "pending"


class OverrideKind(StrEnum):
    """Human queue overrides, in precedence order (B9): BLOCK > HOLD > FORCE > PIN > REORDER."""

    BLOCK = "block"
    HOLD = "hold"
    FORCE = "force"
    PIN = "pin"
    REORDER = "reorder"


OVERRIDE_PRECEDENCE: dict[OverrideKind, int] = {k: i + 1 for i, k in enumerate(OverrideKind)}


class OverrideScope(StrEnum):
    VERSION = "version"  # lapses when the packet is revised
    LINEAGE = "lineage"  # BLOCK/HOLD only: survives revisions until revoked


class DependencyKind(StrEnum):
    FINISH_TO_START = "finish_to_start"  # depends_on must be completed first


class GenerationRunState(StrEnum):
    """The durable run state machine (C1, C24, C27; transitions in ``orchestrator.transitions``).

    ``claimed``, ``succeeded`` and ``abandoned`` are M2 values kept so old rows stay valid;
    ``claimed`` may only go ``stale`` (it has no fencing token)."""

    PLANNED = "planned"
    DISPATCHED = "dispatched"
    AWAITING_RESULT = "awaiting_result"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    FAILED = "failed"
    STALE = "stale"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    #: M3a (R6 OQ18): the generation ended with no gradable output (timeout, disconnect,
    #: adapter fault). Terminal; never a quality signal.
    CRASHED = "crashed"
    # legacy (M2)
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    ABANDONED = "abandoned"


class ScopeType(StrEnum):
    """Governance scopes (C25, C26, C33): what a pause, a budget or a gate bump applies to."""

    GLOBAL = "global"
    CHANNEL = "channel"
    PROJECT = "project"
    OBJECTIVE = "objective"
    BRANCH = "branch"  # a trunk lineage (root ref) and all its descendants
    PACKET = "packet"


class ScopeStatus(StrEnum):
    """C33: a scope is ``normal`` or ``paused``; a pause clears only on explicit owner resume."""

    NORMAL = "normal"
    PAUSED = "paused"


class AttemptOutcome(StrEnum):
    """How one channel attempt ended (C27 exit codes; recorded once per attempt)."""

    SUCCEEDED = "succeeded"  # exit 0, output passed to validation
    INVALID = "invalid"  # validation failed
    FAILED = "failed"  # exit 1
    TIMEOUT = "timeout"  # exit 4 / deadline
    PARKED = "parked"  # exit 3
    CONFIG_ERROR = "config_error"  # exit 2
    STALE = "stale"  # result arrived for an obsolete fencing token / lease
    UNKNOWN = "unknown"  # usage or outcome could not be established (keeps its reservation)
