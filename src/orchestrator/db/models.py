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
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from orchestrator.contracts import (
    DEFAULT_RESPONSE_STRUCTURE,
    AutonomyMode,
    Channel,
    ConfirmationStatus,
    FieldKind,
    FieldLevel,
    PacketKind,
    PacketStatus,
    RunStatus,
    Stage,
    ThresholdAction,
    ThresholdOperator,
)

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
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


def _version() -> Mapped[int]:
    return mapped_column(Integer, default=1, nullable=False)


def _locked() -> Mapped[bool]:
    return mapped_column(Boolean, default=False, nullable=False)


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
    version: Mapped[int] = _version()
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("objectives.id"), unique=True)
    locked: Mapped[bool] = _locked()
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    project: Mapped[Project] = relationship()


class Packet(Base):
    """A Task = Prompt Packet: core fields + master governance fields."""

    __tablename__ = "packets"
    __table_args__ = (UniqueConstraint("ref", "version"),)

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
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
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
    __table_args__ = (UniqueConstraint("eval_cycle_id", "field_name", "pass_no", "attempt"),)

    id: Mapped[str] = _id()
    packet_id: Mapped[str] = mapped_column(ForeignKey("packets.id"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    eval_cycle_id: Mapped[str | None] = mapped_column(String(36), index=True)
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
    """One Run-ID contract: an attempt to obtain a valid output from a channel."""

    __tablename__ = "channel_runs"

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
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[RunStatus] = mapped_column(
        enum_type(RunStatus), default=RunStatus.PENDING, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


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
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True)
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
)
