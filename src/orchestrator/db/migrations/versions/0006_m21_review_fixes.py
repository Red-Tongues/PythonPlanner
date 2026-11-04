"""M2.1: review R2 fixes that need schema (fixes 2–7, 10, 12 of docs/reviews/R2_consensus.md).

* ``packets.gate_revision`` (fix 2) — bumped by every governance write that can change readiness;
  admission and claim compare-and-set it.
* ``packets`` status ``closed_unverified`` (fix 5) — a user closes work without a passing
  evaluation; terminal and locked, never ``completed``. A trigger enforces the lock invariant.
* ``eva_packets.definition_version`` (fix 4) — consensus is current only for the field
  definition version it evaluated.
* ``revocations.target_type`` may be ``dependency`` (fix 6) — ``packet_dependencies`` becomes
  append-only, and changes go through revocations.
* Immutability triggers (fix 6): evaluation-cycle bindings and evaPacket identity columns are
  frozen from insert, cycles cannot be deleted, dependencies are append-only, and PostgreSQL gets
  TRUNCATE guards on ``generation_runs``, ``packet_dependencies``, ``evaluation_cycles`` and
  ``eva_packets``.
* Every view is recreated (fixes 3, 4, 7, 10, 12):
  - integer fixed-point consensus;
  - ruling 5 for disputed rows;
  - exact artifact, preset and definition binding;
  - failure-budget reset after an approved confirmation;
  - revoked dependencies ignored.

SQLite constraint edits use the same documented ``writable_schema`` procedure as 0005 (R2 kept
it: GE8 declined).

FROZEN: trigger and view SQL are literal copies of ``orchestrator.db.ddl.m21_statements`` and
``orchestrator.sql.views.view_statements`` (tests assert equality). Nothing is imported from
the application. Never edit this file after it is applied.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES_0005 = (
    "draft",
    "evaluating_pre",
    "ready",
    "queued",
    "running",
    "evaluating_post",
    "completed",
    "needs_review",
    "held",
    "blocked",
    "failed",
    "awaiting_confirmation",
    "waiting_for_children",
    "archived",
    "evaluating",
    "in_progress",
    "processed",
)
STATUSES_0006 = (*STATUSES_0005, "closed_unverified")
TARGETS_0005 = ("waiver", "override")
TARGETS_0006 = ("waiver", "override", "dependency")
GATE_COLUMN = "\tgate_revision INTEGER DEFAULT 0 NOT NULL, \n"
VIEW_NAMES = (
    "v_eva_progress",
    "v_eva_item_consensus",
    "v_field_consensus",
    "v_field_status",
    "v_packet_stats",
    "v_branch_stats",
    "v_branch_model_usage",
    "v_branch_triggers",
    "v_ready_candidates",
    "v_queue",
)


def _check_sql(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


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
    """SQLite's documented procedure for format-neutral changes (see 0005): rewrite the stored
    CREATE TABLE text under ``writable_schema``, bump ``schema_version`` and verify FKs."""
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
    problems = bind.exec_driver_sql(f"PRAGMA foreign_key_check({table})").all()
    if problems:
        raise RuntimeError(f"foreign key violations in {table}: {problems[:5]}")


def _replace(old: str, new: str) -> Callable[[str], str]:
    def transform(sql: str) -> str:
        if old not in sql:
            raise RuntimeError(f"expected text not found: {old[:60]}")
        return sql.replace(old, new)

    return transform


def _packets_up(sql: str) -> str:
    sql = _replace(_check_sql("status", STATUSES_0005), _check_sql("status", STATUSES_0006))(sql)
    return sql.replace(
        "\tCONSTRAINT pk_packets PRIMARY KEY", GATE_COLUMN + "\tCONSTRAINT pk_packets PRIMARY KEY"
    )


def _packets_down(sql: str) -> str:
    return _replace(_check_sql("status", STATUSES_0006), _check_sql("status", STATUSES_0005))(sql)


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


def upgrade() -> None:
    sqlite = _is_sqlite()
    dialect = "sqlite" if sqlite else "postgresql"
    _run([f"DROP VIEW IF EXISTS {name}" for name in reversed(VIEW_NAMES)])

    if sqlite:
        _sqlite_edit_schema("packets", _packets_up)
        _sqlite_edit_schema(
            "revocations",
            _replace(
                _check_sql("target_type", TARGETS_0005), _check_sql("target_type", TARGETS_0006)
            ),
        )
    else:
        op.add_column(
            "packets",
            sa.Column("gate_revision", sa.Integer(), nullable=False, server_default="0"),
        )
        op.drop_constraint(op.f("ck_packets_packetstatus"), "packets", type_="check")
        op.create_check_constraint(
            op.f("ck_packets_packetstatus"), "packets", _check_sql("status", STATUSES_0006)
        )
        op.drop_constraint(op.f("ck_revocations_target_type"), "revocations", type_="check")
        op.create_check_constraint(
            op.f("ck_revocations_target_type"),
            "revocations",
            _check_sql("target_type", TARGETS_0006),
        )
    op.add_column("eva_packets", sa.Column("definition_version", sa.Integer(), nullable=True))

    _run(TRIGGERS[dialect])
    _run(VIEWS[dialect])


def downgrade() -> None:
    sqlite = _is_sqlite()
    dialect = "sqlite" if sqlite else "postgresql"
    _refuse_if(
        "SELECT COUNT(*) FROM packets WHERE status = 'closed_unverified'",
        "packets are closed_unverified; they cannot be represented before 0006",
    )
    _refuse_if(
        "SELECT COUNT(*) FROM revocations WHERE target_type = 'dependency'",
        "dependency revocations exist; they cannot be represented before 0006",
    )
    _run([f"DROP VIEW IF EXISTS {name}" for name in reversed(VIEW_NAMES)])
    if sqlite:
        _run([f"DROP TRIGGER IF EXISTS {name}" for name, _ in _trigger_names(TRIGGERS[dialect])])
    else:
        drops = [
            f"DROP TRIGGER IF EXISTS {name} ON {table}"
            for name, table in _trigger_names(TRIGGERS[dialect])
        ]
        drops += [
            f"DROP FUNCTION IF EXISTS {fn}()"
            for fn in (
                "orch_packets_closed_guard",
                "orch_evaluation_cycles_guard",
                "orch_eva_packets_identity_guard",
            )
        ]
        _run(drops)

    if sqlite:
        _run(["ALTER TABLE eva_packets DROP COLUMN definition_version"])
        _sqlite_edit_schema("packets", _packets_down)
        _run(["ALTER TABLE packets DROP COLUMN gate_revision"])
        _sqlite_edit_schema(
            "revocations",
            _replace(
                _check_sql("target_type", TARGETS_0006), _check_sql("target_type", TARGETS_0005)
            ),
        )
    else:
        op.drop_column("eva_packets", "definition_version")
        op.drop_constraint(op.f("ck_packets_packetstatus"), "packets", type_="check")
        op.create_check_constraint(
            op.f("ck_packets_packetstatus"), "packets", _check_sql("status", STATUSES_0005)
        )
        op.drop_column("packets", "gate_revision")
        op.drop_constraint(op.f("ck_revocations_target_type"), "revocations", type_="check")
        op.create_check_constraint(
            op.f("ck_revocations_target_type"),
            "revocations",
            _check_sql("target_type", TARGETS_0005),
        )
    # restore 0005's views from its own frozen revision module
    previous = op.get_context().script.get_revision("0005").module  # type: ignore[union-attr]
    _run(previous.VIEWS[dialect])


# --------------------------------------------------------------------------- frozen DDL
# Generated from orchestrator.db.ddl.m21_statements and orchestrator.sql.views at authoring time.

TRIGGERS: dict[str, list[str]] = {
    "sqlite": [
        """CREATE TRIGGER trg_packets_closed_unverified_locks_insert BEFORE INSERT ON packets FOR EACH ROW WHEN NEW.status = 'closed_unverified' AND (NEW.locked IS NOT 1 OR NEW.completed_at IS NULL) BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: closed_unverified packets must be locked with completed_at set'); END""",
        """CREATE TRIGGER trg_packets_closed_unverified_locks_update BEFORE UPDATE ON packets FOR EACH ROW WHEN NEW.status = 'closed_unverified' AND (NEW.locked IS NOT 1 OR NEW.completed_at IS NULL) BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: closed_unverified packets must be locked with completed_at set'); END""",
        """CREATE TRIGGER trg_evaluation_cycles_bindings_frozen BEFORE UPDATE ON evaluation_cycles FOR EACH ROW WHEN NEW.packet_id IS NOT OLD.packet_id OR NEW.packet_version IS NOT OLD.packet_version OR NEW.phase IS NOT OLD.phase OR NEW.target_hash IS NOT OLD.target_hash OR NEW.project_id IS NOT OLD.project_id OR NEW.project_version IS NOT OLD.project_version OR NEW.objective_id IS NOT OLD.objective_id OR NEW.objective_version IS NOT OLD.objective_version OR NEW.registry_revision_id IS NOT OLD.registry_revision_id OR NEW.pass_plan IS NOT OLD.pass_plan OR NEW.models IS NOT OLD.models OR NEW.created_at IS NOT OLD.created_at BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: evaluation cycle bindings are frozen after insert'); END""",
        """CREATE TRIGGER trg_evaluation_cycles_no_delete BEFORE DELETE ON evaluation_cycles FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: evaluation cycles cannot be deleted'); END""",
        """CREATE TRIGGER trg_eva_packets_identity_frozen BEFORE UPDATE ON eva_packets FOR EACH ROW WHEN NEW.packet_id IS NOT OLD.packet_id OR NEW.packet_version IS NOT OLD.packet_version OR NEW.cycle_id IS NOT OLD.cycle_id OR NEW.preset_id IS NOT OLD.preset_id OR NEW.field_name IS NOT OLD.field_name OR NEW.definition_version IS NOT OLD.definition_version OR NEW.phase IS NOT OLD.phase OR NEW.plan IS NOT OLD.plan OR NEW.item_keys IS NOT OLD.item_keys OR NEW.target_hash IS NOT OLD.target_hash OR NEW.model_config IS NOT OLD.model_config OR NEW.model_config_hash IS NOT OLD.model_config_hash OR NEW.content_address IS NOT OLD.content_address OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.escalation IS NOT OLD.escalation OR NEW.escalated_from_id IS NOT OLD.escalated_from_id OR NEW.reused_from_id IS NOT OLD.reused_from_id OR NEW.created_at IS NOT OLD.created_at BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: evaPacket identity columns are frozen from insert'); END""",
        """CREATE TRIGGER trg_packet_dependencies_append_only_update BEFORE UPDATE ON packet_dependencies FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: packet_dependencies is append-only (update rejected)'); END""",
        """CREATE TRIGGER trg_packet_dependencies_append_only_delete BEFORE DELETE ON packet_dependencies FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: packet_dependencies is append-only (delete rejected)'); END""",
    ],
    "postgresql": [
        """CREATE OR REPLACE FUNCTION orch_packets_closed_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF NEW.status = 'closed_unverified'
     AND (NOT NEW.locked OR NEW.completed_at IS NULL) THEN
    RAISE EXCEPTION 'orchestrator-immutable: closed_unverified packets must be locked with completed_at set' USING ERRCODE = '23001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
        """CREATE OR REPLACE FUNCTION orch_evaluation_cycles_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'orchestrator-immutable: evaluation cycles cannot be deleted' USING ERRCODE = '23001';
  END IF;
  IF NEW.packet_id IS DISTINCT FROM OLD.packet_id
     OR NEW.packet_version IS DISTINCT FROM OLD.packet_version
     OR NEW.phase IS DISTINCT FROM OLD.phase
     OR NEW.target_hash IS DISTINCT FROM OLD.target_hash
     OR NEW.project_id IS DISTINCT FROM OLD.project_id
     OR NEW.project_version IS DISTINCT FROM OLD.project_version
     OR NEW.objective_id IS DISTINCT FROM OLD.objective_id
     OR NEW.objective_version IS DISTINCT FROM OLD.objective_version
     OR NEW.registry_revision_id IS DISTINCT FROM OLD.registry_revision_id
     OR NEW.pass_plan IS DISTINCT FROM OLD.pass_plan
     OR NEW.models::text IS DISTINCT FROM OLD.models::text
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'orchestrator-immutable: evaluation cycle bindings are frozen after insert' USING ERRCODE = '23001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
        """CREATE OR REPLACE FUNCTION orch_eva_packets_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF NEW.packet_id IS DISTINCT FROM OLD.packet_id
     OR NEW.packet_version IS DISTINCT FROM OLD.packet_version
     OR NEW.cycle_id IS DISTINCT FROM OLD.cycle_id
     OR NEW.preset_id IS DISTINCT FROM OLD.preset_id
     OR NEW.field_name IS DISTINCT FROM OLD.field_name
     OR NEW.definition_version IS DISTINCT FROM OLD.definition_version
     OR NEW.phase IS DISTINCT FROM OLD.phase
     OR NEW.plan IS DISTINCT FROM OLD.plan
     OR NEW.item_keys::text IS DISTINCT FROM OLD.item_keys::text
     OR NEW.target_hash IS DISTINCT FROM OLD.target_hash
     OR NEW.model_config::text IS DISTINCT FROM OLD.model_config::text
     OR NEW.model_config_hash IS DISTINCT FROM OLD.model_config_hash
     OR NEW.content_address IS DISTINCT FROM OLD.content_address
     OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
     OR NEW.escalation IS DISTINCT FROM OLD.escalation
     OR NEW.escalated_from_id IS DISTINCT FROM OLD.escalated_from_id
     OR NEW.reused_from_id IS DISTINCT FROM OLD.reused_from_id
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'orchestrator-immutable: evaPacket identity columns are frozen from insert' USING ERRCODE = '23001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
        """CREATE TRIGGER trg_packets_closed_unverified BEFORE INSERT OR UPDATE ON packets FOR EACH ROW EXECUTE FUNCTION orch_packets_closed_guard()""",
        """CREATE TRIGGER trg_evaluation_cycles_guard BEFORE UPDATE OR DELETE ON evaluation_cycles FOR EACH ROW EXECUTE FUNCTION orch_evaluation_cycles_guard()""",
        """CREATE TRIGGER trg_eva_packets_identity BEFORE UPDATE ON eva_packets FOR EACH ROW EXECUTE FUNCTION orch_eva_packets_identity_guard()""",
        """CREATE TRIGGER trg_packet_dependencies_append_only BEFORE UPDATE OR DELETE ON packet_dependencies FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_generation_runs_no_truncate BEFORE TRUNCATE ON generation_runs FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_packet_dependencies_no_truncate BEFORE TRUNCATE ON packet_dependencies FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_evaluation_cycles_no_truncate BEFORE TRUNCATE ON evaluation_cycles FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
        """CREATE TRIGGER trg_eva_packets_no_truncate BEFORE TRUNCATE ON eva_packets FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
    ],
}

VIEWS: dict[str, list[str]] = {
    "sqlite": [
        """CREATE VIEW v_eva_progress AS
WITH base AS (
  SELECT e.id AS eva_packet_id, e.packet_id, e.field_name, e.phase, e.state, e.created_at,
         CASE WHEN d.kind = 'text' THEN 1 ELSE 0 END AS is_text,
  MAX(CASE WHEN ep.pass_no = 1 THEN 1 ELSE 0 END) AS done1,
  MAX(CASE WHEN ep.pass_no = 2 THEN 1 ELSE 0 END) AS done2,
  MAX(CASE WHEN ep.pass_no = 3 THEN 1 ELSE 0 END) AS done3,
  MAX(CASE WHEN ep.pass_no = 4 THEN 1 ELSE 0 END) AS done4,
  MAX(CASE WHEN ep.pass_no = 5 THEN 1 ELSE 0 END) AS done5,
  MAX(CASE WHEN ep.pass_no = 6 THEN 1 ELSE 0 END) AS done6,
  CASE WHEN EXISTS (SELECT 1 FROM evaluation_pass_items i WHERE i.eva_packet_id = e.id
                     AND i.pass_no = 1) THEN 1 ELSE 0 END AS a_present,
  CASE WHEN EXISTS (SELECT 1 FROM evaluation_pass_items i WHERE i.eva_packet_id = e.id
                     AND i.pass_no = 2) THEN 1 ELSE 0 END AS b_present
  FROM eva_packets e
  JOIN candidate_field_definitions d ON d.name = e.field_name
  LEFT JOIN evaluation_passes ep ON ep.eva_packet_id = e.id
  GROUP BY e.id, e.packet_id, e.field_name, e.phase, e.state, e.created_at, d.kind
)
SELECT base.*,
  CASE WHEN done1 = 1 AND done2 = 1
        AND (a_present = 0 OR (done4 = 1 AND (is_text = 1 OR done5 = 1)))
        AND (b_present = 0 OR (done3 = 1 AND (is_text = 1 OR done6 = 1)))
       THEN 1 ELSE 0 END AS complete
FROM base""",
        """CREATE VIEW v_eva_item_consensus AS
WITH x AS (
  SELECT e.id AS eva_packet_id, e.packet_id, e.packet_version, e.cycle_id AS eval_cycle_id,
         e.field_name, e.phase, e.created_at, d.kind,
         CASE WHEN d.kind = 'text' THEN 1 ELSE 0 END AS is_text,
         k.value AS item_key, pr.complete,
         i1.value_norm AS va, i2.value_norm AS vb,
         i3.score AS s3, i4.score AS s4, i5.score AS s5, i6.score AS s6,
         COALESCE(i3.supports, 0) AS sup3, COALESCE(i4.supports, 0) AS sup4,
         COALESCE(i5.supports, 0) AS sup5, COALESCE(i6.supports, 0) AS sup6,
         COALESCE(i3.contradictions, 0) AS con3, COALESCE(i4.contradictions, 0) AS con4,
         COALESCE(i5.contradictions, 0) AS con5, COALESCE(i6.contradictions, 0) AS con6,
         COALESCE(i3.unverified, 0) AS unv3, COALESCE(i4.unverified, 0) AS unv4,
         COALESCE(i5.unverified, 0) AS unv5, COALESCE(i6.unverified, 0) AS unv6
  FROM eva_packets e
  JOIN candidate_field_definitions d ON d.name = e.field_name
  JOIN v_eva_progress pr ON pr.eva_packet_id = e.id
  CROSS JOIN json_each(e.item_keys) AS k
  LEFT JOIN evaluation_pass_items i1 ON i1.eva_packet_id = e.id AND i1.pass_no = 1 AND i1.item_key = k.value
  LEFT JOIN evaluation_pass_items i2 ON i2.eva_packet_id = e.id AND i2.pass_no = 2 AND i2.item_key = k.value
  LEFT JOIN evaluation_pass_items i3 ON i3.eva_packet_id = e.id AND i3.pass_no = 3 AND i3.item_key = k.value
  LEFT JOIN evaluation_pass_items i4 ON i4.eva_packet_id = e.id AND i4.pass_no = 4 AND i4.item_key = k.value
  LEFT JOIN evaluation_pass_items i5 ON i5.eva_packet_id = e.id AND i5.pass_no = 5 AND i5.item_key = k.value
  LEFT JOIN evaluation_pass_items i6 ON i6.eva_packet_id = e.id AND i6.pass_no = 6 AND i6.item_key = k.value
),
c AS (
  SELECT x.*, CASE WHEN x.va IS NULL OR x.s4 IS NULL THEN NULL WHEN x.is_text = 1 THEN x.s4 * 1000 WHEN x.s5 IS NULL THEN NULL ELSE (CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.cross_weight'), 0.6) * 10000) AS INTEGER) * x.s4 + CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.self_weight'), 0.4) * 10000) AS INTEGER) * x.s5 + 5) / 10 END AS ca, CASE WHEN x.vb IS NULL OR x.s3 IS NULL THEN NULL WHEN x.is_text = 1 THEN x.s3 * 1000 WHEN x.s6 IS NULL THEN NULL ELSE (CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.cross_weight'), 0.6) * 10000) AS INTEGER) * x.s3 + CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.self_weight'), 0.4) * 10000) AS INTEGER) * x.s6 + 5) / 10 END AS cb FROM x
),
w AS (
  SELECT c.*,
    CASE WHEN c.va IS NULL AND c.vb IS NULL THEN 'both_null'
         WHEN c.va IS NULL THEN 'b_only'
         WHEN c.vb IS NULL THEN 'a_only'
         WHEN c.va = c.vb THEN 'agree'
         ELSE 'disagree' END AS shape,
    CASE WHEN c.va IS NULL AND c.vb IS NULL THEN NULL
         WHEN c.va IS NULL THEN 'b'
         WHEN c.vb IS NULL THEN 'a'
         WHEN c.va = c.vb THEN 'both'
         WHEN c.ca > c.cb THEN 'a'
         WHEN c.cb > c.ca THEN 'b'
         ELSE NULL END AS winner
  FROM c
),
v AS (
  SELECT w.*,
    CASE w.winner WHEN 'a' THEN w.sup4 + w.sup5 WHEN 'b' THEN w.sup3 + w.sup6
         WHEN 'both' THEN w.sup3 + w.sup4 + w.sup5 + w.sup6 ELSE 0 END AS supports,
    CASE w.winner WHEN 'a' THEN w.con4 + w.con5 WHEN 'b' THEN w.con3 + w.con6
         WHEN 'both' THEN w.con3 + w.con4 + w.con5 + w.con6 ELSE 0 END AS contradictions,
    CASE w.winner WHEN 'a' THEN w.unv4 + w.unv5 WHEN 'b' THEN w.unv3 + w.unv6
         WHEN 'both' THEN w.unv3 + w.unv4 + w.unv5 + w.unv6 ELSE 0 END AS unverified,
    CASE WHEN w.shape = 'agree' THEN (w.ca + w.cb + 1) / 2
         WHEN w.shape = 'a_only' THEN w.ca
         WHEN w.shape = 'b_only' THEN w.cb
         WHEN w.shape = 'disagree' THEN CASE WHEN w.ca >= w.cb THEN w.ca ELSE w.cb END
         ELSE NULL END AS decision_e4,
    CASE WHEN w.shape = 'disagree' THEN
           CASE WHEN w.ca >= w.cb THEN w.ca - w.cb ELSE w.cb - w.ca END
         ELSE NULL END AS margin_e4
  FROM w
),
s AS (
  SELECT v.*,
    CASE WHEN v.complete = 0 THEN 'pending'
         WHEN v.shape = 'both_null' THEN 'null'
         WHEN v.supports >= COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'consensus.min_supports'), 1) AND v.contradictions <= v.supports AND (
              (v.shape = 'agree' AND v.decision_e4 >= CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'consensus.agreement_min'), 0.60) * 10000) AS INTEGER))
           OR (v.shape IN ('a_only', 'b_only') AND v.decision_e4 >= CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'consensus.one_sided_min'), 0.70) * 10000) AS INTEGER))
           OR (v.shape = 'disagree' AND v.winner IS NOT NULL
               AND v.decision_e4 >= CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'consensus.winner_min'), 0.70) * 10000) AS INTEGER) AND v.margin_e4 >= CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.dispute_margin'), 0.15) * 10000) AS INTEGER)))
         THEN 'accepted'
         ELSE 'disputed' END AS status
  FROM v
),
r AS (
  SELECT s.*,
    CASE WHEN s.status = 'pending' THEN NULL
         WHEN s.status = 'null' THEN 0
         WHEN s.status = 'accepted' AND s.shape = 'agree' THEN s.decision_e4
         ELSE CASE WHEN s.ca IS NULL THEN s.cb WHEN s.cb IS NULL THEN s.ca WHEN s.ca >= s.cb THEN s.ca ELSE s.cb END END AS confidence_e4
  FROM s
)
SELECT r.eva_packet_id, r.packet_id, r.packet_version, r.eval_cycle_id, r.field_name, r.phase,
       r.kind, r.item_key, r.complete, r.created_at,
       r.va AS value_a, r.vb AS value_b,
       r.ca AS conf_a_e4, r.cb AS conf_b_e4, r.confidence_e4,
       r.ca / 10000.0 AS conf_a, r.cb / 10000.0 AS conf_b,
       r.confidence_e4 / 10000.0 AS confidence,
       CASE WHEN r.shape = 'agree' THEN 1 ELSE 0 END AS agreement,
       r.status,
       CASE WHEN r.complete = 0 THEN 0 ELSE r.supports END AS supports,
       CASE WHEN r.complete = 0 THEN 0 ELSE r.contradictions END AS contradictions,
       CASE WHEN r.complete = 0 THEN 0 ELSE r.unverified END AS unverified,
       CASE WHEN r.complete = 1 AND r.winner = 'a' THEN r.va
            WHEN r.complete = 1 AND r.winner IN ('b', 'both') THEN r.vb
            ELSE NULL END AS winning_value
