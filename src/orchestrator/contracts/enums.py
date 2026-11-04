"""Enumerations shared by contracts, the database schema and the CLI.

All enums are ``StrEnum`` so their values are what is stored in the database and shown to users.
"""

from __future__ import annotations

from enum import StrEnum


class PacketStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    EVALUATING = "evaluating"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PROCESSED = "processed"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    FAILED = "failed"


#: Statuses that lock a packet (Requirement F). Kept in sync with ``orchestrator.db.ddl``.
LOCKING_STATUSES: frozenset[PacketStatus] = frozenset(
    {PacketStatus.COMPLETED, PacketStatus.ARCHIVED}
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
