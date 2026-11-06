"""SQLAlchemy 2.0 schema for every table in PROJECT_PLAN §6.

Primary keys are UUID strings. Human-readable refs (``P-0001``, ``O-0001-01``, ``PP-000001``)
name a *lineage*: every version of a record keeps its ref, ``(ref, version)`` is unique and
``supersedes_id`` links a version to its predecessor (A2). The current head of each lineage is
derived by the ``v_*_heads`` views — children are never re-pointed.

Immutability is enforced by triggers installed from ``orchestrator.db.ddl``; the ORM guard in
``orchestrator.db.guards`` is a first line of defence only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from orchestrator.contracts import (
    DEFAULT_RESPONSE_STRUCTURE,
    AttemptOutcome,
    AutonomyMode,
    Channel,
    ConfirmationStatus,
    CycleState,
    DependencyKind,
    EvaPhase,
    EvaState,
    FieldKind,
    FieldLevel,
    GenerationRunState,
    OverrideKind,
    OverrideScope,
    PacketKind,
    PacketStatus,
    PassPlanName,
    RunStatus,
    ScopeStatus,
    ScopeType,
    Stage,
    ThresholdAction,
    ThresholdOperator,
)
from orchestrator.db.types import UTCDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def enum_type(enum_cls: type[StrEnum]) -> SAEnum:
    """Store enum *values* as VARCHAR with a CHECK constraint (portable; triggers compare text)."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
        name=enum_cls.__name__.lower(),
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _id() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


def _created() -> Mapped[datetime]:
    return mapped_column(UTCDateTime(), default=utcnow, nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)


def _version() -> Mapped[int]:
    return mapped_column(Integer, default=1, nullable=False)


def _locked() -> Mapped[bool]:
    return mapped_column(Boolean, default=False, nullable=False)


def _content_revision() -> Mapped[int]:
    return mapped_column(Integer, default=0, server_default=text("0"), nullable=False)


def _last_producer() -> Mapped[str]:
    return mapped_column(String(64), default="kernel", server_default="kernel", nullable=False)


# --------------------------------------------------------------------------- core tree


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("ref", "version"),)

    id: Mapped[str] = _id()
    ref: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brief: Mapped[str] = mapped_column(Text, default="", nullable=False)
    industry: Mapped[str | None] = mapped_column(String(200))
    sectors: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # "values" / "references" are SQL reserved words, hence the column names.
    core_values: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    goals: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text)
    reference_list: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    tree_contract: Mapped[str | None] = mapped_column(String(64))  # e.g. "ProjectTree@1.0"
    #: M3a (OQ21, OQ26): edited in place; every change of an audited column bumps this and is
    #: captured by the outbox trigger into ``field_revisions``
    content_revision: Mapped[int] = _content_revision()
    last_producer: Mapped[str] = _last_producer()
    version: Mapped[int] = _version()
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), unique=True)
    locked: Mapped[bool] = _locked()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Objective(Base):
    __tablename__ = "objectives"
    __table_args__ = (UniqueConstraint("ref", "version"),)

    id: Mapped[str] = _id()
    ref: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brief: Mapped[str] = mapped_column(Text, default="", nullable=False)
    priority_level: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # Objective-level tags/scores. LLM-generated values always need manual confirmation.
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    content_revision: Mapped[int] = _content_revision()
    last_producer: Mapped[str] = _last_producer()
    version: Mapped[int] = _version()
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("objectives.id"), unique=True)
    locked: Mapped[bool] = _locked()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    project: Mapped[Project] = relationship()