FROM r""",
        """CREATE VIEW v_field_consensus AS
WITH art AS (
  SELECT a.packet_id, a.sha256,
         ROW_NUMBER() OVER (PARTITION BY a.packet_id ORDER BY a.created_at DESC, a.id DESC)
           AS rn
  FROM artifacts a
),
bound AS (
  SELECT e.id, e.packet_id, e.field_name, e.phase, e.escalation, e.created_at, pr.complete,
         CASE WHEN ((e.phase = 'pre' AND e.target_hash = p.input_hash)
                    OR (e.phase = 'post' AND e.target_hash = la.sha256))
                   AND ps.enabled
                   AND ps.revision = (SELECT MAX(p2.revision) FROM eva_presets p2
                                      WHERE p2.field_name = e.field_name
                                        AND p2.phase = e.phase)
                   AND d.enabled
                   AND e.definition_version = d.version
              THEN 1 ELSE 0 END AS is_current
  FROM eva_packets e
  JOIN packets p ON p.id = e.packet_id
  JOIN v_eva_progress pr ON pr.eva_packet_id = e.id
  JOIN eva_presets ps ON ps.id = e.preset_id
  JOIN candidate_field_definitions d ON d.name = e.field_name
  LEFT JOIN art la ON la.packet_id = p.id AND la.rn = 1
  WHERE e.packet_version = p.version
    AND e.state <> 'superseded'
    AND NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = p.id)
),
cand AS (
  SELECT bound.*,
         ROW_NUMBER() OVER (PARTITION BY bound.packet_id, bound.field_name, bound.phase
                            ORDER BY bound.is_current DESC, bound.complete DESC,
                                     bound.escalation DESC, bound.created_at DESC,
                                     bound.id DESC) AS rn
  FROM bound
)
SELECT p.id AS packet_id, p.ref AS packet_ref, p.version AS packet_version,
       c.field_name, c.phase, c.item_key, c.kind,
       c.value_a, c.value_b,
       CASE WHEN cand.is_current = 1 THEN c.conf_a ELSE NULL END AS conf_a,
       CASE WHEN cand.is_current = 1 THEN c.conf_b ELSE NULL END AS conf_b,
       CASE WHEN cand.is_current = 1 THEN c.confidence ELSE NULL END AS confidence,
       CASE WHEN cand.is_current = 1 THEN c.confidence_e4 ELSE NULL END AS confidence_e4,
       c.agreement,
       CASE WHEN cand.is_current = 1 THEN c.status ELSE 'pending' END AS status,
       CASE WHEN cand.is_current = 1 AND c.status = 'accepted' THEN c.winning_value
            ELSE NULL END AS final_value,
       c.supports, c.contradictions, c.unverified, c.eva_packet_id, c.eval_cycle_id,
       cand.is_current
