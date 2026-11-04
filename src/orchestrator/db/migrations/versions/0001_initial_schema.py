"""Initial schema: every table in PROJECT_PLAN §6 plus the R0 amendments (A2, A3).

Revision ID: 0001
Revises: (none)
Create Date: 2026-11-03 23:48:41.171919
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "autonomy_scopes",
        sa.Column(
            "stage",
            sa.Enum(
                "ingestion",
                "objective_llm_values",
                "candidate_promotion",
                "branching",
                "routing",
                "local_generation",
                "workbench_production",
                "repo_write",
                "quality_scan",
                name="stage",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "mode",
            sa.Enum(
                "manual",
                "confirm",
                "auto",
                name="autonomymode",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("is_fixed", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("stage", name=op.f("pk_autonomy_scopes")),
    )
    op.create_table(
        "candidate_field_definitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column(
            "level",
            sa.Enum(
                "task",
                "content",
                name="fieldlevel",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "tagset",
                "score",
                "text",
                "bool",
                name="fieldkind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("vocabulary", sa.JSON(), nullable=True),
        sa.Column("scale", sa.String(length=16), nullable=True),
        sa.Column("max_chars", sa.Integer(), nullable=True),
        sa.Column("max_passes", sa.Integer(), nullable=True),
        sa.Column("prompt_hint", sa.Text(), nullable=False),
        sa.Column("is_key_field", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("contract", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_field_definitions")),
        sa.UniqueConstraint("name", name=op.f("uq_candidate_field_definitions_name")),
    )
    op.create_table(
        "confirmations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "stage",
            sa.Enum(
                "ingestion",
                "objective_llm_values",
                "candidate_promotion",
                "branching",
                "routing",
                "local_generation",
                "workbench_production",
                "repo_write",
                "quality_scan",
                name="stage",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("entity_ref", sa.String(length=16), nullable=True),
        sa.Column("entity_version", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                name="confirmationstatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_confirmations")),
    )
    with op.batch_alter_table("confirmations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_confirmations_status"), ["status"], unique=False)
        batch_op.create_index(
            "uq_confirmations_pending_entity",
            ["stage", "entity_type", "entity_id"],
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        )

    op.create_table(
        "control_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("interrupt_prompt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_state")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=True),
        sa.Column("entity_ref", sa.String(length=16), nullable=True),
        sa.Column("entity_version", sa.Integer(), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_events_action"), ["action"], unique=False)
        batch_op.create_index(batch_op.f("ix_events_entity_id"), ["entity_id"], unique=False)

    op.create_table(
        "exemplars",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("packet_type", sa.String(length=64), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("expected_value", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exemplars")),
    )
    with op.batch_alter_table("exemplars", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_exemplars_field_name"), ["field_name"], unique=False)

    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ref", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("industry", sa.String(length=200), nullable=True),
        sa.Column("sectors", sa.JSON(), nullable=False),
        sa.Column("core_values", sa.JSON(), nullable=False),
        sa.Column("goals", sa.JSON(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("reference_list", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("tree_contract", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["projects.id"], name=op.f("fk_projects_supersedes_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("ref", "version", name=op.f("uq_projects_ref")),
        sa.UniqueConstraint("supersedes_id", name=op.f("uq_projects_supersedes_id")),
    )
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_projects_ref"), ["ref"], unique=False)

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "thresholds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column(
            "operator",
            sa.Enum(
                ">=",
                ">",
                "<=",
                "<",
                "=",
                "!=",
                "has_tag",
                "status_is",
                name="thresholdoperator",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("value", sa.String(length=200), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "require_approval",
                "split",
                "route_workbench_production",
                "route_local_generation",
                name="thresholdaction",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("precedence", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_thresholds")),
        sa.UniqueConstraint("name", name=op.f("uq_thresholds_name")),
    )
    op.create_table(
        "objectives",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ref", sa.String(length=16), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("priority_level", sa.Integer(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_objectives_project_id_projects")
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["objectives.id"],
            name=op.f("fk_objectives_supersedes_id_objectives"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_objectives")),
        sa.UniqueConstraint("ref", "version", name=op.f("uq_objectives_ref")),
        sa.UniqueConstraint("supersedes_id", name=op.f("uq_objectives_supersedes_id")),
    )
    with op.batch_alter_table("objectives", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_objectives_project_id"), ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_objectives_ref"), ["ref"], unique=False)

    op.create_table(
        "packets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ref", sa.String(length=16), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("objective_id", sa.String(length=36), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "packet_kind",
            sa.Enum(
                "task",
                "content_eval",
                name="packetkind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("prompt_description", sa.Text(), nullable=False),
        sa.Column("response_structure", sa.String(length=64), nullable=False),
        sa.Column("response_contract", sa.String(length=80), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "quickfire_evaluation",
                "local_generation",
                "workbench_production",
                name="channel",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("priority_level", sa.Float(), nullable=True),
        sa.Column("manual_boost", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "queued",
                "evaluating",
                "ready",
                "in_progress",
                "awaiting_confirmation",
                "processed",
                "completed",
                "archived",
                "failed",
                name="packetstatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("evaluations_count", sa.Integer(), nullable=False),
        sa.Column("repo_target_path", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("root_id", sa.String(length=36), nullable=True),
        sa.Column("branch_reason", sa.Text(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["objective_id"], ["objectives.id"], name=op.f("fk_packets_objective_id_objectives")
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["packets.id"], name=op.f("fk_packets_parent_id_packets")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_packets_project_id_projects")
        ),
        sa.ForeignKeyConstraint(
            ["root_id"], ["packets.id"], name=op.f("fk_packets_root_id_packets")
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["packets.id"], name=op.f("fk_packets_supersedes_id_packets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_packets")),
        sa.UniqueConstraint("ref", "version", name=op.f("uq_packets_ref")),
        sa.UniqueConstraint("supersedes_id", name=op.f("uq_packets_supersedes_id")),
    )
    with op.batch_alter_table("packets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_packets_objective_id"), ["objective_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_packets_parent_id"), ["parent_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_packets_project_id"), ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_packets_ref"), ["ref"], unique=False)
        batch_op.create_index(batch_op.f("ix_packets_root_id"), ["root_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_packets_status"), ["status"], unique=False)

    op.create_table(
        "channel_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "quickfire_evaluation",
                "local_generation",
                "workbench_production",
                name="channel",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("packet_id", sa.String(length=36), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("contract", sa.String(length=80), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("policy_revision", sa.String(length=64), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "succeeded",
                "invalid",
                "failed",
                name="runstatus",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["packet_id"], ["packets.id"], name=op.f("fk_channel_runs_packet_id_packets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_runs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_channel_runs_idempotency_key")),
        sa.UniqueConstraint("run_id", name=op.f("uq_channel_runs_run_id")),
    )
    with op.batch_alter_table("channel_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_channel_runs_packet_id"), ["packet_id"], unique=False)

    op.create_table(
        "evaluation_passes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("packet_id", sa.String(length=36), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("eval_cycle_id", sa.String(length=36), nullable=True),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("pass_no", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("target_pass_no", sa.Integer(), nullable=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["packet_id"], ["packets.id"], name=op.f("fk_evaluation_passes_packet_id_packets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_passes")),
        sa.UniqueConstraint(
            "eval_cycle_id",
            "field_name",
            "pass_no",
            "attempt",
            name=op.f("uq_evaluation_passes_eval_cycle_id"),
        ),
    )
    with op.batch_alter_table("evaluation_passes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_evaluation_passes_eval_cycle_id"), ["eval_cycle_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_evaluation_passes_field_name"), ["field_name"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_evaluation_passes_packet_id"), ["packet_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_evaluation_passes_run_id"), ["run_id"], unique=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("packet_id", sa.String(length=36), nullable=False),
        sa.Column("channel_run_id", sa.String(length=36), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_run_id"],
            ["channel_runs.id"],
            name=op.f("fk_artifacts_channel_run_id_channel_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["packet_id"], ["packets.id"], name=op.f("fk_artifacts_packet_id_packets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
        sa.UniqueConstraint("packet_id", "path", "sha256", name=op.f("uq_artifacts_packet_id")),
    )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_artifacts_packet_id"), ["packet_id"], unique=False)

    op.create_table(
        "quality_reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=True),
        sa.Column("packet_id", sa.String(length=36), nullable=True),
        sa.Column("project_key", sa.String(length=200), nullable=False),
        sa.Column("branch", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["artifacts.id"], name=op.f("fk_quality_reports_artifact_id_artifacts")
        ),
        sa.ForeignKeyConstraint(
            ["packet_id"], ["packets.id"], name=op.f("fk_quality_reports_packet_id_packets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quality_reports")),
    )
    with op.batch_alter_table("quality_reports", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_quality_reports_artifact_id"), ["artifact_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_quality_reports_packet_id"), ["packet_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("quality_reports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_quality_reports_packet_id"))
        batch_op.drop_index(batch_op.f("ix_quality_reports_artifact_id"))

    op.drop_table("quality_reports")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifacts_packet_id"))

    op.drop_table("artifacts")
    with op.batch_alter_table("evaluation_passes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_evaluation_passes_run_id"))
        batch_op.drop_index(batch_op.f("ix_evaluation_passes_packet_id"))
        batch_op.drop_index(batch_op.f("ix_evaluation_passes_field_name"))
        batch_op.drop_index(batch_op.f("ix_evaluation_passes_eval_cycle_id"))

    op.drop_table("evaluation_passes")
    with op.batch_alter_table("channel_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_channel_runs_packet_id"))

    op.drop_table("channel_runs")
    with op.batch_alter_table("packets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_packets_status"))
        batch_op.drop_index(batch_op.f("ix_packets_root_id"))
        batch_op.drop_index(batch_op.f("ix_packets_ref"))
        batch_op.drop_index(batch_op.f("ix_packets_project_id"))
        batch_op.drop_index(batch_op.f("ix_packets_parent_id"))
        batch_op.drop_index(batch_op.f("ix_packets_objective_id"))

    op.drop_table("packets")
    with op.batch_alter_table("objectives", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_objectives_ref"))
        batch_op.drop_index(batch_op.f("ix_objectives_project_id"))

    op.drop_table("objectives")
    op.drop_table("thresholds")
    op.drop_table("settings")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projects_ref"))

    op.drop_table("projects")
    with op.batch_alter_table("exemplars", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_exemplars_field_name"))

    op.drop_table("exemplars")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_events_entity_id"))
        batch_op.drop_index(batch_op.f("ix_events_action"))

    op.drop_table("events")
    op.drop_table("control_state")
    with op.batch_alter_table("confirmations", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_confirmations_pending_entity",
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        )
        batch_op.drop_index(batch_op.f("ix_confirmations_status"))

    op.drop_table("confirmations")
    op.drop_table("candidate_field_definitions")
    op.drop_table("autonomy_scopes")