class Packet(Base):
    """A Task = Prompt Packet: core fields + master governance fields."""

    __tablename__ = "packets"
    __table_args__ = (
        UniqueConstraint("ref", "version"),
        # R6 OQ24: a re-imagined child keeps its parent's position and orders by generation.
        # Deviation: an index, not UNIQUE — superseded legacy versions and split children share
        # positions; (objective, position, lineage_depth, ref) is the display order.
        Index("ix_packets_objective_position_lineage", "objective_id", "position", "lineage_depth"),
    )

    id: Mapped[str] = _id()
    ref: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    objective_id: Mapped[str | None] = mapped_column(ForeignKey("objectives.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    packet_kind: Mapped[PacketKind] = mapped_column(
        enum_type(PacketKind), default=PacketKind.TASK, nullable=False
    )
    # core fields
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_description: Mapped[str] = mapped_column(Text, nullable=False)
    # master governance fields
    response_structure: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_RESPONSE_STRUCTURE, nullable=False
    )
    response_contract: Mapped[str] = mapped_column(  # "name@version" at the time of creation
        String(80), default=f"{DEFAULT_RESPONSE_STRUCTURE}@1.0", nullable=False
    )
    channel: Mapped[Channel | None] = mapped_column(enum_type(Channel))
    priority_level: Mapped[float | None] = mapped_column(Float)
    manual_boost: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[PacketStatus] = mapped_column(
        enum_type(PacketStatus), default=PacketStatus.DRAFT, nullable=False, index=True
    )
    evaluations_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    repo_target_path: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    locked: Mapped[bool] = _locked()
    # versioning and branching
    version: Mapped[int] = _version()
    # unique → linear history: a packet can be superseded at most once.
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("packets.id"), unique=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("packets.id"), index=True)
    root_id: Mapped[str | None] = mapped_column(ForeignKey("packets.id"), index=True)
    branch_reason: Mapped[str | None] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # kernel claims (M3): atomic claim with an expiring lease and heartbeats
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    # B9 projections of the current consensus (written from the SQL views, stale-write guarded)
    tags: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    scores: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=False
    )
    #: SHA-256 of what evaPackets read (packet core + head project/objective core). Results are
    #: current only while their target hash equals this value.
    input_hash: Mapped[str | None] = mapped_column(String(64))
    #: M2.1 fix 2: bumped by every governance write that can change readiness (waivers,
    #: overrides, revocations, dependencies, approvals); admission and claim compare-and-set it.
    gate_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: M3 C26: incremented by every claim; result acceptance and publication reject a run whose
    #: token is not the packet's current one
    fencing_token: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: M3 E7: the generation run that currently owns this packet version (NULL when none)
    active_run_id: Mapped[str | None] = mapped_column(String(36))
    #: A stale run was detached while a pause/hold/block prevents recovery; reconcile resumes
    #: the packet once restrictions clear without losing the run that deferred it.
    recovery_pending: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # ---- M3a: the genPacket loop (R6)
    #: OQ33: bumped only when an audited content column changes (kernel writes bump it with
    #: ``last_producer``; a raw SQL edit is bumped by the trigger and attributed to ``user_sql``)
    content_revision: Mapped[int] = _content_revision()
    last_producer: Mapped[str] = _last_producer()
    #: OQ18: generations that ended with no gradable output (never a quality signal)
    crash_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: OQ18: a crashed packet is not dispatched again before this time
    cooldown_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    #: OQ14 / OQ18: why the packet is ``held`` (``grade_met``, ``crash_limit``, ``user``)
    hold_reason: Mapped[str | None] = mapped_column(String(32))
    #: OQ2 / OQ6: when the packet last entered ``ready`` (tie-break and ageing)
    ready_since: Mapped[datetime | None] = mapped_column(UTCDateTime())
    #: OQ24: re-imagination generation (0 for an original genPacket); ``depth`` stays the split
    #: depth (B5)
    lineage_depth: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    #: Q6 / OQ24: the sealed bundle of the parent this child re-imagines
    parent_bundle_id: Mapped[str | None] = mapped_column(
        ForeignKey("sealed_bundles.id", use_alter=True)
    )
    parent_bundle_hash: Mapped[str | None] = mapped_column(String(64))
    #: R6 OQ10: ``locally_drafted_examples`` = an example blob is present (``content_blobs``)
    examples_hash: Mapped[str | None] = mapped_column(
        ForeignKey("content_blobs.hash", use_alter=True)
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    project: Mapped[Project] = relationship()
    objective: Mapped[Objective | None] = relationship(foreign_keys=[objective_id])


# --------------------------------------------------------------------------- evaluation layer


class CandidateFieldDefinitionRow(Base):
    __tablename__ = "candidate_field_definitions"

    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    level: Mapped[FieldLevel] = mapped_column(enum_type(FieldLevel), nullable=False)
    kind: Mapped[FieldKind] = mapped_column(enum_type(FieldKind), nullable=False)
    vocabulary: Mapped[list[str] | None] = mapped_column(JSON)
    scale: Mapped[str | None] = mapped_column(String(16))
    max_chars: Mapped[int | None] = mapped_column(Integer)
    max_passes: Mapped[int | None] = mapped_column(Integer)
    prompt_hint: Mapped[str] = mapped_column(Text, nullable=False)
    is_key_field: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = _version()  # bumped on every registry change
    contract: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class EvaluationPass(Base):
    """Every pass of every evaluation. **Append-only** (DB-enforced)."""

    __tablename__ = "evaluation_passes"
    # R1 fix 5: one row per (packet, cycle, field, pass, attempt); the cycle id is mandatory so
    # NULLs can never slip past the uniqueness rule.
    __table_args__ = (
        UniqueConstraint("packet_id", "eval_cycle_id", "field_name", "pass_no", "attempt"),
        # M2: one valid row per pass of an evaPacket (legacy rows have no evaPacket)
        Index("uq_evaluation_passes_eva_pass", "eva_packet_id", "pass_no", unique=True),
    )

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    eval_cycle_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_cycles.id"), nullable=False, index=True
    )
    eva_packet_id: Mapped[str | None] = mapped_column(ForeignKey("eva_packets.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    pass_no: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # extract / cross / self
    target_pass_no: Mapped[int | None] = mapped_column(Integer)
    value: Mapped[Any | None] = mapped_column(JSON)
    score: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class Threshold(Base):
    """Dynamic rule: field / operator / value → action. Rules with the same action OR together."""

    __tablename__ = "thresholds"

    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[ThresholdOperator] = mapped_column(
        enum_type(ThresholdOperator), nullable=False
    )
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[ThresholdAction] = mapped_column(enum_type(ThresholdAction), nullable=False)
    precedence: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: bumped on every change; part of the branching key (B5)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Setting(Base):
    """Weights and tunables (aggregation, priority, attempts). Values are JSON."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = _version()  # bumped on every change
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


# --------------------------------------------------------------------------- execution layer


class ChannelRun(Base):
    """One Run-ID contract: an attempt to obtain a valid output from a channel.

    **Append-only** since M3 (C24, DB-enforced): one row per attempt, written before the
    attempt's external effect for generation runs; the outcome is a ``ChannelRunOutcome``."""

    __tablename__ = "channel_runs"
    __table_args__ = (
        # a unique index (not a constraint) so 0007 can add it to the existing table on SQLite
        Index("uq_channel_runs_operation_id", "operation_id", unique=True),
    )

    id: Mapped[str] = _id()
    run_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    channel: Mapped[Channel] = mapped_column(enum_type(Channel), nullable=False)
    packet_id: Mapped[str | None] = mapped_column(ForeignKey("packets.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    contract: Mapped[str] = mapped_column(String(80), nullable=False)  # "name@version"
    # durable run state machine (M3, C1); values are defined by the kernel
    state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    input_hash: Mapped[str | None] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    policy_revision: Mapped[str | None] = mapped_column(String(64))
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[RunStatus] = mapped_column(
        enum_type(RunStatus), default=RunStatus.PENDING, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    # M3 (C24, C26): the generation run (LG/WP) or evaPacket (QE) this attempt belongs to, its
    # durable operation ID (recovery recognises an effect that already happened) and the
    # fencing token it was dispatched under
    generation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_runs.id"), index=True
    )
    eva_packet_id: Mapped[str | None] = mapped_column(ForeignKey("eva_packets.id"), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64))
    fencing_token: Mapped[int | None] = mapped_column(Integer)


class ChannelRunOutcome(Base):
    """How one attempt ended (C27), recorded exactly once per attempt. **Append-only**."""

    __tablename__ = "channel_run_outcomes"

    id: Mapped[str] = _id()
    channel_run_id: Mapped[str] = mapped_column(
        ForeignKey("channel_runs.id"), unique=True, nullable=False
    )
    generation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_runs.id"), index=True
    )
    outcome: Mapped[AttemptOutcome] = mapped_column(enum_type(AttemptOutcome), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    tokens_used: Mapped[int | None] = mapped_column(Integer)  # NULL = unknown (C36)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class Artifact(Base):
    """A generated output. **Immutable once accepted** (DB-enforced)."""

    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("packet_id", "path", "sha256"),)

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    channel_run_id: Mapped[str | None] = mapped_column(ForeignKey("channel_runs.id"))
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class QualityReport(Base):
    """SonarQube results per branch."""

    __tablename__ = "quality_reports"

    id: Mapped[str] = _id()
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), index=True)
    packet_id: Mapped[str | None] = mapped_column(ForeignKey("packets.id"), index=True)
    project_key: Mapped[str] = mapped_column(String(200), nullable=False)
    branch: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- governance layer