FROM cand
JOIN packets p ON p.id = cand.packet_id
JOIN v_eva_item_consensus c ON c.eva_packet_id = cand.id
WHERE cand.rn = 1""",
        """CREATE VIEW v_field_status AS
SELECT c.packet_id, c.packet_ref, c.packet_version, c.field_name, c.phase, c.kind,
       CASE WHEN SUM(CASE WHEN c.status = 'pending' THEN 1 ELSE 0 END) > 0 THEN 'pending'
            WHEN SUM(CASE WHEN c.status = 'disputed' THEN 1 ELSE 0 END) > 0 THEN 'disputed'
            WHEN SUM(CASE WHEN c.status = 'accepted' THEN 1 ELSE 0 END) = 0 THEN 'null'
            ELSE 'accepted' END AS status,
       COUNT(*) AS items,
       SUM(CASE WHEN c.status = 'accepted' THEN 1 ELSE 0 END) AS accepted_items,
       SUM(CASE WHEN c.status = 'disputed' THEN 1 ELSE 0 END) AS disputed_items,
       MIN(c.confidence) AS min_confidence,
       MAX(c.eva_packet_id) AS eva_packet_id
FROM v_field_consensus c
GROUP BY c.packet_id, c.packet_ref, c.packet_version, c.field_name, c.phase, c.kind""",
        """CREATE VIEW v_packet_stats AS
