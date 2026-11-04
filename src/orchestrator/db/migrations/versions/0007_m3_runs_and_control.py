"""M3 Stage A: durable runs, fencing, the kernel lock, governor and budget tables (C22–C26).

* ``generation_runs`` (C24): the run state machine (``planned → dispatched → awaiting_result →
  validating → accepted | failed | stale``, plus ``awaiting_confirmation`` for exit 3), the
  channel, fencing token, live gate revision, lease, attempts, deadline, resume identity, model
  identity (C29), manifest hash and commit SHA (C30), expected HEAD, failure kind and budget
  charge. Legacy M2 states stay valid.
* ``channel_runs`` (C24) becomes **append-only**: one row per attempt, linked to its generation
  run or evaPacket, with a durable operation ID (unique) and the fencing token it ran under.
  Outcomes go to the new append-only ``channel_run_outcomes`` (one per attempt).
* ``packets.fencing_token`` and ``packets.active_run_id`` (C26, E7).
* ``kernel_leases`` (C26 single-kernel lock), ``governor_states`` and append-only
  ``governor_events`` (C26, C33), ``budget_counters`` plus append-only
  ``budget_reservations``, ``budget_reconciliations`` and ``budget_topups`` (C27, C36).
* Triggers on both dialects: the packet and run **transition tables** (any state change not in
  ``orchestrator.transitions`` is rejected), finished runs and recorded commits are final, run
  bindings are frozen (gate revision +1 only, manifest/commit write-once), the new append-only
  tables reject UPDATE/DELETE (PostgreSQL also TRUNCATE), and control rows are never deleted.

Nothing from 0001–0006 is recreated (C22): ``eva_packets``, ``evaluation_cycles`` and the
``content_eval`` ban are untouched. One new view, v_run_attempts (latest attempt per run, D10 Q3); existing views are untouched.

FROZEN: the trigger SQL is a literal copy of ``orchestrator.db.ddl.m3_statements`` (tests
assert equality). Nothing is imported from the application. Never edit this file after it is
applied. (While the M3 pull request is unmerged, 0007 is still being authored: it is frozen
when M3 merges, as 0002/0003 were for M1.)

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_STATES_0005 = ("claimed", "succeeded", "failed", "abandoned")
RUN_STATES_0007 = (
    "planned",
    "dispatched",
    "awaiting_result",
    "validating",
    "accepted",
    "failed",
    "stale",
    "awaiting_confirmation",
    "claimed",
    "succeeded",
    "abandoned",
)
CHANNELS = ("quickfire_evaluation", "local_generation", "workbench_production")
SCOPE_TYPES = ("global", "channel", "project", "objective", "branch", "packet")
SCOPE_STATUSES = ("normal", "paused")
OUTCOMES = (
    "succeeded",
    "invalid",
    "failed",
    "timeout",
    "parked",
    "config_error",
    "stale",
    "unknown",
)
#: (name, SQL type, NOT NULL default) of every column 0007 adds to an existing table
RUN_COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    ("channel", "VARCHAR(32)", None),
    ("fencing_token", "INTEGER", None),
    ("gate_revision", "INTEGER", None),
    ("lease_expires_at", "DATETIME", None),
    ("heartbeat_at", "DATETIME", None),
    ("attempts", "INTEGER", "0"),
    ("max_attempts", "INTEGER", None),
    ("deadline_at", "DATETIME", None),
    ("resume_key", "VARCHAR(128)", None),
    ("model_identity", "JSON", None),
    ("model_verified", "BOOLEAN", "0"),
    ("manifest_hash", "VARCHAR(64)", None),
    ("commit_sha", "VARCHAR(64)", None),
    ("expected_head", "VARCHAR(64)", None),
    ("failure_kind", "VARCHAR(32)", None),
    ("charged", "BOOLEAN", "0"),
    ("stale_reason", "TEXT", None),
    ("finished_at", "DATETIME", None),
)
NEW_TABLES = (
    "budget_topups",
    "budget_reconciliations",
    "budget_reservations",
    "budget_counters",
    "governor_events",
    "governor_states",
    "kernel_leases",
    "channel_run_outcomes",
)
PG_FUNCTIONS = (
    "orch_packets_transition_guard",
    "orch_generation_runs_m3_guard",
    "orch_kernel_leases_guard",
    "orch_governor_states_guard",
    "orch_no_delete_guard",
)


def _check_sql(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _run(statements: Sequence[str]) -> None:
    cursor = op.get_bind().connection.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
    finally:
        cursor.close()


def _sqlite_edit_schema(table: str, transform: Callable[[str], str]) -> None:
    """SQLite's documented procedure for format-neutral changes (see 0005/0006): rewrite the
    stored CREATE TABLE text under ``writable_schema`` and bump ``schema_version``."""
    bind = op.get_bind()
    old = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).scalar_one()
    new = transform(old)
    if new == old:
        raise RuntimeError(f"{table}: schema edit matched nothing")
    version = bind.exec_driver_sql("PRAGMA schema_version").scalar_one()
    bind.exec_driver_sql("PRAGMA writable_schema = ON")
    bind.exec_driver_sql(
        "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?", (new, table)
    )
    bind.exec_driver_sql(f"PRAGMA schema_version = {int(version) + 1}")
    bind.exec_driver_sql("PRAGMA writable_schema = OFF")


def _replace(old: str, new: str) -> Callable[[str], str]:
    def transform(sql: str) -> str:
        if old not in sql:
            raise RuntimeError(f"expected text not found: {old[:60]}")
        return sql.replace(old, new)

    return transform


def _trigger_names(statements: Sequence[str]) -> list[tuple[str, str]]:
    found = []
    for statement in statements:
        match = re.match(r"CREATE TRIGGER (\w+) .*? ON (\w+)", statement, re.DOTALL)
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def _refuse_if(sql: str, message: str) -> None:
    count = op.get_bind().execute(sa.text(sql)).scalar_one()
    if count:
        raise RuntimeError(f"{count} {message}")


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _scope(prefix: str) -> list[sa.Column]:
    return [
        sa.Column("scope_type", _enum("scopetype", *SCOPE_TYPES), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
    ]


def _create_tables() -> None:
    op.create_table(
        "channel_run_outcomes",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column(
            "channel_run_id", sa.String(36), sa.ForeignKey("channel_runs.id"), nullable=False
        ),
        sa.Column(
            "generation_run_id", sa.String(36), sa.ForeignKey("generation_runs.id"), nullable=True
        ),
        sa.Column("outcome", _enum("attemptoutcome", *OUTCOMES), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("output_hash", sa.String(64), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_run_outcomes")),
        sa.UniqueConstraint("channel_run_id", name=op.f("uq_channel_run_outcomes_channel_run_id")),
    )
    op.create_index(
        "ix_channel_run_outcomes_generation_run_id", "channel_run_outcomes", ["generation_run_id"]
    )
    op.create_table(
        "kernel_leases",
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("holder", sa.String(64), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        _ts("acquired_at", True),
        _ts("heartbeat_at", True),
        _ts("lease_expires_at", True),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_kernel_leases")),
    )
    op.create_table(
        "governor_states",
        sa.Column("id", sa.String(36), nullable=False),
        *_scope("governor_states"),
        sa.Column("status", _enum("scopestatus", *SCOPE_STATUSES), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("changed_by", sa.String(64), nullable=True),
        _ts("last_human_touch_at", True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governor_states")),
        sa.UniqueConstraint("scope_type", "scope_id", name=op.f("uq_governor_states_scope_type")),
    )
    op.create_table(
        "governor_events",
        sa.Column("id", sa.String(36), nullable=False),
        *_scope("governor_events"),
        sa.Column("signal", sa.String(64), nullable=False),
        sa.Column("observed", sa.String(16), nullable=False),
        sa.Column("value_e4", sa.Integer(), nullable=True),
        sa.Column("threshold_e4", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("observation_key", sa.String(160), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governor_events")),
        sa.UniqueConstraint("observation_key", name=op.f("uq_governor_events_observation_key")),
    )
    op.create_table(
        "budget_counters",
        sa.Column("id", sa.String(36), nullable=False),
        *_scope("budget_counters"),
        sa.Column("metric", sa.String(16), nullable=False),
        sa.Column("period", sa.String(8), nullable=False),
        _ts("period_start"),
        sa.Column("used", sa.Integer(), nullable=False),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_counters")),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "metric",
            "period",
            "period_start",
            name=op.f("uq_budget_counters_scope_type"),
        ),
    )
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("counter_id", sa.String(36), sa.ForeignKey("budget_counters.id"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("packet_id", sa.String(36), sa.ForeignKey("packets.id"), nullable=True),
        sa.Column(
            "generation_run_id", sa.String(36), sa.ForeignKey("generation_runs.id"), nullable=True
        ),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_reservations")),
        sa.UniqueConstraint(
            "operation_id", "counter_id", name=op.f("uq_budget_reservations_operation_id")
        ),
    )
    op.create_index("ix_budget_reservations_operation_id", "budget_reservations", ["operation_id"])
    op.create_table(
        "budget_reconciliations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column(
            "reservation_id",
            sa.String(36),
            sa.ForeignKey("budget_reservations.id"),
            nullable=False,
        ),
        sa.Column("actual", sa.Integer(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_reconciliations")),
        sa.UniqueConstraint(
            "reservation_id", name=op.f("uq_budget_reconciliations_reservation_id")
        ),
    )
    op.create_table(
        "budget_topups",
        sa.Column("id", sa.String(36), nullable=False),
        *_scope("budget_topups"),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("period", sa.String(8), nullable=True),
        _ts("period_start", True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_topups")),
    )


def _sqlite_columns() -> None:
    """ADD COLUMN on SQLite (FK columns carry a named column-level REFERENCES clause)."""
    stmts = [
        "ALTER TABLE packets ADD COLUMN fencing_token INTEGER DEFAULT 0 NOT NULL",
        "ALTER TABLE packets ADD COLUMN active_run_id VARCHAR(36)",
        "ALTER TABLE channel_runs ADD COLUMN generation_run_id VARCHAR(36) CONSTRAINT "
        "fk_channel_runs_generation_run_id_generation_runs REFERENCES generation_runs (id)",
        "ALTER TABLE channel_runs ADD COLUMN eva_packet_id VARCHAR(36) CONSTRAINT "
        "fk_channel_runs_eva_packet_id_eva_packets REFERENCES eva_packets (id)",
        "ALTER TABLE channel_runs ADD COLUMN operation_id VARCHAR(64)",
        "ALTER TABLE channel_runs ADD COLUMN fencing_token INTEGER",
    ]
    for name, sql_type, default in RUN_COLUMNS:
        extra = f" DEFAULT {default} NOT NULL" if default is not None else ""
        if name == "channel":
            extra = (
                " CONSTRAINT ck_generation_runs_channel CHECK ("
                + _check_sql("channel", CHANNELS)
                + ")"
            )
        stmts.append(f"ALTER TABLE generation_runs ADD COLUMN {name} {sql_type}{extra}")
    _run(stmts)


def _pg_columns() -> None:
    types = {
        "VARCHAR(32)": sa.String(32),
        "VARCHAR(64)": sa.String(64),
        "VARCHAR(128)": sa.String(128),
        "INTEGER": sa.Integer(),
        "DATETIME": sa.DateTime(timezone=True),
        "JSON": sa.JSON(),
        "BOOLEAN": sa.Boolean(),
        "TEXT": sa.Text(),
    }
    op.add_column(
        "packets", sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("packets", sa.Column("active_run_id", sa.String(36), nullable=True))
    for name in ("generation_run_id", "eva_packet_id"):
        op.add_column("channel_runs", sa.Column(name, sa.String(36), nullable=True))
    op.create_foreign_key(
        op.f("fk_channel_runs_generation_run_id_generation_runs"),
        "channel_runs",
        "generation_runs",
        ["generation_run_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_channel_runs_eva_packet_id_eva_packets"),
        "channel_runs",
        "eva_packets",
        ["eva_packet_id"],
        ["id"],
    )
    op.add_column("channel_runs", sa.Column("operation_id", sa.String(64), nullable=True))
    op.add_column("channel_runs", sa.Column("fencing_token", sa.Integer(), nullable=True))
    for name, sql_type, default in RUN_COLUMNS:
        server_default = None
        if default is not None:
            server_default = sa.false() if sql_type == "BOOLEAN" else default
        op.add_column(
            "generation_runs",
            sa.Column(
                name,
                types[sql_type],
                nullable=default is None,
                server_default=server_default,
            ),
        )
    op.create_check_constraint(
        op.f("ck_generation_runs_channel"), "generation_runs", _check_sql("channel", CHANNELS)
    )
    op.drop_constraint(
        op.f("ck_generation_runs_generationrunstate"), "generation_runs", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_generation_runs_generationrunstate"),
        "generation_runs",
        _check_sql("state", RUN_STATES_0007),
    )


def upgrade() -> None:
    sqlite = _is_sqlite()
    dialect = "sqlite" if sqlite else "postgresql"
    if sqlite:
        _sqlite_edit_schema(
            "generation_runs",
            _replace(_check_sql("state", RUN_STATES_0005), _check_sql("state", RUN_STATES_0007)),
        )
        _sqlite_columns()
    else:
        _pg_columns()
    for column in ("generation_run_id", "eva_packet_id"):
        op.create_index(f"ix_channel_runs_{column}", "channel_runs", [column])
    op.create_index("uq_channel_runs_operation_id", "channel_runs", ["operation_id"], unique=True)
    _create_tables()
    _run(TRIGGERS[dialect])
    _run(list(VIEWS.values()))


def downgrade() -> None:
    sqlite = _is_sqlite()
    dialect = "sqlite" if sqlite else "postgresql"
    _refuse_if(
        "SELECT COUNT(*) FROM generation_runs WHERE "
        + "NOT "
        + _check_sql("state", RUN_STATES_0005),
        "generation runs use M3 states; they cannot be represented before 0007",
    )
    _run([f"DROP VIEW IF EXISTS {name}" for name in VIEWS])
    if sqlite:
        _run([f"DROP TRIGGER IF EXISTS {name}" for name, _ in _trigger_names(TRIGGERS[dialect])])
    else:
        drops = [
            f"DROP TRIGGER IF EXISTS {name} ON {table}"
            for name, table in _trigger_names(TRIGGERS[dialect])
        ]
        drops += [f"DROP FUNCTION IF EXISTS {fn}()" for fn in PG_FUNCTIONS]
        _run(drops)
    for table in NEW_TABLES:
        op.drop_table(table)
    op.drop_index("uq_channel_runs_operation_id", table_name="channel_runs")
    for column in ("generation_run_id", "eva_packet_id"):
        op.drop_index(f"ix_channel_runs_{column}", table_name="channel_runs")
    if sqlite:
        drops = [f"ALTER TABLE generation_runs DROP COLUMN {n}" for n, _, _ in RUN_COLUMNS]
        drops += [
            f"ALTER TABLE channel_runs DROP COLUMN {n}"
            for n in ("generation_run_id", "eva_packet_id", "operation_id", "fencing_token")
        ]
        drops += [
            f"ALTER TABLE packets DROP COLUMN {n}" for n in ("fencing_token", "active_run_id")
        ]
        _run(drops)
        _sqlite_edit_schema(
            "generation_runs",
            _replace(_check_sql("state", RUN_STATES_0007), _check_sql("state", RUN_STATES_0005)),
        )
    else:
        op.drop_constraint(op.f("ck_generation_runs_channel"), "generation_runs", type_="check")
        for name, _, _ in RUN_COLUMNS:
            op.drop_column("generation_runs", name)
        op.drop_constraint(
            op.f("fk_channel_runs_generation_run_id_generation_runs"),
            "channel_runs",
            type_="foreignkey",
        )
        op.drop_constraint(
            op.f("fk_channel_runs_eva_packet_id_eva_packets"), "channel_runs", type_="foreignkey"
        )
        for name in ("generation_run_id", "eva_packet_id", "operation_id", "fencing_token"):
            op.drop_column("channel_runs", name)
        for name in ("fencing_token", "active_run_id"):
            op.drop_column("packets", name)
        op.drop_constraint(
            op.f("ck_generation_runs_generationrunstate"), "generation_runs", type_="check"
        )
        op.create_check_constraint(
            op.f("ck_generation_runs_generationrunstate"),
            "generation_runs",
            _check_sql("state", RUN_STATES_0005),
        )


# --------------------------------------------------------------------------- frozen DDL
# Generated from orchestrator.db.ddl.m3_statements at authoring time.

TRIGGERS: dict[str, list[str]] = {
    "sqlite": [
        """CREATE TRIGGER trg_packets_transition_table BEFORE UPDATE ON packets FOR EACH ROW WHEN NEW.status IS NOT OLD.status AND (OLD.status || '>' || NEW.status) NOT IN ('awaiting_confirmation>blocked', 'awaiting_confirmation>closed_unverified', 'awaiting_confirmation>completed', 'awaiting_confirmation>evaluating_post', 'awaiting_confirmation>evaluating_pre', 'awaiting_confirmation>held', 'awaiting_confirmation>queued', 'awaiting_confirmation>running', 'awaiting_confirmation>waiting_for_children', 'blocked>closed_unverified', 'blocked>evaluating_post', 'blocked>evaluating_pre', 'blocked>held', 'draft>blocked', 'draft>closed_unverified', 'draft>evaluating_pre', 'draft>held', 'evaluating>closed_unverified', 'evaluating>evaluating_pre', 'evaluating_post>awaiting_confirmation', 'evaluating_post>blocked', 'evaluating_post>closed_unverified', 'evaluating_post>completed', 'evaluating_post>held', 'evaluating_post>needs_review', 'evaluating_pre>awaiting_confirmation', 'evaluating_pre>blocked', 'evaluating_pre>closed_unverified', 'evaluating_pre>held', 'evaluating_pre>needs_review', 'evaluating_pre>ready', 'evaluating_pre>waiting_for_children', 'failed>awaiting_confirmation', 'failed>blocked', 'failed>closed_unverified', 'failed>evaluating_pre', 'failed>held', 'failed>queued', 'held>blocked', 'held>closed_unverified', 'held>evaluating_pre', 'in_progress>closed_unverified', 'in_progress>evaluating_pre', 'needs_review>blocked', 'needs_review>closed_unverified', 'needs_review>evaluating_post', 'needs_review>evaluating_pre', 'needs_review>held', 'processed>closed_unverified', 'processed>evaluating_pre', 'queued>awaiting_confirmation', 'queued>blocked', 'queued>closed_unverified', 'queued>evaluating_pre', 'queued>held', 'queued>running', 'ready>awaiting_confirmation', 'ready>blocked', 'ready>closed_unverified', 'ready>evaluating_pre', 'ready>held', 'ready>queued', 'ready>waiting_for_children', 'running>awaiting_confirmation', 'running>blocked', 'running>closed_unverified', 'running>evaluating_post', 'running>evaluating_pre', 'running>failed', 'waiting_for_children>closed_unverified', 'waiting_for_children>completed') BEGIN SELECT RAISE(ABORT, 'orchestrator-transition: packet status change is not in the M3 transition table'); END""",
        """CREATE TRIGGER trg_generation_runs_transition_table BEFORE UPDATE ON generation_runs FOR EACH ROW WHEN NEW.state IS NOT OLD.state AND (OLD.state || '>' || NEW.state) NOT IN ('accepted>stale', 'awaiting_confirmation>dispatched', 'awaiting_confirmation>failed', 'awaiting_confirmation>stale', 'awaiting_result>awaiting_confirmation', 'awaiting_result>failed', 'awaiting_result>stale', 'awaiting_result>validating', 'claimed>stale', 'dispatched>awaiting_result', 'dispatched>failed', 'dispatched>stale', 'planned>dispatched', 'planned>stale', 'validating>accepted', 'validating>awaiting_result', 'validating>failed', 'validating>stale') BEGIN SELECT RAISE(ABORT, 'orchestrator-transition: run state change is not in the M3 transition table'); END""",
        """CREATE TRIGGER trg_generation_runs_final BEFORE UPDATE ON generation_runs FOR EACH ROW WHEN OLD.state IN ('failed', 'stale', 'succeeded', 'abandoned') OR (OLD.state = 'accepted' AND OLD.commit_sha IS NOT NULL) BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: a finished generation run cannot be changed'); END""",
        """CREATE TRIGGER trg_generation_runs_bindings_frozen BEFORE UPDATE ON generation_runs FOR EACH ROW WHEN NEW.claimed_by IS NOT OLD.claimed_by OR NEW.channel IS NOT OLD.channel OR NEW.fencing_token IS NOT OLD.fencing_token OR NEW.created_at IS NOT OLD.created_at OR (OLD.manifest_hash IS NOT NULL AND NEW.manifest_hash IS NOT OLD.manifest_hash) OR (OLD.commit_sha IS NOT NULL AND NEW.commit_sha IS NOT OLD.commit_sha) OR (NEW.gate_revision IS NOT OLD.gate_revision AND (OLD.gate_revision IS NULL OR NEW.gate_revision IS NOT OLD.gate_revision + 1)) BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: generation run bindings are frozen (gate revision moves by +1 only)'); END""",
        """CREATE TRIGGER trg_kernel_leases_epoch BEFORE UPDATE ON kernel_leases FOR EACH ROW WHEN NEW.epoch < OLD.epoch OR NEW.name IS NOT OLD.name BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: the kernel lease epoch never decreases'); END""",
        """CREATE TRIGGER trg_governor_states_revision BEFORE UPDATE ON governor_states FOR EACH ROW WHEN NEW.revision <= OLD.revision OR NEW.scope_type IS NOT OLD.scope_type OR NEW.scope_id IS NOT OLD.scope_id BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: a governor state change must increase its revision and keep its scope'); END""",
        """CREATE TRIGGER trg_channel_runs_append_only_update BEFORE UPDATE ON channel_runs FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: channel_runs is append-only (update rejected)'); END""",
        """CREATE TRIGGER trg_channel_runs_append_only_delete BEFORE DELETE ON channel_runs FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: channel_runs is append-only (delete rejected)'); END""",
        """CREATE TRIGGER trg_channel_run_outcomes_append_only_update BEFORE UPDATE ON channel_run_outcomes FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: channel_run_outcomes is append-only (update rejected)'); END""",
        """CREATE TRIGGER trg_channel_run_outcomes_append_only_delete BEFORE DELETE ON channel_run_outcomes FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: channel_run_outcomes is append-only (delete rejected)'); END""",
        """CREATE TRIGGER trg_governor_events_append_only_update BEFORE UPDATE ON governor_events FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: governor_events is append-only (update rejected)'); END""",
        """CREATE TRIGGER trg_governor_events_append_only_delete BEFORE DELETE ON governor_events FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: governor_events is append-only (delete rejected)'); END""",
        """CREATE TRIGGER trg_budget_reservations_append_only_update BEFORE UPDATE ON budget_reservations FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: budget_reservations is append-only (update rejected)'); END""",
        """CREATE TRIGGER trg_budget_reservations_append_only_delete BEFORE DELETE ON budget_reservations FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: budget_reservations is append-only (delete rejected)'); END""",
        """CREATE TRIGGER trg_budget_reconciliations_append_only_update BEFORE UPDATE ON budget_reconciliations FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: budget_reconciliations is append-only (update rejected)'); END""",
        """CREATE TRIGGER trg_budget_reconciliations_append_only_delete BEFORE DELETE ON budget_reconciliations FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: budget_reconciliations is append-only (delete rejected)'); END""",
        """CREATE TRIGGER trg_budget_topups_append_only_update BEFORE UPDATE ON budget_topups FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: budget_topups is append-only (update rejected)'); END""",
        """CREATE TRIGGER trg_budget_topups_append_only_delete BEFORE DELETE ON budget_topups FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: budget_topups is append-only (delete rejected)'); END""",
        """CREATE TRIGGER trg_kernel_leases_no_delete BEFORE DELETE ON kernel_leases FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: kernel_leases rows cannot be deleted'); END""",
        """CREATE TRIGGER trg_governor_states_no_delete BEFORE DELETE ON governor_states FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: governor_states rows cannot be deleted'); END""",
        """CREATE TRIGGER trg_budget_counters_no_delete BEFORE DELETE ON budget_counters FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: budget_counters rows cannot be deleted'); END""",
    ],
    "postgresql": [
        """CREATE OR REPLACE FUNCTION orch_packets_transition_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status
     AND (OLD.status || '>' || NEW.status) NOT IN ('awaiting_confirmation>blocked', 'awaiting_confirmation>closed_unverified', 'awaiting_confirmation>completed', 'awaiting_confirmation>evaluating_post', 'awaiting_confirmation>evaluating_pre', 'awaiting_confirmation>held', 'awaiting_confirmation>queued', 'awaiting_confirmation>running', 'awaiting_confirmation>waiting_for_children', 'blocked>closed_unverified', 'blocked>evaluating_post', 'blocked>evaluating_pre', 'blocked>held', 'draft>blocked', 'draft>closed_unverified', 'draft>evaluating_pre', 'draft>held', 'evaluating>closed_unverified', 'evaluating>evaluating_pre', 'evaluating_post>awaiting_confirmation', 'evaluating_post>blocked', 'evaluating_post>closed_unverified', 'evaluating_post>completed', 'evaluating_post>held', 'evaluating_post>needs_review', 'evaluating_pre>awaiting_confirmation', 'evaluating_pre>blocked', 'evaluating_pre>closed_unverified', 'evaluating_pre>held', 'evaluating_pre>needs_review', 'evaluating_pre>ready', 'evaluating_pre>waiting_for_children', 'failed>awaiting_confirmation', 'failed>blocked', 'failed>closed_unverified', 'failed>evaluating_pre', 'failed>held', 'failed>queued', 'held>blocked', 'held>closed_unverified', 'held>evaluating_pre', 'in_progress>closed_unverified', 'in_progress>evaluating_pre', 'needs_review>blocked', 'needs_review>closed_unverified', 'needs_review>evaluating_post', 'needs_review>evaluating_pre', 'needs_review>held', 'processed>closed_unverified', 'processed>evaluating_pre', 'queued>awaiting_confirmation', 'queued>blocked', 'queued>closed_unverified', 'queued>evaluating_pre', 'queued>held', 'queued>running', 'ready>awaiting_confirmation', 'ready>blocked', 'ready>closed_unverified', 'ready>evaluating_pre', 'ready>held', 'ready>queued', 'ready>waiting_for_children', 'running>awaiting_confirmation', 'running>blocked', 'running>closed_unverified', 'running>evaluating_post', 'running>evaluating_pre', 'running>failed', 'waiting_for_children>closed_unverified', 'waiting_for_children>completed') THEN
    RAISE EXCEPTION 'orchestrator-transition: packet status change is not in the M3 transition table' USING ERRCODE = '23001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
        """CREATE OR REPLACE FUNCTION orch_generation_runs_m3_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF OLD.state IN ('failed', 'stale', 'succeeded', 'abandoned')
     OR (OLD.state = 'accepted' AND OLD.commit_sha IS NOT NULL) THEN
    RAISE EXCEPTION 'orchestrator-immutable: a finished generation run cannot be changed' USING ERRCODE = '23001';
  END IF;
  IF NEW.state IS DISTINCT FROM OLD.state
     AND (OLD.state || '>' || NEW.state) NOT IN ('accepted>stale', 'awaiting_confirmation>dispatched', 'awaiting_confirmation>failed', 'awaiting_confirmation>stale', 'awaiting_result>awaiting_confirmation', 'awaiting_result>failed', 'awaiting_result>stale', 'awaiting_result>validating', 'claimed>stale', 'dispatched>awaiting_result', 'dispatched>failed', 'dispatched>stale', 'planned>dispatched', 'planned>stale', 'validating>accepted', 'validating>awaiting_result', 'validating>failed', 'validating>stale') THEN
    RAISE EXCEPTION 'orchestrator-transition: run state change is not in the M3 transition table' USING ERRCODE = '23001';
  END IF;
  IF NEW.claimed_by IS DISTINCT FROM OLD.claimed_by
     OR NEW.channel IS DISTINCT FROM OLD.channel
     OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
     OR NEW.created_at IS DISTINCT FROM OLD.created_at
     OR (OLD.manifest_hash IS NOT NULL AND NEW.manifest_hash IS DISTINCT FROM OLD.manifest_hash)
     OR (OLD.commit_sha IS NOT NULL AND NEW.commit_sha IS DISTINCT FROM OLD.commit_sha)
     OR (NEW.gate_revision IS DISTINCT FROM OLD.gate_revision
         AND (OLD.gate_revision IS NULL
              OR NEW.gate_revision IS DISTINCT FROM OLD.gate_revision + 1)) THEN
    RAISE EXCEPTION 'orchestrator-immutable: generation run bindings are frozen (gate revision moves by +1 only)' USING ERRCODE = '23001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
        """CREATE OR REPLACE FUNCTION orch_kernel_leases_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'orchestrator-immutable: kernel_leases rows cannot be deleted' USING ERRCODE = '23001';
  END IF;
  IF NEW.epoch < OLD.epoch OR NEW.name IS DISTINCT FROM OLD.name THEN
    RAISE EXCEPTION 'orchestrator-immutable: the kernel lease epoch never decreases' USING ERRCODE = '23001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
        """CREATE OR REPLACE FUNCTION orch_governor_states_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'orchestrator-immutable: governor_states rows cannot be deleted' USING ERRCODE = '23001';
  END IF;
  IF NEW.revision <= OLD.revision OR NEW.scope_type IS DISTINCT FROM OLD.scope_type
     OR NEW.scope_id IS DISTINCT FROM OLD.scope_id THEN
    RAISE EXCEPTION 'orchestrator-immutable: a governor state change must increase its revision and keep its scope' USING ERRCODE = '23001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
        """CREATE OR REPLACE FUNCTION orch_no_delete_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  RAISE EXCEPTION 'orchestrator-immutable: % rows cannot be deleted', TG_TABLE_NAME USING ERRCODE = '23001';
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
        """CREATE TRIGGER trg_packets_transition_table BEFORE UPDATE ON packets FOR EACH ROW EXECUTE FUNCTION orch_packets_transition_guard()""",
        """CREATE TRIGGER trg_generation_runs_m3 BEFORE UPDATE ON generation_runs FOR EACH ROW EXECUTE FUNCTION orch_generation_runs_m3_guard()""",
        """CREATE TRIGGER trg_kernel_leases_guard BEFORE UPDATE OR DELETE ON kernel_leases FOR EACH ROW EXECUTE FUNCTION orch_kernel_leases_guard()""",
        """CREATE TRIGGER trg_governor_states_guard BEFORE UPDATE OR DELETE ON governor_states FOR EACH ROW EXECUTE FUNCTION orch_governor_states_guard()""",
        """CREATE TRIGGER trg_budget_counters_no_delete BEFORE DELETE ON budget_counters FOR EACH ROW EXECUTE FUNCTION orch_no_delete_guard()""",
        """CREATE TRIGGER trg_channel_runs_append_only BEFORE UPDATE OR DELETE ON channel_runs FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_channel_runs_no_truncate BEFORE TRUNCATE ON channel_runs FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_channel_run_outcomes_append_only BEFORE UPDATE OR DELETE ON channel_run_outcomes FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_channel_run_outcomes_no_truncate BEFORE TRUNCATE ON channel_run_outcomes FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_governor_events_append_only BEFORE UPDATE OR DELETE ON governor_events FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_governor_events_no_truncate BEFORE TRUNCATE ON governor_events FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_budget_reservations_append_only BEFORE UPDATE OR DELETE ON budget_reservations FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_budget_reservations_no_truncate BEFORE TRUNCATE ON budget_reservations FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_budget_reconciliations_append_only BEFORE UPDATE OR DELETE ON budget_reconciliations FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_budget_reconciliations_no_truncate BEFORE TRUNCATE ON budget_reconciliations FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_budget_topups_append_only BEFORE UPDATE OR DELETE ON budget_topups FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_budget_topups_no_truncate BEFORE TRUNCATE ON budget_topups FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_kernel_leases_no_truncate BEFORE TRUNCATE ON kernel_leases FOR EACH STATEMENT EXECUTE FUNCTION orch_no_delete_guard()""",
        """CREATE TRIGGER trg_governor_states_no_truncate BEFORE TRUNCATE ON governor_states FOR EACH STATEMENT EXECUTE FUNCTION orch_no_delete_guard()""",
        """CREATE TRIGGER trg_budget_counters_no_truncate BEFORE TRUNCATE ON budget_counters FOR EACH STATEMENT EXECUTE FUNCTION orch_no_delete_guard()""",
    ],
}

VIEWS: dict[str, str] = {
    "v_run_attempts": """CREATE VIEW v_run_attempts AS
SELECT r.id AS generation_run_id, r.packet_id, r.packet_version, r.state, r.channel,
       r.fencing_token, r.attempts, r.max_attempts, c.id AS channel_run_id, c.run_id,
       c.attempt, c.operation_id, c.created_at AS authorised_at, o.outcome,
       o.exit_code, o.error, o.created_at AS outcome_at
FROM generation_runs r
LEFT JOIN channel_runs c ON c.generation_run_id = r.id
  AND NOT EXISTS (SELECT 1 FROM channel_runs c2
                  WHERE c2.generation_run_id = r.id AND c2.attempt > c.attempt)
LEFT JOIN channel_run_outcomes o ON o.channel_run_id = c.id""",
}
