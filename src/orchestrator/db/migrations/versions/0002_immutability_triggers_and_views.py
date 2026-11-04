"""Immutability triggers (Requirement F, A1) and lineage-head views (A2).

FROZEN (R1 fix 4): every statement this revision applies is written out below, so replaying it
always produces the same database. Never edit this file; put changes in a new revision.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQLITE_UPGRADE: tuple[str, ...] = (
    """CREATE TRIGGER trg_projects_locked_no_update BEFORE UPDATE ON projects FOR EACH ROW WHEN OLD.locked = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: locked row in projects cannot be updated'); END""",
    """CREATE TRIGGER trg_projects_locked_no_delete BEFORE DELETE ON projects FOR EACH ROW WHEN OLD.locked = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: locked row in projects cannot be deleted'); END""",
    """CREATE TRIGGER trg_objectives_locked_no_update BEFORE UPDATE ON objectives FOR EACH ROW WHEN OLD.locked = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: locked row in objectives cannot be updated'); END""",
    """CREATE TRIGGER trg_objectives_locked_no_delete BEFORE DELETE ON objectives FOR EACH ROW WHEN OLD.locked = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: locked row in objectives cannot be deleted'); END""",
    """CREATE TRIGGER trg_packets_locked_no_update BEFORE UPDATE ON packets FOR EACH ROW WHEN OLD.locked = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: locked row in packets cannot be updated'); END""",
    """CREATE TRIGGER trg_packets_locked_no_delete BEFORE DELETE ON packets FOR EACH ROW WHEN OLD.locked = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: locked row in packets cannot be deleted'); END""",
    """CREATE TRIGGER trg_packets_completion_locks_insert BEFORE INSERT ON packets FOR EACH ROW WHEN NEW.status IN ('archived', 'completed') AND (NEW.locked IS NOT 1 OR NEW.completed_at IS NULL) BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: completed/archived packets must be locked with completed_at set'); END""",
    """CREATE TRIGGER trg_artifacts_accepted_at_insert BEFORE INSERT ON artifacts FOR EACH ROW WHEN NEW.accepted = 1 AND NEW.accepted_at IS NULL BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: accepted artifacts must have accepted_at set'); END""",
    """CREATE TRIGGER trg_packets_completion_locks_update BEFORE UPDATE ON packets FOR EACH ROW WHEN NEW.status IN ('archived', 'completed') AND (NEW.locked IS NOT 1 OR NEW.completed_at IS NULL) BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: completed/archived packets must be locked with completed_at set'); END""",
    """CREATE TRIGGER trg_artifacts_accepted_at_update BEFORE UPDATE ON artifacts FOR EACH ROW WHEN NEW.accepted = 1 AND NEW.accepted_at IS NULL BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: accepted artifacts must have accepted_at set'); END""",
    """CREATE TRIGGER trg_artifacts_accepted_no_update BEFORE UPDATE ON artifacts FOR EACH ROW WHEN OLD.accepted = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: accepted artifact cannot be updated'); END""",
    """CREATE TRIGGER trg_artifacts_accepted_no_delete BEFORE DELETE ON artifacts FOR EACH ROW WHEN OLD.accepted = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: accepted artifact cannot be deleted'); END""",
    """CREATE TRIGGER trg_evaluation_passes_append_only_update BEFORE UPDATE ON evaluation_passes FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: evaluation_passes is append-only (update rejected)'); END""",
    """CREATE TRIGGER trg_evaluation_passes_append_only_delete BEFORE DELETE ON evaluation_passes FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: evaluation_passes is append-only (delete rejected)'); END""",
    """CREATE TRIGGER trg_events_append_only_update BEFORE UPDATE ON events FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: events is append-only (update rejected)'); END""",
    """CREATE TRIGGER trg_events_append_only_delete BEFORE DELETE ON events FOR EACH ROW WHEN 1 = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: events is append-only (delete rejected)'); END""",
    """CREATE TRIGGER trg_autonomy_fixed_no_update BEFORE UPDATE ON autonomy_scopes FOR EACH ROW WHEN OLD.is_fixed = 1 AND (NEW.mode IS NOT OLD.mode OR NEW.is_fixed IS NOT OLD.is_fixed OR NEW.stage IS NOT OLD.stage) BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: fixed autonomy scope cannot be changed'); END""",
    """CREATE TRIGGER trg_autonomy_fixed_no_delete BEFORE DELETE ON autonomy_scopes FOR EACH ROW WHEN OLD.is_fixed = 1 BEGIN SELECT RAISE(ABORT, 'orchestrator-immutable: fixed autonomy scope cannot be deleted'); END""",
    """CREATE VIEW v_project_heads AS
WITH RECURSIVE chain(row_id, current_id) AS (
  SELECT id, id FROM projects
  UNION ALL
  SELECT chain.row_id, nxt.id FROM chain JOIN projects nxt ON nxt.supersedes_id = chain.current_id
)
SELECT chain.row_id AS id, head.id AS head_id, head.ref AS head_ref,
       head.version AS head_version, (chain.row_id = head.id) AS is_head
FROM chain JOIN projects head ON head.id = chain.current_id
WHERE NOT EXISTS (SELECT 1 FROM projects s WHERE s.supersedes_id = head.id)""",
    """CREATE VIEW v_objective_heads AS
WITH RECURSIVE chain(row_id, current_id) AS (
  SELECT id, id FROM objectives
  UNION ALL
  SELECT chain.row_id, nxt.id FROM chain JOIN objectives nxt ON nxt.supersedes_id = chain.current_id
)
SELECT chain.row_id AS id, head.id AS head_id, head.ref AS head_ref,
       head.version AS head_version, (chain.row_id = head.id) AS is_head
FROM chain JOIN objectives head ON head.id = chain.current_id
WHERE NOT EXISTS (SELECT 1 FROM objectives s WHERE s.supersedes_id = head.id)""",
    """CREATE VIEW v_packet_heads AS
WITH RECURSIVE chain(row_id, current_id) AS (
  SELECT id, id FROM packets
  UNION ALL
  SELECT chain.row_id, nxt.id FROM chain JOIN packets nxt ON nxt.supersedes_id = chain.current_id
)
SELECT chain.row_id AS id, head.id AS head_id, head.ref AS head_ref,
       head.version AS head_version, (chain.row_id = head.id) AS is_head
FROM chain JOIN packets head ON head.id = chain.current_id
WHERE NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = head.id)""",
)