SELECT p.id AS packet_id, p.ref AS packet_ref, p.version AS packet_version,
       CASE WHEN TRIM(REPLACE(p.prompt_description, char(10), ' ')) = '' THEN 0
            ELSE LENGTH(TRIM(REPLACE(p.prompt_description, char(10), ' '))) - LENGTH(REPLACE(TRIM(REPLACE(p.prompt_description, char(10), ' ')), ' ', '')) + 1 END
         AS prompt_word_count,
       (SELECT COUNT(*) FROM eva_packets e WHERE e.packet_id = p.id) AS evaluation_runs,
       (SELECT COUNT(*) FROM channel_runs r WHERE r.packet_id = p.id
          AND r.channel = 'quickfire_evaluation') AS evaluation_attempts,
       COALESCE((SELECT SUM(ep.latency_ms) FROM evaluation_passes ep
                 WHERE ep.packet_id = p.id), 0) AS evaluation_ms,
       ((julianday(COALESCE(p.completed_at, CURRENT_TIMESTAMP)) - julianday(p.created_at)) * 24.0) AS lifecycle_hours,
       (SELECT COUNT(*) FROM v_field_consensus c WHERE c.packet_id = p.id AND c.kind = 'score'
          AND c.status = 'accepted' AND CAST(c.final_value AS NUMERIC) >= COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'stats.score_threshold'), 7))
         AS scores_above_threshold,
       COALESCE((SELECT SUM(i.supports + i.contradictions) FROM evaluation_pass_items i
                 JOIN eva_packets e ON e.id = i.eva_packet_id
                 WHERE e.packet_id = p.id), 0) AS evidence_items,
       0 AS generation_ms
FROM packets p""",
        """CREATE VIEW v_branch_stats AS
WITH fed AS (
  SELECT r.ref AS root_ref, m.id AS member_id, m.ref AS member_ref
  FROM packets r
  JOIN packets m ON m.root_id = r.id OR m.id = r.id
  WHERE r.root_id IS NULL OR r.root_id = r.id
),
scores AS (
  SELECT fed.root_ref, CAST(c.final_value AS NUMERIC) AS v
  FROM fed JOIN v_field_consensus c ON c.packet_id = fed.member_id
  WHERE c.kind = 'score' AND c.status = 'accepted'
)
SELECT fed.root_ref,
       (SELECT MIN(p.created_at) FROM packets p WHERE p.ref = fed.root_ref) AS created_at,
       COUNT(DISTINCT CASE WHEN fed.member_ref <> fed.root_ref THEN fed.member_ref END)
         AS descendant_count,
       (SELECT AVG(s.v * s.v) - AVG(s.v) * AVG(s.v) FROM scores s
         WHERE s.root_ref = fed.root_ref) AS score_variance,
       COUNT(DISTINCT fed.member_id) - COUNT(DISTINCT fed.member_ref) AS revision_count,
       (SELECT COUNT(*) FROM channel_runs r JOIN fed f2 ON f2.member_id = r.packet_id
         WHERE f2.root_ref = fed.root_ref AND r.status IN ('failed', 'invalid')) AS failed_runs,
       (SELECT COALESCE(SUM(i.contradictions), 0) FROM evaluation_pass_items i
          JOIN eva_packets e ON e.id = i.eva_packet_id
          JOIN fed f3 ON f3.member_id = e.packet_id
         WHERE f3.root_ref = fed.root_ref) AS contradiction_count,
       (SELECT COUNT(DISTINCT ep.model) FROM evaluation_passes ep
          JOIN fed f4 ON f4.member_id = ep.packet_id
         WHERE f4.root_ref = fed.root_ref) AS distinct_models
FROM fed
GROUP BY fed.root_ref
HAVING COUNT(DISTINCT CASE WHEN fed.member_ref <> fed.root_ref THEN fed.member_ref END) > 0""",
        """CREATE VIEW v_branch_model_usage AS
SELECT r.ref AS root_ref, ep.model, COUNT(*) AS passes
FROM packets r
JOIN packets m ON m.root_id = r.id OR m.id = r.id
JOIN evaluation_passes ep ON ep.packet_id = m.id
WHERE r.root_id IS NULL OR r.root_id = r.id
GROUP BY r.ref, ep.model""",
        """CREATE VIEW v_branch_triggers AS
SELECT c.packet_id, c.packet_ref, c.packet_version,
       t.id AS threshold_id, t.name AS threshold_name, t.version AS threshold_version,
       t.action, t.precedence, COUNT(*) AS matched_items, MIN(c.field_name) AS field_name
FROM v_field_consensus c
JOIN thresholds t ON t.enabled
  AND (t.field_name = c.field_name
       OR (t.field_name = '@key_fields' AND c.field_name IN
           (SELECT d.name FROM candidate_field_definitions d WHERE d.is_key_field)))
WHERE c.phase = 'pre' AND (
     (t.operator = 'status_is' AND c.status = t.value)
  OR (t.operator = 'has_tag' AND c.kind = 'tagset' AND c.status = 'accepted'
      AND c.item_key = t.value AND c.final_value = 'on')
  OR (c.kind = 'score' AND c.status = 'accepted' AND (
         (t.operator = '>=' AND CAST(c.final_value AS NUMERIC) >= CAST(t.value AS NUMERIC)) OR (t.operator = '>' AND CAST(c.final_value AS NUMERIC) > CAST(t.value AS NUMERIC))
      OR (t.operator = '<=' AND CAST(c.final_value AS NUMERIC) <= CAST(t.value AS NUMERIC)) OR (t.operator = '<' AND CAST(c.final_value AS NUMERIC) < CAST(t.value AS NUMERIC))
      OR (t.operator = '=' AND CAST(c.final_value AS NUMERIC) = CAST(t.value AS NUMERIC)) OR (t.operator = '!=' AND CAST(c.final_value AS NUMERIC) <> CAST(t.value AS NUMERIC))))
  OR (c.kind IN ('bool', 'text') AND c.status = 'accepted' AND (
         (t.operator = '=' AND c.final_value = t.value)
      OR (t.operator = '!=' AND c.final_value <> t.value))))
GROUP BY c.packet_id, c.packet_ref, c.packet_version, t.id, t.name, t.version, t.action,
         t.precedence""",
        """CREATE VIEW v_ready_candidates AS