class AutonomyScope(Base):
    """Stage → mode (Requirement E). ``is_fixed`` scopes cannot change (DB-enforced)."""

    __tablename__ = "autonomy_scopes"

    stage: Mapped[Stage] = mapped_column(enum_type(Stage), primary_key=True)
    mode: Mapped[AutonomyMode] = mapped_column(enum_type(AutonomyMode), nullable=False)
    is_fixed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Confirmation(Base):
    """Approval queue entry (Requirement E)."""

    __tablename__ = "confirmations"
    __table_args__ = (
        # at most one *pending* confirmation per entity and stage (partial index, both dialects)
        Index(
            "uq_confirmations_pending_entity",
            "stage",
            "entity_type",
            "entity_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[str] = _id()
    stage: Mapped[Stage] = mapped_column(enum_type(Stage), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_ref: Mapped[str | None] = mapped_column(String(16))
    entity_version: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[ConfirmationStatus] = mapped_column(
        enum_type(ConfirmationStatus),
        default=ConfirmationStatus.PENDING,
        nullable=False,
        index=True,
    )
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Event(Base):
    """Audit log of every state change. **Append-only** (DB-enforced)."""

    __tablename__ = "events"

    id: Mapped[str] = _id()
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(128), index=True)  # ids and setting keys
    entity_ref: Mapped[str | None] = mapped_column(String(16))
    entity_version: Mapped[int | None] = mapped_column(Integer)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class ControlState(Base):
    """Single-row loop control: paused flag and pending interrupt prompt."""

    __tablename__ = "control_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interrupt_prompt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Exemplar(Base):
    """Ground-truth example per field / packet type for few-shot prompts and accuracy checks."""

    __tablename__ = "exemplars"

    id: Mapped[str] = _id()
    field_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    packet_type: Mapped[str] = mapped_column(String(64), default="task", nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    expected_value: Mapped[Any] = mapped_column(JSON, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class RefCounter(Base):
    """Atomic ref allocation (R1 fix 6): one row per counter, incremented in the transaction."""

    __tablename__ = "ref_counters"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False)


class FieldRegistryRevision(Base):
    """Snapshot of the candidate-field registry each time it changes (R1 fix 12). Append-only.

    M2 evaluation cycles reference ``id`` so every evaluation names the exact definitions used.
    """

    __tablename__ = "field_registry_revisions"

    id: Mapped[str] = _id()
    revision: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    field_count: Mapped[int] = mapped_column(Integer, nullable=False)
    definitions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- M2: evaPackets (B9)


class EvaluationCycle(Base):
    """Groups the evaPackets behind one readiness decision (B1), bound to exact versions."""

    __tablename__ = "evaluation_cycles"

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    packet_version: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[EvaPhase] = mapped_column(enum_type(EvaPhase), nullable=False)
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    project_version: Mapped[int | None] = mapped_column(Integer)
    objective_id: Mapped[str | None] = mapped_column(ForeignKey("objectives.id"))
    objective_version: Mapped[int | None] = mapped_column(Integer)
    registry_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("field_registry_revisions.id")
    )
    pass_plan: Mapped[str] = mapped_column(String(32), nullable=False)
    models: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    state: Mapped[CycleState] = mapped_column(
        enum_type(CycleState), default=CycleState.OPEN, nullable=False
    )
    created_at: Mapped[datetime] = _created()
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = _updated()


class EvaPreset(Base):
    """A preset evaluative prompt for one field and phase. **Append-only**: a change is a new
    revision; the highest revision per (field, phase) is current."""

    __tablename__ = "eva_presets"
    __table_args__ = (
        UniqueConstraint("field_name", "phase", "revision"),
        CheckConstraint("tier IN (1, 2, 3)", name="tier"),
    )

    id: Mapped[str] = _id()
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[EvaPhase] = mapped_column(enum_type(EvaPhase), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 gating, 2 routing, 3 optional
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    hard: Mapped[bool] = mapped_column(Boolean, nullable=False)  # hard gates can never be waived
    gate_operator: Mapped[str | None] = mapped_column(String(16))
    gate_value: Mapped[str | None] = mapped_column(String(64))
    plan: Mapped[PassPlanName] = mapped_column(enum_type(PassPlanName), nullable=False)
    prompt_template: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = _created()


class EvaPacket(Base):
    """One field of one genPacket version, one phase, one preset revision, one target hash."""

    __tablename__ = "eva_packets"

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    packet_version: Mapped[int] = mapped_column(Integer, nullable=False)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_cycles.id"), nullable=False, index=True
    )
    preset_id: Mapped[str] = mapped_column(ForeignKey("eva_presets.id"), nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: M2.1 fix 4: the field definition version evaluated; consensus is current only while it
    #: equals ``candidate_field_definitions.version``
    definition_version: Mapped[int | None] = mapped_column(Integer)
    phase: Mapped[EvaPhase] = mapped_column(enum_type(EvaPhase), nullable=False)
    plan: Mapped[PassPlanName] = mapped_column(enum_type(PassPlanName), nullable=False)
    #: consensus items: the vocabulary for tag sets, [""] otherwise (bound at creation)
    item_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_address: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    state: Mapped[EvaState] = mapped_column(
        enum_type(EvaState), default=EvaState.PLANNED, nullable=False
    )
    escalation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # B12
    escalated_from_id: Mapped[str | None] = mapped_column(ForeignKey("eva_packets.id"))
    reused_from_id: Mapped[str | None] = mapped_column(ForeignKey("eva_packets.id"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class EvaluationPassItem(Base):
    """One consensus item of one valid pass: the tag (tag sets) or "" (other kinds).

    Extraction passes have an item only for non-null values; evaluation passes have an item for
    each item of the value they evaluate, with *verified* evidence counts. **Append-only.**
    """

    __tablename__ = "evaluation_pass_items"
    __table_args__ = (
        UniqueConstraint("pass_id", "item_key"),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 10)", name="score_range"),
    )

    id: Mapped[str] = _id()
    pass_id: Mapped[str] = mapped_column(ForeignKey("evaluation_passes.id"), nullable=False)
    eva_packet_id: Mapped[str] = mapped_column(
        ForeignKey("eva_packets.id"), nullable=False, index=True
    )
    pass_no: Mapped[int] = mapped_column(Integer, nullable=False)
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value_norm: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(Integer)
    supports: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    contradictions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unverified: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _created()


class QueueOverride(Base):
    """A human queue decision (B9). **Append-only**; revoked through ``revocations``."""

    __tablename__ = "queue_overrides"
    __table_args__ = (
        # only BLOCK and HOLD may cover the whole lineage
        CheckConstraint("scope = 'version' OR kind IN ('block', 'hold')", name="lineage_scope"),
    )

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    packet_ref: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    kind: Mapped[OverrideKind] = mapped_column(enum_type(OverrideKind), nullable=False)
    scope: Mapped[OverrideScope] = mapped_column(enum_type(OverrideScope), nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[float | None] = mapped_column(Float)  # REORDER: priority delta
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class Waiver(Base):
    """A version-bound human acceptance of one *soft* field (B9). **Append-only**."""

    __tablename__ = "waivers"

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    packet_ref: Mapped[str] = mapped_column(String(16), nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[EvaPhase] = mapped_column(enum_type(EvaPhase), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class Revocation(Base):
    """Revokes one waiver or queue override (its own append-only record, B9)."""

    __tablename__ = "revocations"
    __table_args__ = (
        UniqueConstraint("target_type", "target_id"),
        CheckConstraint("target_type IN ('waiver', 'override', 'dependency')", name="target_type"),
    )

    id: Mapped[str] = _id()
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)  # waiver | override
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class PacketDependency(Base):
    """A typed execution edge (D1). Parent/child hierarchy is *not* a dependency."""

    __tablename__ = "packet_dependencies"
    __table_args__ = (UniqueConstraint("packet_id", "depends_on_id", "kind"),)

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    depends_on_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False)
    kind: Mapped[DependencyKind] = mapped_column(enum_type(DependencyKind), nullable=False)
    satisfied_when: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    created_at: Mapped[datetime] = _created()


class GenerationRun(Base):
    """One generation dispatch of a genPacket version (C24): owns the run state machine
    (``orchestrator.transitions``), the lease, the fencing token and the frozen snapshot.

    DB-enforced (``db/ddl.py``): the snapshot, binding columns and fencing token never change; the
    gate revision only moves by +1 (R1); manifest hash and commit SHA are write-once; a terminal
    run, or an accepted run whose commit is recorded, never changes; state changes follow the
    run table."""

    __tablename__ = "generation_runs"

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    packet_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    claimed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    #: M2 column (unused since M3: attempts point at their run). ``use_alter`` breaks the FK
    #: cycle with ``channel_runs.generation_run_id`` so schema comparison still checks both.
    channel_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("channel_runs.id", use_alter=True)
    )
    state: Mapped[GenerationRunState] = mapped_column(
        enum_type(GenerationRunState), default=GenerationRunState.PLANNED, nullable=False
    )
    evaluation_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    # M3 (C24, C26, C27, C29, C30, C32). Legacy M2 rows have NULL fencing token and channel.
    channel: Mapped[Channel | None] = mapped_column(enum_type(Channel))
    fencing_token: Mapped[int | None] = mapped_column(Integer)
    gate_revision: Mapped[int | None] = mapped_column(Integer)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    max_attempts: Mapped[int | None] = mapped_column(Integer)
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    #: stable conversation / resume identity (exit 3 resumes the same conversation, C27)
    resume_key: Mapped[str | None] = mapped_column(String(128))
    model_identity: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    model_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    manifest_hash: Mapped[str | None] = mapped_column(String(64))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    expected_head: Mapped[str | None] = mapped_column(String(64))
    failure_kind: Mapped[str | None] = mapped_column(String(32))
    #: True once this run's failure was added to the packet's lifetime failure budget
    charged: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    stale_reason: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    #: R6 OQ20: the packet's ``content_revision`` at dispatch; the seal reconstructs exactly
    #: that revision from ``field_revisions`` (edits during the run carry to the child)
    claim_content_revision: Mapped[int | None] = mapped_column(Integer)


class QueueAdmission(Base):
    """One admission ready → queued, with its class (fair admission, B9). **Append-only**."""

    __tablename__ = "queue_admissions"

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    packet_version: Mapped[int] = mapped_column(Integer, nullable=False)
    admission_class: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _created()


class BranchApplication(Base):
    """Idempotency record of one split (B5): unique per packet version × rule × rule version."""

    __tablename__ = "branch_applications"
    __table_args__ = (
        UniqueConstraint("packet_id", "packet_version", "threshold_id", "threshold_version"),
    )

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False)
    packet_version: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_id: Mapped[str] = mapped_column(ForeignKey("thresholds.id"), nullable=False)
    threshold_version: Mapped[int] = mapped_column(Integer, nullable=False)
    children: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- M3: control layer


class KernelLease(Base):
    """The single-kernel lock (C26): a lease row taken by conditional UPDATE. ``epoch`` grows on
    every acquisition, so an old owner is fenced out of every later guarded write."""

    __tablename__ = "kernel_leases"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[str | None] = mapped_column(String(64))
    epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    acquired_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = _updated()


class GovernorState(Base):
    """Persisted status of one governance scope (C26, C33): ``normal`` or ``paused``. A pause
    clears only on an explicit owner resume. ``revision`` grows with every change and with
    every dispatch authorised in the scope, which serialises dispatch with pause."""

    __tablename__ = "governor_states"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id"),)

    id: Mapped[str] = _id()
    scope_type: Mapped[ScopeType] = mapped_column(enum_type(ScopeType), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ScopeStatus] = mapped_column(
        enum_type(ScopeStatus), default=ScopeStatus.NORMAL, nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    changed_by: Mapped[str | None] = mapped_column(String(64))
    last_human_touch_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class GovernorEvent(Base):
    """Every governor observation and action (C33). **Append-only**; ``observation_key`` makes
    each observation count once."""

    __tablename__ = "governor_events"

    id: Mapped[str] = _id()
    scope_type: Mapped[ScopeType] = mapped_column(enum_type(ScopeType), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signal: Mapped[str] = mapped_column(String(64), nullable=False)
    observed: Mapped[str] = mapped_column(String(16), nullable=False)  # breach|clean|unknown|…
    value_e4: Mapped[int | None] = mapped_column(Integer)
    threshold_e4: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()


class BudgetCounter(Base):
    """Used capacity of one scope × metric × window (C36). Reserved by conditional UPDATE
    (``used + n <= limit``), atomically with an append-only ``BudgetReservation``."""

    __tablename__ = "budget_counters"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "metric", "period", "period_start"),
        CheckConstraint("used >= 0", name="used_nonnegative"),
    )

    id: Mapped[str] = _id()
    scope_type: Mapped[ScopeType] = mapped_column(enum_type(ScopeType), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(16), nullable=False)  # runs | tokens | packets
    period: Mapped[str] = mapped_column(String(8), nullable=False)  # hour | day
    period_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = _updated()


class BudgetReservation(Base):
    """One reservation of capacity before an external attempt (C36). **Append-only**."""

    __tablename__ = "budget_reservations"
    __table_args__ = (
        UniqueConstraint("operation_id", "counter_id"),
        CheckConstraint("amount >= 0", name="amount_nonnegative"),
    )

    id: Mapped[str] = _id()
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    counter_id: Mapped[str] = mapped_column(ForeignKey("budget_counters.id"), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    packet_id: Mapped[str | None] = mapped_column(ForeignKey("packets.id"))
    generation_run_id: Mapped[str | None] = mapped_column(ForeignKey("generation_runs.id"))
    created_at: Mapped[datetime] = _created()


class BudgetReconciliation(Base):
    """Actual usage of one reservation, recorded exactly once (C36). **Append-only**. A
    reservation with unknown usage has no row and keeps its reservation."""

    __tablename__ = "budget_reconciliations"
    __table_args__ = (CheckConstraint("actual >= 0", name="actual_nonnegative"),)

    id: Mapped[str] = _id()
    reservation_id: Mapped[str] = mapped_column(
        ForeignKey("budget_reservations.id"), unique=True, nullable=False
    )
    actual: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created()


class BudgetTopup(Base):
    """An owner-authorised budget top-up (C27 failure allowance, C36). **Append-only**."""

    __tablename__ = "budget_topups"
    __table_args__ = (CheckConstraint("amount >= 0", name="amount_nonnegative"),)

    id: Mapped[str] = _id()
    scope_type: Mapped[ScopeType] = mapped_column(enum_type(ScopeType), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    period: Mapped[str | None] = mapped_column(String(8))
    period_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------- M3a: genPacket loop


class ContentBlob(Base):
    """A value stored once and referenced by its sha256 (T1-C v3 §1). **Insert-once**."""

    __tablename__ = "content_blobs"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class RawRevisionOutbox(Base):
    """Transient raw capture of an audited column change (T1-C v3 H1). Written only by the
    AFTER UPDATE triggers; the kernel drains it synchronously into ``content_blobs`` and
    ``field_revisions`` (R6 OQ35) and deletes the row."""

    __tablename__ = "raw_revision_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tx_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    producer: Mapped[str] = mapped_column(String(64), nullable=False)
    content_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = _created()


class FieldRevision(Base):
    """One change of one audited field, hash-only (T1-C v3 §1, Q6). **Append-only**."""

    __tablename__ = "field_revisions"

    id: Mapped[str] = _id()
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    old_hash: Mapped[str] = mapped_column(ForeignKey("content_blobs.hash"), nullable=False)
    new_hash: Mapped[str] = mapped_column(ForeignKey("content_blobs.hash"), nullable=False)
    #: Q6: the hash this value was derived from (the field's previous value)
    based_on_hash: Mapped[str | None] = mapped_column(String(64))
    producer: Mapped[str] = mapped_column(String(64), nullable=False)
    content_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: sha256 of every audited column of the entity after this revision group
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created()


class SealedBundle(Base):
    """The one permanent snapshot of a genPacket after ``gen_eva`` (T1-C v3 §1, R6 OQ13/OQ20).
    **Append-only**; ``UNIQUE(packet_id)``: a genPacket seals once."""

    __tablename__ = "sealed_bundles"

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), unique=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("generation_runs.id"))
    claim_content_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    #: the canonical JSON snapshot, stored in ``content_blobs``
    snapshot_hash: Mapped[str] = mapped_column(ForeignKey("content_blobs.hash"), nullable=False)
    #: sha256 over the snapshot hash, grade and artifact hash
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    composite_grade_e4: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_hash: Mapped[str | None] = mapped_column(String(64))
    #: OQ15: the commit of the output on ``refs/gen/<run_id>`` (NULL with no repository)
    output_ref: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()


class EvaContextLink(Base):
    """One context source an evaPacket read (R6 OQ22). **Append-only** except that
    ``is_questioned`` may go from false to true (OQ23, OQ34; DB-enforced)."""

    __tablename__ = "eva_context_links"
    __table_args__ = (
        CheckConstraint("link_strength IN ('direct', 'peripheral')", name="strength"),
        CheckConstraint(
            "source_type IN ('project', 'objective', 'packet', 'definition')", name="source_type"
        ),
    )

    id: Mapped[str] = _id()
    eva_packet_id: Mapped[str] = mapped_column(
        ForeignKey("eva_packets.id"), nullable=False, index=True
    )
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: the source's ``content_revision`` (or definition version) when the evaPacket read it
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    link_strength: Mapped[str] = mapped_column(String(16), nullable=False)
    #: direct_quote | hard_dep | ambient | carried | definition
    link_reason: Mapped[str] = mapped_column(String(16), nullable=False)
    quote: Mapped[str | None] = mapped_column(Text)
    is_questioned: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    created_at: Mapped[datetime] = _created()


class EvaDiscardLog(Base):
    """An evaluation whose commit lost the lockout / content-revision CAS (R6 OQ30).
    **Append-only**."""

    __tablename__ = "eva_discard_log"

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    cycle_id: Mapped[str | None] = mapped_column(ForeignKey("evaluation_cycles.id"))
    epoch_id: Mapped[int | None] = mapped_column(Integer)
    expected_content_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_content_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    locked_out: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class QueueEpochSnapshot(Base):
    """genQueue as computed at one admission tick, dispatch or completion (R6 OQ31). Lockout
    and dispatch read the newest epoch, never the live ``ROW_NUMBER``. **Append-only**."""

    __tablename__ = "queue_epoch_snapshot"
    __table_args__ = (UniqueConstraint("epoch_id", "packet_id"),)

    id: Mapped[str] = _id()
    epoch_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    queue_pos: Mapped[int | None] = mapped_column(Integer)
    is_locked_out: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score_e4: Mapped[int | None] = mapped_column(Integer)
    cause: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _created()


class FeedbackEvent(Base):
    """A human queue weight (R6 OQ9): BOOST, NUDGE, DEMOTE or PIN. **Append-only**."""

    __tablename__ = "feedback_events"
    __table_args__ = (CheckConstraint("kind IN ('boost', 'nudge', 'demote', 'pin')", name="kind"),)

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class CrashEvent(Base):
    """A generation that ended with no gradable output (R6 OQ18). **Append-only**."""

    __tablename__ = "crash_events"

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("generation_runs.id"), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class RouteHealth(Base):
    """Per-channel route health (R6 OQ18): unhealthy until ``unhealthy_until`` after three
    crashes within 300 s. Rows are never deleted."""

    __tablename__ = "route_health"

    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    unhealthy_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    crashes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = _updated()


TABLE_NAMES: tuple[str, ...] = (
    "projects",
    "objectives",
    "packets",
    "candidate_field_definitions",
    "evaluation_passes",
    "thresholds",
    "settings",
    "channel_runs",
    "artifacts",
    "quality_reports",
    "autonomy_scopes",
    "confirmations",
    "events",
    "control_state",
    "exemplars",
    "ref_counters",
    "field_registry_revisions",
    "evaluation_cycles",
    "eva_presets",
    "eva_packets",
    "evaluation_pass_items",
    "queue_overrides",
    "waivers",
    "revocations",
    "packet_dependencies",
    "generation_runs",
    "queue_admissions",
    "branch_applications",
    "channel_run_outcomes",
    "kernel_leases",
    "governor_states",
    "governor_events",
    "budget_counters",
    "budget_reservations",
    "budget_reconciliations",
    "budget_topups",
    "content_blobs",
    "raw_revision_outbox",
    "field_revisions",
    "sealed_bundles",
    "eva_context_links",
    "eva_discard_log",
    "queue_epoch_snapshot",
    "feedback_events",
    "crash_events",
    "route_health",
)