POSTGRESQL_UPGRADE: tuple[str, ...] = (
    """CREATE OR REPLACE FUNCTION orch_locked_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    IF OLD.locked THEN
      RAISE EXCEPTION 'orchestrator-immutable: % rejected: row % in % is locked', lower(TG_OP), OLD.ref, TG_TABLE_NAME USING ERRCODE = '23001';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
    """CREATE OR REPLACE FUNCTION orch_packets_completion_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF NEW.status IN ('archived', 'completed')
     AND (NOT NEW.locked OR NEW.completed_at IS NULL) THEN
    RAISE EXCEPTION 'orchestrator-immutable: completed/archived packets must be locked with completed_at set' USING ERRCODE = '23001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
    """CREATE OR REPLACE FUNCTION orch_artifacts_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    IF OLD.accepted THEN
      RAISE EXCEPTION 'orchestrator-immutable: accepted artifact cannot be %', lower(TG_OP) USING ERRCODE = '23001';
    END IF;
  END IF;
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    IF NEW.accepted AND NEW.accepted_at IS NULL THEN
      RAISE EXCEPTION 'orchestrator-immutable: accepted artifacts must have accepted_at set' USING ERRCODE = '23001';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
    """CREATE OR REPLACE FUNCTION orch_append_only_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  RAISE EXCEPTION 'orchestrator-immutable: % is append-only (% rejected)', TG_TABLE_NAME, lower(TG_OP) USING ERRCODE = '23001';
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
    """CREATE OR REPLACE FUNCTION orch_autonomy_fixed_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    IF OLD.is_fixed THEN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'orchestrator-immutable: fixed autonomy scope cannot be deleted' USING ERRCODE = '23001';
      END IF;
      IF NEW.mode IS DISTINCT FROM OLD.mode
         OR NEW.is_fixed IS DISTINCT FROM OLD.is_fixed
         OR NEW.stage IS DISTINCT FROM OLD.stage THEN
        RAISE EXCEPTION 'orchestrator-immutable: fixed autonomy scope cannot be changed' USING ERRCODE = '23001';
      END IF;
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$fn$""",
    """CREATE TRIGGER trg_projects_locked BEFORE UPDATE OR DELETE ON projects FOR EACH ROW EXECUTE FUNCTION orch_locked_guard()""",
    """CREATE TRIGGER trg_objectives_locked BEFORE UPDATE OR DELETE ON objectives FOR EACH ROW EXECUTE FUNCTION orch_locked_guard()""",
    """CREATE TRIGGER trg_packets_locked BEFORE UPDATE OR DELETE ON packets FOR EACH ROW EXECUTE FUNCTION orch_locked_guard()""",
    """CREATE TRIGGER trg_packets_completion BEFORE INSERT OR UPDATE ON packets FOR EACH ROW EXECUTE FUNCTION orch_packets_completion_guard()""",
    """CREATE TRIGGER trg_artifacts_guard BEFORE INSERT OR UPDATE OR DELETE ON artifacts FOR EACH ROW EXECUTE FUNCTION orch_artifacts_guard()""",
    """CREATE TRIGGER trg_evaluation_passes_append_only BEFORE UPDATE OR DELETE ON evaluation_passes FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()""",
    """CREATE TRIGGER trg_evaluation_passes_no_truncate BEFORE TRUNCATE ON evaluation_passes FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
    """CREATE TRIGGER trg_events_append_only BEFORE UPDATE OR DELETE ON events FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()""",
    """CREATE TRIGGER trg_events_no_truncate BEFORE TRUNCATE ON events FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()""",
    """CREATE TRIGGER trg_autonomy_fixed_guard BEFORE UPDATE OR DELETE ON autonomy_scopes FOR EACH ROW EXECUTE FUNCTION orch_autonomy_fixed_guard()""",
    """CREATE VIEW v_project_heads AS
WITH RECURSIVE chain(row_id, current_id) AS (
  SELECT id, id FROM projects
  UNION ALL
  SELECT chain.row_id, nxt.id FROM chain JOIN projects nxt ON nxt.supersedes_id = chain.current_id
)
SELECT chain.row_id AS id, head.id AS head_id, head.ref AS head_ref,
       head.version AS head_version, (chain.row_id = head.id) AS is_head
FROM chain JOIN projects head ON head.id = chain.current_id
WHERE NOT EXISTS (SELECT 1 FROM projects s WHERE s.supersedes_id = head.id)""",
    """CREATE VIEW v_objective_heads AS
WITH RECURSIVE chain(row_id, current_id) AS (
  SELECT id, id FROM objectives
  UNION ALL
  SELECT chain.row_id, nxt.id FROM chain JOIN objectives nxt ON nxt.supersedes_id = chain.current_id
)
SELECT chain.row_id AS id, head.id AS head_id, head.ref AS head_ref,
       head.version AS head_version, (chain.row_id = head.id) AS is_head
FROM chain JOIN objectives head ON head.id = chain.current_id
WHERE NOT EXISTS (SELECT 1 FROM objectives s WHERE s.supersedes_id = head.id)""",
    """CREATE VIEW v_packet_heads AS
WITH RECURSIVE chain(row_id, current_id) AS (
  SELECT id, id FROM packets
  UNION ALL
  SELECT chain.row_id, nxt.id FROM chain JOIN packets nxt ON nxt.supersedes_id = chain.current_id
)
SELECT chain.row_id AS id, head.id AS head_id, head.ref AS head_ref,
       head.version AS head_version, (chain.row_id = head.id) AS is_head
FROM chain JOIN packets head ON head.id = chain.current_id
WHERE NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = head.id)""",
)

VIEWS: tuple[str, ...] = ("v_project_heads", "v_objective_heads", "v_packet_heads")
SQLITE_TRIGGERS: tuple[str, ...] = (
    "trg_projects_locked_no_update",
    "trg_projects_locked_no_delete",
    "trg_objectives_locked_no_update",
    "trg_objectives_locked_no_delete",
    "trg_packets_locked_no_update",
    "trg_packets_locked_no_delete",
    "trg_packets_completion_locks_insert",
    "trg_artifacts_accepted_at_insert",
    "trg_packets_completion_locks_update",
    "trg_artifacts_accepted_at_update",
    "trg_artifacts_accepted_no_update",
    "trg_artifacts_accepted_no_delete",
    "trg_evaluation_passes_append_only_update",
    "trg_evaluation_passes_append_only_delete",
    "trg_events_append_only_update",
    "trg_events_append_only_delete",
    "trg_autonomy_fixed_no_update",
    "trg_autonomy_fixed_no_delete",
)
POSTGRESQL_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("trg_projects_locked", "projects"),
    ("trg_objectives_locked", "objectives"),
    ("trg_packets_locked", "packets"),
    ("trg_packets_completion", "packets"),
    ("trg_artifacts_guard", "artifacts"),
    ("trg_evaluation_passes_append_only", "evaluation_passes"),
    ("trg_evaluation_passes_no_truncate", "evaluation_passes"),
    ("trg_events_append_only", "events"),
    ("trg_events_no_truncate", "events"),
    ("trg_autonomy_fixed_guard", "autonomy_scopes"),
)
POSTGRESQL_FUNCTIONS: tuple[str, ...] = (
    "orch_locked_guard",
    "orch_packets_completion_guard",
    "orch_artifacts_guard",
    "orch_append_only_guard",
    "orch_autonomy_fixed_guard",
)


def _run(statements: Sequence[str]) -> None:
    # Raw DBAPI cursor without parameters, so '%' and ':' in trigger bodies are not placeholders.
    cursor = op.get_bind().connection.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
    finally:
        cursor.close()


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    _run(SQLITE_UPGRADE if dialect == "sqlite" else POSTGRESQL_UPGRADE)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    statements = [f"DROP VIEW IF EXISTS {view}" for view in VIEWS]
    if dialect == "sqlite":
        statements += [f"DROP TRIGGER IF EXISTS {name}" for name in SQLITE_TRIGGERS]
    else:
        statements += [f"DROP TRIGGER IF EXISTS {n} ON {t}" for n, t in POSTGRESQL_TRIGGERS]
        statements += [f"DROP FUNCTION IF EXISTS {f}()" for f in POSTGRESQL_FUNCTIONS]
    _run(statements)