WITH req AS (
  SELECT ps.field_name, ps.phase, ps.tier, ps.hard
  FROM eva_presets ps
  WHERE ps.enabled AND ps.required AND ps.tier IN (1, 2)
    AND ps.revision = (SELECT MAX(p2.revision) FROM eva_presets p2
                       WHERE p2.field_name = ps.field_name AND p2.phase = ps.phase)
),
heads AS (
  SELECT p.* FROM packets p
  WHERE NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = p.id)
    AND NOT p.locked
)
SELECT h.id AS packet_id, h.ref AS packet_ref, h.version AS packet_version, h.status,
       COUNT(req.field_name) AS required_fields,
       SUM(CASE WHEN fs.status = 'accepted' THEN 1 ELSE 0 END) AS accepted,
       SUM(CASE WHEN fs.status = 'disputed' THEN 1 ELSE 0 END) AS disputed,
       SUM(CASE WHEN fs.status = 'null' THEN 1 ELSE 0 END) AS null_fields,
       SUM(CASE WHEN fs.status = 'pending' THEN 1 ELSE 0 END) AS pending,
       SUM(CASE WHEN fs.status IS NULL THEN 1 ELSE 0 END) AS missing,
       (SELECT COUNT(*) FROM waivers wv WHERE wv.packet_id = h.id
          AND wv.target_version = h.version
          AND NOT EXISTS (SELECT 1 FROM revocations rv WHERE rv.target_type = 'waiver'
                          AND rv.target_id = wv.id)) AS active_waivers,
       (SELECT COUNT(*) FROM packet_dependencies dep
          JOIN packets dp ON dp.id = dep.depends_on_id
         WHERE dep.packet_id = h.id
           AND NOT EXISTS (SELECT 1 FROM revocations rd WHERE rd.target_type = 'dependency'
                           AND rd.target_id = dep.id)
           AND NOT EXISTS (SELECT 1 FROM packets dh WHERE dh.ref = dp.ref
                           AND dh.status = 'completed')) AS unmet_dependencies,
       (SELECT COUNT(*) FROM confirmations cf WHERE cf.entity_id = h.id
          AND cf.status = 'pending') AS pending_approvals
FROM heads h
CROSS JOIN req
LEFT JOIN v_field_status fs ON fs.packet_id = h.id AND fs.field_name = req.field_name
  AND fs.phase = req.phase
WHERE req.phase = 'pre'
GROUP BY h.id, h.ref, h.version, h.status""",
        """CREATE VIEW v_queue AS
WITH x AS (
  SELECT p.id AS packet_id, p.ref AS packet_ref, p.version AS packet_version, p.status,
         p.created_at, p.claimed_by, p.lease_expires_at,
         COALESCE(o.priority_level, 5) AS objective_priority,
         COALESCE((SELECT CAST(c.final_value AS NUMERIC) FROM v_field_consensus c
                   WHERE c.packet_id = p.id AND c.field_name = 'ambiguity_quotient'
                     AND c.phase = 'pre' AND c.status = 'accepted'), 5) AS ambiguity,
         CASE WHEN p.manual_boost < 0 THEN 0 WHEN p.manual_boost > 10 THEN 10
              ELSE p.manual_boost END AS boost,
         (SELECT COUNT(*) FROM channel_runs r WHERE r.packet_id = p.id AND r.status = 'failed'
            AND r.channel IN ('local_generation', 'workbench_production')
            AND NOT EXISTS (SELECT 1 FROM confirmations cf WHERE cf.entity_id = p.id
                              AND cf.entity_type = 'failure_budget' AND cf.status = 'approved'
                              AND julianday(cf.decided_at)
                                  >= julianday(r.finished_at))) AS failed_runs,
         (SELECT MAX(r.finished_at) FROM channel_runs r WHERE r.packet_id = p.id
            AND r.status = 'failed'
            AND r.channel IN ('local_generation', 'workbench_production')) AS last_failure_at,
         ((julianday(CURRENT_TIMESTAMP) - julianday(p.created_at)) * 24.0) AS age_hours,
         (SELECT MIN(CASE o.kind WHEN 'block' THEN 1 WHEN 'hold' THEN 2 WHEN 'force' THEN 3
                                 WHEN 'pin' THEN 4 ELSE 5 END)
            FROM queue_overrides o WHERE o.packet_ref = p.ref AND NOT EXISTS (SELECT 1 FROM revocations rv WHERE rv.target_type = 'override' AND rv.target_id = o.id) AND (o.scope = 'lineage' OR o.target_version = p.version)) AS override_rank,
         COALESCE((SELECT SUM(o.value) FROM queue_overrides o WHERE o.packet_ref = p.ref
                     AND o.kind = 'reorder' AND NOT EXISTS (SELECT 1 FROM revocations rv WHERE rv.target_type = 'override' AND rv.target_id = o.id) AND (o.scope = 'lineage' OR o.target_version = p.version)), 0) AS reorder_delta,
         (SELECT COUNT(*) FROM confirmations cf WHERE cf.entity_id = p.id
            AND cf.status = 'pending') AS pending_approvals
  FROM packets p
  LEFT JOIN objectives o ON o.id = p.objective_id
  WHERE p.status IN ('ready', 'queued')
    AND NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = p.id)
),
y AS (
  SELECT x.*,
    CASE WHEN x.override_rank = 1 THEN 'blocked'
         WHEN x.override_rank = 2 THEN 'held'
         WHEN x.pending_approvals > 0 THEN 'awaiting_approval'
         WHEN x.failed_runs >= COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.failure_budget'), 3) THEN 'failure_budget'
         WHEN x.last_failure_at IS NOT NULL AND ((julianday(CURRENT_TIMESTAMP) - julianday(x.last_failure_at)) * 24.0) < (COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.retry_backoff_minutes'), 30) / 60.0) THEN 'backoff'
         WHEN x.claimed_by IS NOT NULL AND x.lease_expires_at IS NOT NULL
              AND julianday(x.lease_expires_at) > julianday(CURRENT_TIMESTAMP) THEN 'claimed'
         ELSE NULL END AS ineligible_reason,
    CASE WHEN x.override_rank = 3 THEN 1
         WHEN x.override_rank = 4 THEN 2
         WHEN x.age_hours >= COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.max_wait_hours'), 48) THEN 3
         ELSE 4 END AS queue_class,
    ROUND(CAST(
        COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_objective'), 0.30) * x.objective_priority / 10.0
      + COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_age'), 0.25) * (CASE WHEN x.age_hours < 0 THEN 0
                           WHEN x.age_hours > COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.max_age_hours'), 72) THEN COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.max_age_hours'), 72)
                           ELSE x.age_hours END) / COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.max_age_hours'), 72)
      + COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_ambiguity'), 0.15) * (10 - x.ambiguity) / 10.0
      + COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_boost'), 0.20) * x.boost / 10.0
      - COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_failures'), 0.10) * (CASE WHEN x.failed_runs > COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.failure_budget'), 3) THEN COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.failure_budget'), 3)
                                ELSE x.failed_runs END) / COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.failure_budget'), 3)
      + x.reorder_delta AS NUMERIC), 6) AS priority
  FROM x
)
SELECT y.packet_id, y.packet_ref, y.packet_version, y.status, y.created_at,
       CASE WHEN y.ineligible_reason IS NULL THEN 1 ELSE 0 END AS eligible,
       y.ineligible_reason, y.queue_class, y.priority, y.age_hours, y.failed_runs,
       CASE WHEN y.ineligible_reason IS NULL THEN ROW_NUMBER() OVER (
              PARTITION BY CASE WHEN y.ineligible_reason IS NULL THEN 0 ELSE 1 END
              ORDER BY y.queue_class, y.priority DESC, y.created_at, y.packet_ref)
            ELSE NULL END AS position
FROM y""",
    ],
    "postgresql": [
        """CREATE VIEW v_eva_progress AS
WITH base AS (
  SELECT e.id AS eva_packet_id, e.packet_id, e.field_name, e.phase, e.state, e.created_at,
         CASE WHEN d.kind = 'text' THEN 1 ELSE 0 END AS is_text,
  MAX(CASE WHEN ep.pass_no = 1 THEN 1 ELSE 0 END) AS done1,
  MAX(CASE WHEN ep.pass_no = 2 THEN 1 ELSE 0 END) AS done2,
  MAX(CASE WHEN ep.pass_no = 3 THEN 1 ELSE 0 END) AS done3,
  MAX(CASE WHEN ep.pass_no = 4 THEN 1 ELSE 0 END) AS done4,
  MAX(CASE WHEN ep.pass_no = 5 THEN 1 ELSE 0 END) AS done5,
  MAX(CASE WHEN ep.pass_no = 6 THEN 1 ELSE 0 END) AS done6,
  CASE WHEN EXISTS (SELECT 1 FROM evaluation_pass_items i WHERE i.eva_packet_id = e.id
                     AND i.pass_no = 1) THEN 1 ELSE 0 END AS a_present,
  CASE WHEN EXISTS (SELECT 1 FROM evaluation_pass_items i WHERE i.eva_packet_id = e.id
                     AND i.pass_no = 2) THEN 1 ELSE 0 END AS b_present
  FROM eva_packets e
  JOIN candidate_field_definitions d ON d.name = e.field_name
  LEFT JOIN evaluation_passes ep ON ep.eva_packet_id = e.id
  GROUP BY e.id, e.packet_id, e.field_name, e.phase, e.state, e.created_at, d.kind
)
SELECT base.*,
  CASE WHEN done1 = 1 AND done2 = 1
        AND (a_present = 0 OR (done4 = 1 AND (is_text = 1 OR done5 = 1)))
        AND (b_present = 0 OR (done3 = 1 AND (is_text = 1 OR done6 = 1)))
       THEN 1 ELSE 0 END AS complete
FROM base""",
        """CREATE VIEW v_eva_item_consensus AS
WITH x AS (
  SELECT e.id AS eva_packet_id, e.packet_id, e.packet_version, e.cycle_id AS eval_cycle_id,
         e.field_name, e.phase, e.created_at, d.kind,
         CASE WHEN d.kind = 'text' THEN 1 ELSE 0 END AS is_text,
         k.value AS item_key, pr.complete,
         i1.value_norm AS va, i2.value_norm AS vb,
         i3.score AS s3, i4.score AS s4, i5.score AS s5, i6.score AS s6,
         COALESCE(i3.supports, 0) AS sup3, COALESCE(i4.supports, 0) AS sup4,
         COALESCE(i5.supports, 0) AS sup5, COALESCE(i6.supports, 0) AS sup6,
         COALESCE(i3.contradictions, 0) AS con3, COALESCE(i4.contradictions, 0) AS con4,
         COALESCE(i5.contradictions, 0) AS con5, COALESCE(i6.contradictions, 0) AS con6,
         COALESCE(i3.unverified, 0) AS unv3, COALESCE(i4.unverified, 0) AS unv4,
         COALESCE(i5.unverified, 0) AS unv5, COALESCE(i6.unverified, 0) AS unv6
  FROM eva_packets e
  JOIN candidate_field_definitions d ON d.name = e.field_name
  JOIN v_eva_progress pr ON pr.eva_packet_id = e.id
  CROSS JOIN json_array_elements_text(e.item_keys) AS k(value)
  LEFT JOIN evaluation_pass_items i1 ON i1.eva_packet_id = e.id AND i1.pass_no = 1 AND i1.item_key = k.value
  LEFT JOIN evaluation_pass_items i2 ON i2.eva_packet_id = e.id AND i2.pass_no = 2 AND i2.item_key = k.value
  LEFT JOIN evaluation_pass_items i3 ON i3.eva_packet_id = e.id AND i3.pass_no = 3 AND i3.item_key = k.value
  LEFT JOIN evaluation_pass_items i4 ON i4.eva_packet_id = e.id AND i4.pass_no = 4 AND i4.item_key = k.value
  LEFT JOIN evaluation_pass_items i5 ON i5.eva_packet_id = e.id AND i5.pass_no = 5 AND i5.item_key = k.value
  LEFT JOIN evaluation_pass_items i6 ON i6.eva_packet_id = e.id AND i6.pass_no = 6 AND i6.item_key = k.value
),
c AS (
  SELECT x.*, CASE WHEN x.va IS NULL OR x.s4 IS NULL THEN NULL WHEN x.is_text = 1 THEN x.s4 * 1000 WHEN x.s5 IS NULL THEN NULL ELSE (CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.cross_weight'), 0.6) * 10000) AS INTEGER) * x.s4 + CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.self_weight'), 0.4) * 10000) AS INTEGER) * x.s5 + 5) / 10 END AS ca, CASE WHEN x.vb IS NULL OR x.s3 IS NULL THEN NULL WHEN x.is_text = 1 THEN x.s3 * 1000 WHEN x.s6 IS NULL THEN NULL ELSE (CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.cross_weight'), 0.6) * 10000) AS INTEGER) * x.s3 + CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.self_weight'), 0.4) * 10000) AS INTEGER) * x.s6 + 5) / 10 END AS cb FROM x
),
w AS (
  SELECT c.*,
    CASE WHEN c.va IS NULL AND c.vb IS NULL THEN 'both_null'
         WHEN c.va IS NULL THEN 'b_only'
         WHEN c.vb IS NULL THEN 'a_only'
         WHEN c.va = c.vb THEN 'agree'
         ELSE 'disagree' END AS shape,
    CASE WHEN c.va IS NULL AND c.vb IS NULL THEN NULL
         WHEN c.va IS NULL THEN 'b'
         WHEN c.vb IS NULL THEN 'a'
         WHEN c.va = c.vb THEN 'both'
         WHEN c.ca > c.cb THEN 'a'
         WHEN c.cb > c.ca THEN 'b'
         ELSE NULL END AS winner
  FROM c
),
v AS (
  SELECT w.*,
    CASE w.winner WHEN 'a' THEN w.sup4 + w.sup5 WHEN 'b' THEN w.sup3 + w.sup6
         WHEN 'both' THEN w.sup3 + w.sup4 + w.sup5 + w.sup6 ELSE 0 END AS supports,
    CASE w.winner WHEN 'a' THEN w.con4 + w.con5 WHEN 'b' THEN w.con3 + w.con6
         WHEN 'both' THEN w.con3 + w.con4 + w.con5 + w.con6 ELSE 0 END AS contradictions,
    CASE w.winner WHEN 'a' THEN w.unv4 + w.unv5 WHEN 'b' THEN w.unv3 + w.unv6
         WHEN 'both' THEN w.unv3 + w.unv4 + w.unv5 + w.unv6 ELSE 0 END AS unverified,
    CASE WHEN w.shape = 'agree' THEN (w.ca + w.cb + 1) / 2
         WHEN w.shape = 'a_only' THEN w.ca
         WHEN w.shape = 'b_only' THEN w.cb
         WHEN w.shape = 'disagree' THEN CASE WHEN w.ca >= w.cb THEN w.ca ELSE w.cb END
         ELSE NULL END AS decision_e4,
    CASE WHEN w.shape = 'disagree' THEN
           CASE WHEN w.ca >= w.cb THEN w.ca - w.cb ELSE w.cb - w.ca END
         ELSE NULL END AS margin_e4
  FROM w
),
s AS (
  SELECT v.*,
    CASE WHEN v.complete = 0 THEN 'pending'
         WHEN v.shape = 'both_null' THEN 'null'
         WHEN v.supports >= COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'consensus.min_supports'), 1) AND v.contradictions <= v.supports AND (
              (v.shape = 'agree' AND v.decision_e4 >= CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'consensus.agreement_min'), 0.60) * 10000) AS INTEGER))
           OR (v.shape IN ('a_only', 'b_only') AND v.decision_e4 >= CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'consensus.one_sided_min'), 0.70) * 10000) AS INTEGER))
           OR (v.shape = 'disagree' AND v.winner IS NOT NULL
               AND v.decision_e4 >= CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'consensus.winner_min'), 0.70) * 10000) AS INTEGER) AND v.margin_e4 >= CAST(ROUND(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'aggregation.dispute_margin'), 0.15) * 10000) AS INTEGER)))
         THEN 'accepted'
         ELSE 'disputed' END AS status
  FROM v
),
r AS (
  SELECT s.*,
    CASE WHEN s.status = 'pending' THEN NULL
         WHEN s.status = 'null' THEN 0
         WHEN s.status = 'accepted' AND s.shape = 'agree' THEN s.decision_e4
         ELSE CASE WHEN s.ca IS NULL THEN s.cb WHEN s.cb IS NULL THEN s.ca WHEN s.ca >= s.cb THEN s.ca ELSE s.cb END END AS confidence_e4
  FROM s
)
SELECT r.eva_packet_id, r.packet_id, r.packet_version, r.eval_cycle_id, r.field_name, r.phase,
       r.kind, r.item_key, r.complete, r.created_at,
       r.va AS value_a, r.vb AS value_b,
       r.ca AS conf_a_e4, r.cb AS conf_b_e4, r.confidence_e4,
       r.ca / 10000.0 AS conf_a, r.cb / 10000.0 AS conf_b,
       r.confidence_e4 / 10000.0 AS confidence,
       CASE WHEN r.shape = 'agree' THEN 1 ELSE 0 END AS agreement,
       r.status,
       CASE WHEN r.complete = 0 THEN 0 ELSE r.supports END AS supports,
       CASE WHEN r.complete = 0 THEN 0 ELSE r.contradictions END AS contradictions,
       CASE WHEN r.complete = 0 THEN 0 ELSE r.unverified END AS unverified,
       CASE WHEN r.complete = 1 AND r.winner = 'a' THEN r.va
            WHEN r.complete = 1 AND r.winner IN ('b', 'both') THEN r.vb
            ELSE NULL END AS winning_value
FROM r""",
        """CREATE VIEW v_field_consensus AS
WITH art AS (
  SELECT a.packet_id, a.sha256,
         ROW_NUMBER() OVER (PARTITION BY a.packet_id ORDER BY a.created_at DESC, a.id DESC)
           AS rn
  FROM artifacts a
),
bound AS (
  SELECT e.id, e.packet_id, e.field_name, e.phase, e.escalation, e.created_at, pr.complete,
         CASE WHEN ((e.phase = 'pre' AND e.target_hash = p.input_hash)
                    OR (e.phase = 'post' AND e.target_hash = la.sha256))
                   AND ps.enabled
                   AND ps.revision = (SELECT MAX(p2.revision) FROM eva_presets p2
                                      WHERE p2.field_name = e.field_name
                                        AND p2.phase = e.phase)
                   AND d.enabled
                   AND e.definition_version = d.version
              THEN 1 ELSE 0 END AS is_current
  FROM eva_packets e
  JOIN packets p ON p.id = e.packet_id
  JOIN v_eva_progress pr ON pr.eva_packet_id = e.id
  JOIN eva_presets ps ON ps.id = e.preset_id
  JOIN candidate_field_definitions d ON d.name = e.field_name
  LEFT JOIN art la ON la.packet_id = p.id AND la.rn = 1
  WHERE e.packet_version = p.version
    AND e.state <> 'superseded'
    AND NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = p.id)
),
cand AS (
  SELECT bound.*,
         ROW_NUMBER() OVER (PARTITION BY bound.packet_id, bound.field_name, bound.phase
                            ORDER BY bound.is_current DESC, bound.complete DESC,
                                     bound.escalation DESC, bound.created_at DESC,
                                     bound.id DESC) AS rn
  FROM bound
)
SELECT p.id AS packet_id, p.ref AS packet_ref, p.version AS packet_version,
       c.field_name, c.phase, c.item_key, c.kind,
       c.value_a, c.value_b,
       CASE WHEN cand.is_current = 1 THEN c.conf_a ELSE NULL END AS conf_a,
       CASE WHEN cand.is_current = 1 THEN c.conf_b ELSE NULL END AS conf_b,
       CASE WHEN cand.is_current = 1 THEN c.confidence ELSE NULL END AS confidence,
       CASE WHEN cand.is_current = 1 THEN c.confidence_e4 ELSE NULL END AS confidence_e4,
       c.agreement,
       CASE WHEN cand.is_current = 1 THEN c.status ELSE 'pending' END AS status,
       CASE WHEN cand.is_current = 1 AND c.status = 'accepted' THEN c.winning_value
            ELSE NULL END AS final_value,
       c.supports, c.contradictions, c.unverified, c.eva_packet_id, c.eval_cycle_id,
       cand.is_current
FROM cand
JOIN packets p ON p.id = cand.packet_id
JOIN v_eva_item_consensus c ON c.eva_packet_id = cand.id
WHERE cand.rn = 1""",
        """CREATE VIEW v_field_status AS
SELECT c.packet_id, c.packet_ref, c.packet_version, c.field_name, c.phase, c.kind,
       CASE WHEN SUM(CASE WHEN c.status = 'pending' THEN 1 ELSE 0 END) > 0 THEN 'pending'
            WHEN SUM(CASE WHEN c.status = 'disputed' THEN 1 ELSE 0 END) > 0 THEN 'disputed'
            WHEN SUM(CASE WHEN c.status = 'accepted' THEN 1 ELSE 0 END) = 0 THEN 'null'
            ELSE 'accepted' END AS status,
       COUNT(*) AS items,
       SUM(CASE WHEN c.status = 'accepted' THEN 1 ELSE 0 END) AS accepted_items,
       SUM(CASE WHEN c.status = 'disputed' THEN 1 ELSE 0 END) AS disputed_items,
       MIN(c.confidence) AS min_confidence,
       MAX(c.eva_packet_id) AS eva_packet_id
FROM v_field_consensus c
GROUP BY c.packet_id, c.packet_ref, c.packet_version, c.field_name, c.phase, c.kind""",
        """CREATE VIEW v_packet_stats AS
SELECT p.id AS packet_id, p.ref AS packet_ref, p.version AS packet_version,
       CASE WHEN TRIM(REPLACE(p.prompt_description, chr(10), ' ')) = '' THEN 0
            ELSE LENGTH(TRIM(REPLACE(p.prompt_description, chr(10), ' '))) - LENGTH(REPLACE(TRIM(REPLACE(p.prompt_description, chr(10), ' ')), ' ', '')) + 1 END
         AS prompt_word_count,
       (SELECT COUNT(*) FROM eva_packets e WHERE e.packet_id = p.id) AS evaluation_runs,
       (SELECT COUNT(*) FROM channel_runs r WHERE r.packet_id = p.id
          AND r.channel = 'quickfire_evaluation') AS evaluation_attempts,
       COALESCE((SELECT SUM(ep.latency_ms) FROM evaluation_passes ep
                 WHERE ep.packet_id = p.id), 0) AS evaluation_ms,
       (EXTRACT(EPOCH FROM (COALESCE(p.completed_at, CURRENT_TIMESTAMP) - p.created_at)) / 3600.0) AS lifecycle_hours,
       (SELECT COUNT(*) FROM v_field_consensus c WHERE c.packet_id = p.id AND c.kind = 'score'
          AND c.status = 'accepted' AND CAST(c.final_value AS NUMERIC) >= COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'stats.score_threshold'), 7))
         AS scores_above_threshold,
       COALESCE((SELECT SUM(i.supports + i.contradictions) FROM evaluation_pass_items i
                 JOIN eva_packets e ON e.id = i.eva_packet_id
                 WHERE e.packet_id = p.id), 0) AS evidence_items,
       0 AS generation_ms
FROM packets p""",
        """CREATE VIEW v_branch_stats AS
WITH fed AS (
  SELECT r.ref AS root_ref, m.id AS member_id, m.ref AS member_ref
  FROM packets r
  JOIN packets m ON m.root_id = r.id OR m.id = r.id
  WHERE r.root_id IS NULL OR r.root_id = r.id
),
scores AS (
  SELECT fed.root_ref, CAST(c.final_value AS NUMERIC) AS v
  FROM fed JOIN v_field_consensus c ON c.packet_id = fed.member_id
  WHERE c.kind = 'score' AND c.status = 'accepted'
)
SELECT fed.root_ref,
       (SELECT MIN(p.created_at) FROM packets p WHERE p.ref = fed.root_ref) AS created_at,
       COUNT(DISTINCT CASE WHEN fed.member_ref <> fed.root_ref THEN fed.member_ref END)
         AS descendant_count,
       (SELECT AVG(s.v * s.v) - AVG(s.v) * AVG(s.v) FROM scores s
         WHERE s.root_ref = fed.root_ref) AS score_variance,
       COUNT(DISTINCT fed.member_id) - COUNT(DISTINCT fed.member_ref) AS revision_count,
       (SELECT COUNT(*) FROM channel_runs r JOIN fed f2 ON f2.member_id = r.packet_id
         WHERE f2.root_ref = fed.root_ref AND r.status IN ('failed', 'invalid')) AS failed_runs,
       (SELECT COALESCE(SUM(i.contradictions), 0) FROM evaluation_pass_items i
          JOIN eva_packets e ON e.id = i.eva_packet_id
          JOIN fed f3 ON f3.member_id = e.packet_id
         WHERE f3.root_ref = fed.root_ref) AS contradiction_count,
       (SELECT COUNT(DISTINCT ep.model) FROM evaluation_passes ep
          JOIN fed f4 ON f4.member_id = ep.packet_id
         WHERE f4.root_ref = fed.root_ref) AS distinct_models
FROM fed
GROUP BY fed.root_ref
HAVING COUNT(DISTINCT CASE WHEN fed.member_ref <> fed.root_ref THEN fed.member_ref END) > 0""",
        """CREATE VIEW v_branch_model_usage AS
SELECT r.ref AS root_ref, ep.model, COUNT(*) AS passes
FROM packets r
JOIN packets m ON m.root_id = r.id OR m.id = r.id
JOIN evaluation_passes ep ON ep.packet_id = m.id
WHERE r.root_id IS NULL OR r.root_id = r.id
GROUP BY r.ref, ep.model""",
        """CREATE VIEW v_branch_triggers AS
SELECT c.packet_id, c.packet_ref, c.packet_version,
       t.id AS threshold_id, t.name AS threshold_name, t.version AS threshold_version,
       t.action, t.precedence, COUNT(*) AS matched_items, MIN(c.field_name) AS field_name
FROM v_field_consensus c
JOIN thresholds t ON t.enabled
  AND (t.field_name = c.field_name
       OR (t.field_name = '@key_fields' AND c.field_name IN
           (SELECT d.name FROM candidate_field_definitions d WHERE d.is_key_field)))
WHERE c.phase = 'pre' AND (
     (t.operator = 'status_is' AND c.status = t.value)
  OR (t.operator = 'has_tag' AND c.kind = 'tagset' AND c.status = 'accepted'
      AND c.item_key = t.value AND c.final_value = 'on')
  OR (c.kind = 'score' AND c.status = 'accepted' AND (
         (t.operator = '>=' AND CAST(c.final_value AS NUMERIC) >= CAST(t.value AS NUMERIC)) OR (t.operator = '>' AND CAST(c.final_value AS NUMERIC) > CAST(t.value AS NUMERIC))
      OR (t.operator = '<=' AND CAST(c.final_value AS NUMERIC) <= CAST(t.value AS NUMERIC)) OR (t.operator = '<' AND CAST(c.final_value AS NUMERIC) < CAST(t.value AS NUMERIC))
      OR (t.operator = '=' AND CAST(c.final_value AS NUMERIC) = CAST(t.value AS NUMERIC)) OR (t.operator = '!=' AND CAST(c.final_value AS NUMERIC) <> CAST(t.value AS NUMERIC))))
  OR (c.kind IN ('bool', 'text') AND c.status = 'accepted' AND (
         (t.operator = '=' AND c.final_value = t.value)
      OR (t.operator = '!=' AND c.final_value <> t.value))))
GROUP BY c.packet_id, c.packet_ref, c.packet_version, t.id, t.name, t.version, t.action,
         t.precedence""",
        """CREATE VIEW v_ready_candidates AS
WITH req AS (
  SELECT ps.field_name, ps.phase, ps.tier, ps.hard
  FROM eva_presets ps
  WHERE ps.enabled AND ps.required AND ps.tier IN (1, 2)
    AND ps.revision = (SELECT MAX(p2.revision) FROM eva_presets p2
                       WHERE p2.field_name = ps.field_name AND p2.phase = ps.phase)
),
heads AS (
  SELECT p.* FROM packets p
  WHERE NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = p.id)
    AND NOT p.locked
)
SELECT h.id AS packet_id, h.ref AS packet_ref, h.version AS packet_version, h.status,
       COUNT(req.field_name) AS required_fields,
       SUM(CASE WHEN fs.status = 'accepted' THEN 1 ELSE 0 END) AS accepted,
       SUM(CASE WHEN fs.status = 'disputed' THEN 1 ELSE 0 END) AS disputed,
       SUM(CASE WHEN fs.status = 'null' THEN 1 ELSE 0 END) AS null_fields,
       SUM(CASE WHEN fs.status = 'pending' THEN 1 ELSE 0 END) AS pending,
       SUM(CASE WHEN fs.status IS NULL THEN 1 ELSE 0 END) AS missing,
       (SELECT COUNT(*) FROM waivers wv WHERE wv.packet_id = h.id
          AND wv.target_version = h.version
          AND NOT EXISTS (SELECT 1 FROM revocations rv WHERE rv.target_type = 'waiver'
                          AND rv.target_id = wv.id)) AS active_waivers,
       (SELECT COUNT(*) FROM packet_dependencies dep
          JOIN packets dp ON dp.id = dep.depends_on_id
         WHERE dep.packet_id = h.id
           AND NOT EXISTS (SELECT 1 FROM revocations rd WHERE rd.target_type = 'dependency'
                           AND rd.target_id = dep.id)
           AND NOT EXISTS (SELECT 1 FROM packets dh WHERE dh.ref = dp.ref
                           AND dh.status = 'completed')) AS unmet_dependencies,
       (SELECT COUNT(*) FROM confirmations cf WHERE cf.entity_id = h.id
          AND cf.status = 'pending') AS pending_approvals
FROM heads h
CROSS JOIN req
LEFT JOIN v_field_status fs ON fs.packet_id = h.id AND fs.field_name = req.field_name
  AND fs.phase = req.phase
WHERE req.phase = 'pre'
GROUP BY h.id, h.ref, h.version, h.status""",
        """CREATE VIEW v_queue AS
WITH x AS (
  SELECT p.id AS packet_id, p.ref AS packet_ref, p.version AS packet_version, p.status,
         p.created_at, p.claimed_by, p.lease_expires_at,
         COALESCE(o.priority_level, 5) AS objective_priority,
         COALESCE((SELECT CAST(c.final_value AS NUMERIC) FROM v_field_consensus c
                   WHERE c.packet_id = p.id AND c.field_name = 'ambiguity_quotient'
                     AND c.phase = 'pre' AND c.status = 'accepted'), 5) AS ambiguity,
         CASE WHEN p.manual_boost < 0 THEN 0 WHEN p.manual_boost > 10 THEN 10
              ELSE p.manual_boost END AS boost,
         (SELECT COUNT(*) FROM channel_runs r WHERE r.packet_id = p.id AND r.status = 'failed'
            AND r.channel IN ('local_generation', 'workbench_production')
            AND NOT EXISTS (SELECT 1 FROM confirmations cf WHERE cf.entity_id = p.id
                              AND cf.entity_type = 'failure_budget' AND cf.status = 'approved'
                              AND cf.decided_at
                                  >= r.finished_at)) AS failed_runs,
         (SELECT MAX(r.finished_at) FROM channel_runs r WHERE r.packet_id = p.id
            AND r.status = 'failed'
            AND r.channel IN ('local_generation', 'workbench_production')) AS last_failure_at,
         (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - p.created_at)) / 3600.0) AS age_hours,
         (SELECT MIN(CASE o.kind WHEN 'block' THEN 1 WHEN 'hold' THEN 2 WHEN 'force' THEN 3
                                 WHEN 'pin' THEN 4 ELSE 5 END)
            FROM queue_overrides o WHERE o.packet_ref = p.ref AND NOT EXISTS (SELECT 1 FROM revocations rv WHERE rv.target_type = 'override' AND rv.target_id = o.id) AND (o.scope = 'lineage' OR o.target_version = p.version)) AS override_rank,
         COALESCE((SELECT SUM(o.value) FROM queue_overrides o WHERE o.packet_ref = p.ref
                     AND o.kind = 'reorder' AND NOT EXISTS (SELECT 1 FROM revocations rv WHERE rv.target_type = 'override' AND rv.target_id = o.id) AND (o.scope = 'lineage' OR o.target_version = p.version)), 0) AS reorder_delta,
         (SELECT COUNT(*) FROM confirmations cf WHERE cf.entity_id = p.id
            AND cf.status = 'pending') AS pending_approvals
  FROM packets p
  LEFT JOIN objectives o ON o.id = p.objective_id
  WHERE p.status IN ('ready', 'queued')
    AND NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = p.id)
),
y AS (
  SELECT x.*,
    CASE WHEN x.override_rank = 1 THEN 'blocked'
         WHEN x.override_rank = 2 THEN 'held'
         WHEN x.pending_approvals > 0 THEN 'awaiting_approval'
         WHEN x.failed_runs >= COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.failure_budget'), 3) THEN 'failure_budget'
         WHEN x.last_failure_at IS NOT NULL AND (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - x.last_failure_at)) / 3600.0) < (COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.retry_backoff_minutes'), 30) / 60.0) THEN 'backoff'
         WHEN x.claimed_by IS NOT NULL AND x.lease_expires_at IS NOT NULL
              AND x.lease_expires_at > CURRENT_TIMESTAMP THEN 'claimed'
         ELSE NULL END AS ineligible_reason,
    CASE WHEN x.override_rank = 3 THEN 1
         WHEN x.override_rank = 4 THEN 2
         WHEN x.age_hours >= COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.max_wait_hours'), 48) THEN 3
         ELSE 4 END AS queue_class,
    ROUND(CAST(
        COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_objective'), 0.30) * x.objective_priority / 10.0
      + COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_age'), 0.25) * (CASE WHEN x.age_hours < 0 THEN 0
                           WHEN x.age_hours > COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.max_age_hours'), 72) THEN COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.max_age_hours'), 72)
                           ELSE x.age_hours END) / COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.max_age_hours'), 72)
      + COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_ambiguity'), 0.15) * (10 - x.ambiguity) / 10.0
      + COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_boost'), 0.20) * x.boost / 10.0
      - COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'priority.w_failures'), 0.10) * (CASE WHEN x.failed_runs > COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.failure_budget'), 3) THEN COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.failure_budget'), 3)
                                ELSE x.failed_runs END) / COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st WHERE st.key = 'queue.failure_budget'), 3)
      + x.reorder_delta AS NUMERIC), 6) AS priority
  FROM x
)
SELECT y.packet_id, y.packet_ref, y.packet_version, y.status, y.created_at,
       CASE WHEN y.ineligible_reason IS NULL THEN 1 ELSE 0 END AS eligible,
       y.ineligible_reason, y.queue_class, y.priority, y.age_hours, y.failed_runs,
       CASE WHEN y.ineligible_reason IS NULL THEN ROW_NUMBER() OVER (
              PARTITION BY CASE WHEN y.ineligible_reason IS NULL THEN 0 ELSE 1 END
              ORDER BY y.queue_class, y.priority DESC, y.created_at, y.packet_ref)
            ELSE NULL END AS position
FROM y""",
    ],
}
