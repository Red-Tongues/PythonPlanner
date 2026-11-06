"""Every trigger of the schema (part of the master schema; CLAUDE.md golden rule 7).

The triggers are the **guarantee** behind Requirement F (completed work is immutable). They must
never be disabled, including in tests. Every error message starts with ``IMMUTABLE_MARKER`` so
callers can recognise an immutability violation whatever the database driver.

Rules enforced identically on SQLite and PostgreSQL (amendment A1):

* ``projects``, ``objectives``, ``packets``: a row with ``OLD.locked`` true rejects UPDATE and
  DELETE — so ``locked`` can never go from true to false.
* ``packets`` (INSERT and UPDATE): ``status IN ('completed','archived')`` requires
  ``locked = true`` and ``completed_at`` set.
* ``artifacts``: once ``OLD.accepted`` is true, UPDATE and DELETE are rejected; on INSERT and
  UPDATE ``accepted = true`` requires ``accepted_at`` set.
* ``evaluation_passes``, ``events`` and every other append-only table: UPDATE and DELETE always
  rejected (PostgreSQL also TRUNCATE).
* ``autonomy_scopes``: fixed scopes (``is_fixed``) cannot be changed or deleted.
* Lineage (R1 fixes 1–2): superseded history is immutable; lineage fields never change.
* evaPackets, evaluation cycles, generation runs: identity and binding columns frozen from insert;
  finished rows final (M2, M2.1, M3).
* The packet and run transition tables (M3 C23), from ``orchestrator.transitions``.

``trigger_statements(dialect)`` returns them all in install order. ``orchestrator.db.schema``
assembles them with the tables (``db/models.py``) and views (``sql/views.py``) into the master
schema; the baseline migration holds the generated text (``scripts/generate_baseline.py``).
``is_immutability_violation`` / ``is_transition_violation`` are the runtime helpers.
"""

from __future__ import annotations

import logging

from orchestrator.contracts import LOCKING_STATUSES

log = logging.getLogger(__name__)

IMMUTABLE_MARKER = "orchestrator-immutable"
APPEND_ONLY_TABLES: tuple[str, ...] = ("evaluation_passes", "events")
LOCKABLE_TABLES: tuple[str, ...] = ("projects", "objectives", "packets")

#: Locking statuses checked by the M1 completion trigger. ``closed_unverified`` (M2.1) has its
#: own trigger (``_m21_*``).
_LOCKING_SQL = "'archived', 'completed', 'sealed'"
_LOCK_MSG = "completed/archived/sealed packets must be locked with completed_at set"
assert {"archived", "completed", "sealed"} <= {s.value for s in LOCKING_STATUSES}


# --------------------------------------------------------------------------- SQLite


def _sqlite_trigger(name: str, timing: str, table: str, when: str, message: str) -> list[str]:
    return [
        f"DROP TRIGGER IF EXISTS {name}",
        f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH ROW WHEN {when} "
        f"BEGIN SELECT RAISE(ABORT, '{IMMUTABLE_MARKER}: {message}'); END",
    ]


def _sqlite_statements() -> list[str]:
    t = _sqlite_trigger
    stmts: list[str] = []
    for table in LOCKABLE_TABLES:
        stmts += t(
            f"trg_{table}_locked_no_update",
            "BEFORE UPDATE",
            table,
            "OLD.locked = 1",
            f"locked row in {table} cannot be updated",
        )
        stmts += t(
            f"trg_{table}_locked_no_delete",
            "BEFORE DELETE",
            table,
            "OLD.locked = 1",
            f"locked row in {table} cannot be deleted",
        )
    lock_check = (
        f"NEW.status IN ({_LOCKING_SQL}) AND (NEW.locked IS NOT 1 OR NEW.completed_at IS NULL)"
    )
    accept_check = "NEW.accepted = 1 AND NEW.accepted_at IS NULL"
    for timing, op in (("BEFORE INSERT", "insert"), ("BEFORE UPDATE", "update")):
        stmts += t(
            f"trg_packets_completion_locks_{op}",
            timing,
            "packets",
            lock_check,
            "completed/archived/sealed packets must be locked with completed_at set",
        )
        stmts += t(
            f"trg_artifacts_accepted_at_{op}",
            timing,
            "artifacts",
            accept_check,
            "accepted artifacts must have accepted_at set",
        )
    stmts += t(
        "trg_artifacts_accepted_no_update",
        "BEFORE UPDATE",
        "artifacts",
        "OLD.accepted = 1",
        "accepted artifact cannot be updated",
    )
    stmts += t(
        "trg_artifacts_accepted_no_delete",
        "BEFORE DELETE",
        "artifacts",
        "OLD.accepted = 1",
        "accepted artifact cannot be deleted",
    )
    for table in APPEND_ONLY_TABLES:
        for timing, op in (("BEFORE UPDATE", "update"), ("BEFORE DELETE", "delete")):
            stmts += t(
                f"trg_{table}_append_only_{op}",
                timing,
                table,
                "1 = 1",
                f"{table} is append-only ({op} rejected)",
            )
    stmts += t(
        "trg_autonomy_fixed_no_update",
        "BEFORE UPDATE",
        "autonomy_scopes",
        "OLD.is_fixed = 1 AND (NEW.mode IS NOT OLD.mode OR NEW.is_fixed IS NOT OLD.is_fixed "
        "OR NEW.stage IS NOT OLD.stage)",
        "fixed autonomy scope cannot be changed",
    )
    stmts += t(
        "trg_autonomy_fixed_no_delete",
        "BEFORE DELETE",
        "autonomy_scopes",
        "OLD.is_fixed = 1",
        "fixed autonomy scope cannot be deleted",
    )
    return stmts


# --------------------------------------------------------------------------- PostgreSQL


def _pg_raise(message: str, *args: str) -> str:
    # 23001 = restrict_violation → surfaces as an IntegrityError through SQLAlchemy.
    fmt_args = "".join(f", {a}" for a in args)
    return f"RAISE EXCEPTION '{IMMUTABLE_MARKER}: {message}'{fmt_args} USING ERRCODE = '23001';"


def _pg_function(name: str, body: str) -> str:
    # NEW is NULL on DELETE and OLD is NULL on INSERT, so every access is guarded by TG_OP.
    return (
        f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $fn$\n"
        f"BEGIN\n{body}\n"
        "  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;\n"
        "  RETURN NEW;\n"
        "END;\n$fn$"
    )


def _pg_trigger(name: str, timing: str, table: str, func: str, level: str = "ROW") -> list[str]:
    return [
        f"DROP TRIGGER IF EXISTS {name} ON {table}",
        f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH {level} EXECUTE FUNCTION {func}()",
    ]


def _postgresql_statements() -> list[str]:
    locked_msg = _pg_raise(
        "% rejected: row % in % is locked", "lower(TG_OP)", "OLD.ref", "TG_TABLE_NAME"
    )
    stmts = [
        _pg_function(
            "orch_locked_guard",
            "  IF TG_OP IN ('UPDATE', 'DELETE') THEN\n"
            f"    IF OLD.locked THEN\n      {locked_msg}\n    END IF;\n"
            "  END IF;",
        ),
        _pg_function(
            "orch_packets_completion_guard",
            f"  IF NEW.status IN ({_LOCKING_SQL})\n"
            "     AND (NOT NEW.locked OR NEW.completed_at IS NULL) THEN\n"
            f"    {_pg_raise(_LOCK_MSG)}\n"
            "  END IF;",
        ),
        _pg_function(
            "orch_artifacts_guard",
            "  IF TG_OP IN ('UPDATE', 'DELETE') THEN\n"
            "    IF OLD.accepted THEN\n"
            f"      {_pg_raise('accepted artifact cannot be %', 'lower(TG_OP)')}\n"
            "    END IF;\n"
            "  END IF;\n"
            "  IF TG_OP IN ('INSERT', 'UPDATE') THEN\n"
            "    IF NEW.accepted AND NEW.accepted_at IS NULL THEN\n"
            f"      {_pg_raise('accepted artifacts must have accepted_at set')}\n"
            "    END IF;\n"
            "  END IF;",
        ),
        _pg_function(
            "orch_append_only_guard",
            "  " + _pg_raise("% is append-only (% rejected)", "TG_TABLE_NAME", "lower(TG_OP)"),
        ),
        _pg_function(
            "orch_autonomy_fixed_guard",
            "  IF TG_OP IN ('UPDATE', 'DELETE') THEN\n"
            "    IF OLD.is_fixed THEN\n"
            "      IF TG_OP = 'DELETE' THEN\n"
            f"        {_pg_raise('fixed autonomy scope cannot be deleted')}\n"
            "      END IF;\n"
            "      IF NEW.mode IS DISTINCT FROM OLD.mode\n"
            "         OR NEW.is_fixed IS DISTINCT FROM OLD.is_fixed\n"
            "         OR NEW.stage IS DISTINCT FROM OLD.stage THEN\n"
            f"        {_pg_raise('fixed autonomy scope cannot be changed')}\n"
            "      END IF;\n"
            "    END IF;\n"
            "  END IF;",
        ),
    ]
    tr = _pg_trigger
    for table in LOCKABLE_TABLES:
        stmts += tr(f"trg_{table}_locked", "BEFORE UPDATE OR DELETE", table, "orch_locked_guard")
    stmts += tr(
        "trg_packets_completion",
        "BEFORE INSERT OR UPDATE",
        "packets",
        "orch_packets_completion_guard",
    )
    stmts += tr(
        "trg_artifacts_guard",
        "BEFORE INSERT OR UPDATE OR DELETE",
        "artifacts",
        "orch_artifacts_guard",
    )
    for table in APPEND_ONLY_TABLES:
        stmts += tr(
            f"trg_{table}_append_only", "BEFORE UPDATE OR DELETE", table, "orch_append_only_guard"
        )
        stmts += tr(
            f"trg_{table}_no_truncate",
            "BEFORE TRUNCATE",
            table,
            "orch_append_only_guard",
            level="STATEMENT",
        )
    stmts += tr(
        "trg_autonomy_fixed_guard",
        "BEFORE UPDATE OR DELETE",
        "autonomy_scopes",
        "orch_autonomy_fixed_guard",
    )
    return stmts


# --------------------------------------------------------------------------- errors


def is_immutability_violation(exc: BaseException) -> bool:
    """True if a database error was raised by one of these triggers."""
    return IMMUTABLE_MARKER in str(exc)


# --------------------------------------------------------------------------- lineage (R1 fixes 1-2)

#: table → (ancestry columns a successor must share and that never change,
#:          SQL condition on OLD meaning "other rows depend on this one")
LINEAGE: dict[str, tuple[tuple[str, ...], str]] = {
    "projects": (
        (),
        "EXISTS (SELECT 1 FROM objectives d WHERE d.project_id = OLD.id) "
        "OR EXISTS (SELECT 1 FROM packets d WHERE d.project_id = OLD.id)",
    ),
    "objectives": (
        ("project_id",),
        "EXISTS (SELECT 1 FROM packets d WHERE d.objective_id = OLD.id)",
    ),
    "packets": (
        ("project_id", "objective_id", "parent_id", "root_id"),
        "EXISTS (SELECT 1 FROM packets d WHERE d.id <> OLD.id "
        "AND (d.parent_id = OLD.id OR d.root_id = OLD.id)) "
        "OR EXISTS (SELECT 1 FROM evaluation_passes d WHERE d.packet_id = OLD.id) "
        "OR EXISTS (SELECT 1 FROM artifacts d WHERE d.packet_id = OLD.id) "
        "OR EXISTS (SELECT 1 FROM channel_runs d WHERE d.packet_id = OLD.id) "
        "OR EXISTS (SELECT 1 FROM quality_reports d WHERE d.packet_id = OLD.id)",
    ),
}
IMMUTABLE_LINEAGE_COLUMNS = ("ref", "version", "supersedes_id")


def _create_sqlite_trigger(name: str, timing: str, table: str, when: str, message: str) -> str:
    return (
        f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH ROW WHEN {when} "
        f"BEGIN SELECT RAISE(ABORT, '{IMMUTABLE_MARKER}: {message}'); END"
    )


def _sqlite_append_only(table: str) -> list[str]:
    return [
        _create_sqlite_trigger(
            f"trg_{table}_append_only_{op}",
            f"BEFORE {op.upper()}",
            table,
            "1 = 1",
            f"{table} is append-only ({op} rejected)",
        )
        for op in ("update", "delete")
    ]


def _sqlite_lineage(table: str) -> list[str]:
    ancestry, dependents = LINEAGE[table]
    successor = f"EXISTS (SELECT 1 FROM {table} s WHERE s.supersedes_id = OLD.id)"
    same_ancestry = "".join(f" AND p.{c} IS NEW.{c}" for c in ancestry)
    valid_predecessor = (
        f"EXISTS (SELECT 1 FROM {table} p WHERE p.id = NEW.supersedes_id "
        f"AND p.ref = NEW.ref AND p.version + 1 = NEW.version{same_ancestry})"
    )
    changed = " OR ".join(
        f"NEW.{c} IS NOT OLD.{c}" for c in (*IMMUTABLE_LINEAGE_COLUMNS, *ancestry)
    )
    t = _create_sqlite_trigger
    return [
        t(
            f"trg_{table}_superseded_no_update",
            "BEFORE UPDATE",
            table,
            successor,
            f"superseded row in {table} is history and cannot be updated",
        ),
        t(
            f"trg_{table}_superseded_no_delete",
            "BEFORE DELETE",
            table,
            successor,
            f"superseded row in {table} is history and cannot be deleted",
        ),
        t(
            f"trg_{table}_successor_no_delete",
            "BEFORE DELETE",
            table,
            "OLD.supersedes_id IS NOT NULL",
            f"a superseding version in {table} cannot be deleted; archive instead",
        ),
        t(
            f"trg_{table}_dependents_no_delete",
            "BEFORE DELETE",
            table,
            dependents,
            f"row in {table} has dependents and cannot be deleted; archive instead",
        ),
        t(
            f"trg_{table}_lineage_no_rewrite",
            "BEFORE UPDATE",
            table,
            changed,
            f"lineage fields of {table} (ref, version, supersedes_id, ancestry) are immutable",
        ),
        t(
            f"trg_{table}_lineage_start",
            "BEFORE INSERT",
            table,
            "NEW.supersedes_id IS NULL AND NEW.version <> 1",
            f"a new lineage in {table} starts at version 1",
        ),
        t(
            f"trg_{table}_lineage_successor",
            "BEFORE INSERT",
            table,
            f"NEW.supersedes_id IS NOT NULL AND NOT {valid_predecessor}",
            f"a successor in {table} must keep the ref, have version = predecessor + 1 "
            "and the same ancestry",
        ),
    ]


def _pg_lineage(table: str) -> list[str]:
    ancestry, dependents = LINEAGE[table]
    same_ancestry = "".join(f" AND p.{c} IS NOT DISTINCT FROM NEW.{c}" for c in ancestry)
    changed = " OR ".join(
        f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in (*IMMUTABLE_LINEAGE_COLUMNS, *ancestry)
    )
    fn = f"orch_{table}_lineage_guard"
    r = _pg_raise
    history = r("superseded row % in % is history (% rejected)", "OLD.ref", "TG_TABLE_NAME",
                "lower(TG_OP)")  # fmt: skip
    successor = r("superseding version % v% in % cannot be deleted; archive instead", "OLD.ref",
                  "OLD.version", "TG_TABLE_NAME")  # fmt: skip
    has_dependents = r("row % in % has dependents and cannot be deleted; archive instead",
                       "OLD.ref", "TG_TABLE_NAME")  # fmt: skip
    rewrite = r("lineage fields of % (ref, version, supersedes_id, ancestry) are immutable",
                "TG_TABLE_NAME")  # fmt: skip
    start = r("a new lineage in % starts at version 1", "TG_TABLE_NAME")
    successor_rule = r("a successor in % must keep the ref, have version = predecessor + 1 and "
                       "the same ancestry", "TG_TABLE_NAME")  # fmt: skip
    body = f"""CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    IF EXISTS (SELECT 1 FROM {table} s WHERE s.supersedes_id = OLD.id) THEN
      {history}
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN
    IF OLD.supersedes_id IS NOT NULL THEN
      {successor}
    END IF;
    IF {dependents} THEN
      {has_dependents}
    END IF;
    RETURN OLD;
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF {changed} THEN
      {rewrite}
    END IF;
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.supersedes_id IS NULL THEN
      IF NEW.version <> 1 THEN
        {start}
      END IF;
    ELSIF NOT EXISTS (SELECT 1 FROM {table} p WHERE p.id = NEW.supersedes_id
                      AND p.ref = NEW.ref AND p.version + 1 = NEW.version{same_ancestry}) THEN
      {successor_rule}
    END IF;
  END IF;
  RETURN NEW;
END;
$fn$"""
    return [
        body,
        f"CREATE TRIGGER trg_{table}_lineage BEFORE INSERT OR UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {fn}()",
    ]


# --------------------------------------------------------------------------- evaPackets, runs (M2)

#: M1 append-only tables beyond the two above, and the M2 ones (UPDATE/DELETE rejected)
REGISTRY_APPEND_ONLY: tuple[str, ...] = ("field_registry_revisions",)
M2_APPEND_ONLY: tuple[str, ...] = (
    "evaluation_pass_items",
    "eva_presets",
    "queue_overrides",
    "waivers",
    "revocations",
    "queue_admissions",
    "branch_applications",
)
_NO_CONTENT_EVAL = "new content_eval packets are not allowed; evaluate output with evaPackets (B9)"
_EVA_TERMINAL_SQL = "OLD.state IN ('consensus', 'disputed', 'failed', 'superseded')"


def _m2_sqlite() -> list[str]:
    t = _create_sqlite_trigger
    stmts = [s for table in M2_APPEND_ONLY for s in _sqlite_append_only(table)]
    stmts += [
        t(
            "trg_eva_packets_terminal_no_update",
            "BEFORE UPDATE",
            "eva_packets",
            _EVA_TERMINAL_SQL,
            "a finished evaPacket cannot be changed",
        ),
        t(
            "trg_eva_packets_terminal_no_delete",
            "BEFORE DELETE",
            "eva_packets",
            _EVA_TERMINAL_SQL,
            "a finished evaPacket cannot be deleted",
        ),
        t(
            "trg_generation_runs_snapshot_frozen",
            "BEFORE UPDATE",
            "generation_runs",
            "NEW.evaluation_snapshot IS NOT OLD.evaluation_snapshot OR NEW.packet_id IS NOT "
            "OLD.packet_id OR NEW.packet_version IS NOT OLD.packet_version OR NEW.input_hash IS "
            "NOT OLD.input_hash",
            "a generation run evaluation snapshot is frozen",
        ),
        t(
            "trg_generation_runs_no_delete",
            "BEFORE DELETE",
            "generation_runs",
            "1 = 1",
            "generation runs cannot be deleted",
        ),
        t(
            "trg_packets_no_new_content_eval",
            "BEFORE INSERT",
            "packets",
            "NEW.packet_kind = 'content_eval'",
            "new content_eval packets are not allowed; evaluate output with evaPackets (B9)",
        ),
    ]
    return stmts


def _m2_pg_raise(message: str) -> str:
    return f"RAISE EXCEPTION '{IMMUTABLE_MARKER}: {message}' USING ERRCODE = '23001';"


def _m2_postgresql() -> list[str]:
    r = _m2_pg_raise
    stmts = [
        "CREATE OR REPLACE FUNCTION orch_eva_packets_guard() RETURNS trigger LANGUAGE plpgsql "
        "AS $fn$\n"
        "BEGIN\n"
        f"  IF {_EVA_TERMINAL_SQL} THEN\n"
        f"    {r('a finished evaPacket cannot be changed or deleted')}\n"
        "  END IF;\n"
        "  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;\n"
        "  RETURN NEW;\n"
        "END;\n$fn$",
        "CREATE OR REPLACE FUNCTION orch_generation_runs_guard() RETURNS trigger LANGUAGE "
        "plpgsql AS $fn$\n"
        "BEGIN\n"
        "  IF TG_OP = 'DELETE' THEN\n"
        f"    {r('generation runs cannot be deleted')}\n"
        "  END IF;\n"
        "  IF NEW.evaluation_snapshot::text IS DISTINCT FROM OLD.evaluation_snapshot::text\n"
        "     OR NEW.packet_id IS DISTINCT FROM OLD.packet_id\n"
        "     OR NEW.packet_version IS DISTINCT FROM OLD.packet_version\n"
        "     OR NEW.input_hash IS DISTINCT FROM OLD.input_hash THEN\n"
        f"    {r('a generation run evaluation snapshot is frozen')}\n"
        "  END IF;\n"
        "  RETURN NEW;\n"
        "END;\n$fn$",
        "CREATE OR REPLACE FUNCTION orch_packets_kind_guard() RETURNS trigger LANGUAGE plpgsql "
        "AS $fn$\n"
        "BEGIN\n"
        "  IF NEW.packet_kind = 'content_eval' THEN\n"
        f"    {r(_NO_CONTENT_EVAL)}\n"
        "  END IF;\n"
        "  RETURN NEW;\n"
        "END;\n$fn$",
    ]
    for table in M2_APPEND_ONLY:
        stmts += [
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()",
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()",
        ]
    stmts += [
        "CREATE TRIGGER trg_eva_packets_terminal BEFORE UPDATE OR DELETE ON eva_packets "
        "FOR EACH ROW EXECUTE FUNCTION orch_eva_packets_guard()",
        "CREATE TRIGGER trg_generation_runs_guard BEFORE UPDATE OR DELETE ON generation_runs "
        "FOR EACH ROW EXECUTE FUNCTION orch_generation_runs_guard()",
        "CREATE TRIGGER trg_packets_no_new_content_eval BEFORE INSERT ON packets "
        "FOR EACH ROW EXECUTE FUNCTION orch_packets_kind_guard()",
    ]
    return stmts


# --------------------------------------------------------------------------- M2.1 (review R2)
# Fixes 5 and 6: closed_unverified locks; cycle bindings and evaPacket identity frozen.

#: binding columns of an evaluation cycle: frozen after insert (only state / completed_at move)
CYCLE_BINDINGS: tuple[str, ...] = (
    "packet_id",
    "packet_version",
    "phase",
    "target_hash",
    "project_id",
    "project_version",
    "objective_id",
    "objective_version",
    "registry_revision_id",
    "pass_plan",
    "models",
    "created_at",
)
#: identity columns of an evaPacket: frozen from insert (only state / error / updated_at move)
EVA_IDENTITY: tuple[str, ...] = (
    "packet_id",
    "packet_version",
    "cycle_id",
    "preset_id",
    "field_name",
    "definition_version",
    "phase",
    "plan",
    "item_keys",
    "target_hash",
    "model_config",
    "model_config_hash",
    "content_address",
    "idempotency_key",
    "escalation",
    "escalated_from_id",
    "reused_from_id",
    "created_at",
)
JSON_COLUMNS: frozenset[str] = frozenset({"models", "item_keys", "model_config"})
#: PostgreSQL tables that get a statement-level BEFORE TRUNCATE guard (M2.1)
M21_TRUNCATE_GUARDED: tuple[str, ...] = (
    "generation_runs",
    "packet_dependencies",
    "evaluation_cycles",
    "eva_packets",
)


def _m21_sqlite() -> list[str]:
    def trigger(name: str, timing: str, table: str, when: str, message: str) -> str:
        return (
            f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH ROW WHEN {when} "
            f"BEGIN SELECT RAISE(ABORT, '{IMMUTABLE_MARKER}: {message}'); END"
        )

    def changed(columns: tuple[str, ...]) -> str:
        return " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in columns)

    closed = (
        "NEW.status = 'closed_unverified' AND (NEW.locked IS NOT 1 OR NEW.completed_at IS NULL)"
    )
    stmts = [
        trigger(
            f"trg_packets_closed_unverified_locks_{op}",
            f"BEFORE {op.upper()}",
            "packets",
            closed,
            "closed_unverified packets must be locked with completed_at set",
        )
        for op in ("insert", "update")
    ]
    stmts += [
        trigger(
            "trg_evaluation_cycles_bindings_frozen",
            "BEFORE UPDATE",
            "evaluation_cycles",
            changed(CYCLE_BINDINGS),
            "evaluation cycle bindings are frozen after insert",
        ),
        trigger(
            "trg_evaluation_cycles_no_delete",
            "BEFORE DELETE",
            "evaluation_cycles",
            "1 = 1",
            "evaluation cycles cannot be deleted",
        ),
        trigger(
            "trg_eva_packets_identity_frozen",
            "BEFORE UPDATE",
            "eva_packets",
            changed(EVA_IDENTITY),
            "evaPacket identity columns are frozen from insert",
        ),
    ]
    stmts += [
        trigger(
            f"trg_packet_dependencies_append_only_{op}",
            f"BEFORE {op.upper()}",
            "packet_dependencies",
            "1 = 1",
            f"packet_dependencies is append-only ({op} rejected)",
        )
        for op in ("update", "delete")
    ]
    return stmts


def _m21_postgresql() -> list[str]:
    def distinct(columns: tuple[str, ...]) -> str:
        return "\n     OR ".join(
            f"NEW.{c}::text IS DISTINCT FROM OLD.{c}::text"
            if c in JSON_COLUMNS
            else f"NEW.{c} IS DISTINCT FROM OLD.{c}"
            for c in columns
        )

    stmts = [
        _pg_function(
            "orch_packets_closed_guard",
            "  IF NEW.status = 'closed_unverified'\n"
            "     AND (NOT NEW.locked OR NEW.completed_at IS NULL) THEN\n"
            f"    {_pg_raise('closed_unverified packets must be locked with completed_at set')}\n"
            "  END IF;",
        ),
        _pg_function(
            "orch_evaluation_cycles_guard",
            "  IF TG_OP = 'DELETE' THEN\n"
            f"    {_pg_raise('evaluation cycles cannot be deleted')}\n"
            "  END IF;\n"
            f"  IF {distinct(CYCLE_BINDINGS)} THEN\n"
            f"    {_pg_raise('evaluation cycle bindings are frozen after insert')}\n"
            "  END IF;",
        ),
        _pg_function(
            "orch_eva_packets_identity_guard",
            f"  IF {distinct(EVA_IDENTITY)} THEN\n"
            f"    {_pg_raise('evaPacket identity columns are frozen from insert')}\n"
            "  END IF;",
        ),
    ]
    stmts += [
        "CREATE TRIGGER trg_packets_closed_unverified BEFORE INSERT OR UPDATE ON packets "
        "FOR EACH ROW EXECUTE FUNCTION orch_packets_closed_guard()",
        "CREATE TRIGGER trg_evaluation_cycles_guard BEFORE UPDATE OR DELETE ON evaluation_cycles "
        "FOR EACH ROW EXECUTE FUNCTION orch_evaluation_cycles_guard()",
        "CREATE TRIGGER trg_eva_packets_identity BEFORE UPDATE ON eva_packets "
        "FOR EACH ROW EXECUTE FUNCTION orch_eva_packets_identity_guard()",
        "CREATE TRIGGER trg_packet_dependencies_append_only BEFORE UPDATE OR DELETE ON "
        "packet_dependencies FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()",
    ]
    stmts += [
        f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
        "FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()"
        for table in M21_TRUNCATE_GUARDED
    ]
    return stmts


def m21_statements(dialect: str) -> list[str]:
    """The M2.1 trigger DDL, per dialect."""
    if dialect == "sqlite":
        return _m21_sqlite()
    if dialect == "postgresql":
        return _m21_postgresql()
    raise NotImplementedError(f"no M2.1 DDL for dialect {dialect!r}")


# --------------------------------------------------------------------------- M3 (C22-C26)
# Runs, fencing, the kernel lock, governor and budgets, and the transition tables.

TRANSITION_MARKER = "orchestrator-transition"

#: M3 append-only tables (UPDATE and DELETE always rejected; PostgreSQL also TRUNCATE)
M3_APPEND_ONLY: tuple[str, ...] = (
    "channel_runs",
    "channel_run_outcomes",
    "governor_events",
    "budget_reservations",
    "budget_reconciliations",
    "budget_topups",
)
#: M3 tables whose rows are never deleted (mutable state, but history is kept)
M3_NO_DELETE: tuple[str, ...] = ("kernel_leases", "governor_states", "budget_counters")
#: generation_runs columns fixed from insert (in addition to 0005's snapshot binding)
RUN_FROZEN: tuple[str, ...] = (
    "claimed_by",
    "channel",
    "fencing_token",
    "claim_content_revision",
    "created_at",
)
#: generation_runs columns that may be set once and never changed afterwards
RUN_WRITE_ONCE: tuple[str, ...] = ("manifest_hash", "commit_sha")
RUN_TERMINAL_SQL = "'failed', 'stale', 'succeeded', 'abandoned'"
RUN_BINDINGS_MSG = "generation run bindings are frozen (gate revision moves by +1 only)"
GOVERNOR_MSG = "a governor state change must increase its revision and keep its scope"


def _pairs_sql(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(f"'{a}>{b}'" for a, b in pairs)


def _m3_sqlite(packet_pairs: list[tuple[str, str]], run_pairs: list[tuple[str, str]]) -> list[str]:
    def trigger(name: str, timing: str, table: str, when: str, message: str, marker: str) -> str:
        return (
            f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH ROW WHEN {when} "
            f"BEGIN SELECT RAISE(ABORT, '{marker}: {message}'); END"
        )

    imm, tra = IMMUTABLE_MARKER, TRANSITION_MARKER
    stmts = [
        trigger(
            "trg_packets_transition_table",
            "BEFORE UPDATE",
            "packets",
            "NEW.status IS NOT OLD.status AND (OLD.status || '>' || NEW.status) NOT IN ("
            + _pairs_sql(packet_pairs)
            + ")",
            "packet status change is not in the M3 transition table",
            tra,
        ),
        trigger(
            "trg_generation_runs_transition_table",
            "BEFORE UPDATE",
            "generation_runs",
            "NEW.state IS NOT OLD.state AND (OLD.state || '>' || NEW.state) NOT IN ("
            + _pairs_sql(run_pairs)
            + ")",
            "run state change is not in the M3 transition table",
            tra,
        ),
        trigger(
            "trg_generation_runs_final",
            "BEFORE UPDATE",
            "generation_runs",
            f"OLD.state IN ({RUN_TERMINAL_SQL}) "
            "OR (OLD.state = 'accepted' AND OLD.commit_sha IS NOT NULL)",
            "a finished generation run cannot be changed",
            imm,
        ),
        trigger(
            "trg_generation_runs_bindings_frozen",
            "BEFORE UPDATE",
            "generation_runs",
            " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in RUN_FROZEN)
            + " OR "
            + " OR ".join(
                f"(OLD.{c} IS NOT NULL AND NEW.{c} IS NOT OLD.{c})" for c in RUN_WRITE_ONCE
            )
            + " OR (NEW.gate_revision IS NOT OLD.gate_revision "
            "AND (OLD.gate_revision IS NULL OR NEW.gate_revision IS NOT OLD.gate_revision + 1))",
            "generation run bindings are frozen (gate revision moves by +1 only)",
            imm,
        ),
        trigger(
            "trg_kernel_leases_epoch",
            "BEFORE UPDATE",
            "kernel_leases",
            "NEW.epoch < OLD.epoch OR NEW.name IS NOT OLD.name",
            "the kernel lease epoch never decreases",
            imm,
        ),
        trigger(
            "trg_governor_states_revision",
            "BEFORE UPDATE",
            "governor_states",
            "NEW.revision <= OLD.revision OR NEW.scope_type IS NOT OLD.scope_type "
            "OR NEW.scope_id IS NOT OLD.scope_id",
            "a governor state change must increase its revision and keep its scope",
            imm,
        ),
    ]
    for table in M3_APPEND_ONLY:
        stmts += [
            trigger(
                f"trg_{table}_append_only_{op}",
                f"BEFORE {op.upper()}",
                table,
                "1 = 1",
                f"{table} is append-only ({op} rejected)",
                imm,
            )
            for op in ("update", "delete")
        ]
    stmts += [
        trigger(
            f"trg_{table}_no_delete",
            "BEFORE DELETE",
            table,
            "1 = 1",
            f"{table} rows cannot be deleted",
            imm,
        )
        for table in M3_NO_DELETE
    ]
    return stmts


def _m3_postgresql(
    packet_pairs: list[tuple[str, str]], run_pairs: list[tuple[str, str]]
) -> list[str]:
    def raise_(marker: str, message: str) -> str:
        return f"RAISE EXCEPTION '{marker}: {message}' USING ERRCODE = '23001';"

    imm, tra = IMMUTABLE_MARKER, TRANSITION_MARKER
    frozen = "\n     OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in RUN_FROZEN)
    once = "\n     OR ".join(
        f"(OLD.{c} IS NOT NULL AND NEW.{c} IS DISTINCT FROM OLD.{c})" for c in RUN_WRITE_ONCE
    )
    stmts = [
        _pg_function(
            "orch_packets_transition_guard",
            "  IF NEW.status IS DISTINCT FROM OLD.status\n"
            "     AND (OLD.status || '>' || NEW.status) NOT IN (" + _pairs_sql(packet_pairs) + ") "
            "THEN\n"
            f"    {raise_(tra, 'packet status change is not in the M3 transition table')}\n"
            "  END IF;",
        ),
        _pg_function(
            "orch_generation_runs_m3_guard",
            f"  IF OLD.state IN ({RUN_TERMINAL_SQL})\n"
            "     OR (OLD.state = 'accepted' AND OLD.commit_sha IS NOT NULL) THEN\n"
            f"    {raise_(imm, 'a finished generation run cannot be changed')}\n"
            "  END IF;\n"
            "  IF NEW.state IS DISTINCT FROM OLD.state\n"
            "     AND (OLD.state || '>' || NEW.state) NOT IN (" + _pairs_sql(run_pairs) + ") "
            "THEN\n"
            f"    {raise_(tra, 'run state change is not in the M3 transition table')}\n"
            "  END IF;\n"
            f"  IF {frozen}\n     OR {once}\n"
            "     OR (NEW.gate_revision IS DISTINCT FROM OLD.gate_revision\n"
            "         AND (OLD.gate_revision IS NULL\n"
            "              OR NEW.gate_revision IS DISTINCT FROM OLD.gate_revision + 1)) THEN\n"
            f"    {raise_(imm, RUN_BINDINGS_MSG)}\n"
            "  END IF;",
        ),
        _pg_function(
            "orch_kernel_leases_guard",
            "  IF TG_OP = 'DELETE' THEN\n"
            f"    {raise_(imm, 'kernel_leases rows cannot be deleted')}\n"
            "  END IF;\n"
            "  IF NEW.epoch < OLD.epoch OR NEW.name IS DISTINCT FROM OLD.name THEN\n"
            f"    {raise_(imm, 'the kernel lease epoch never decreases')}\n"
            "  END IF;",
        ),
        _pg_function(
            "orch_governor_states_guard",
            "  IF TG_OP = 'DELETE' THEN\n"
            f"    {raise_(imm, 'governor_states rows cannot be deleted')}\n"
            "  END IF;\n"
            "  IF NEW.revision <= OLD.revision OR NEW.scope_type IS DISTINCT FROM OLD.scope_type\n"
            "     OR NEW.scope_id IS DISTINCT FROM OLD.scope_id THEN\n"
            f"    {raise_(imm, GOVERNOR_MSG)}\n"
            "  END IF;",
        ),
        _pg_function(
            "orch_no_delete_guard", "  " + _pg_raise("% rows cannot be deleted", "TG_TABLE_NAME")
        ),
        "CREATE TRIGGER trg_packets_transition_table BEFORE UPDATE ON packets "
        "FOR EACH ROW EXECUTE FUNCTION orch_packets_transition_guard()",
        "CREATE TRIGGER trg_generation_runs_m3 BEFORE UPDATE ON generation_runs "
        "FOR EACH ROW EXECUTE FUNCTION orch_generation_runs_m3_guard()",
        "CREATE TRIGGER trg_kernel_leases_guard BEFORE UPDATE OR DELETE ON kernel_leases "
        "FOR EACH ROW EXECUTE FUNCTION orch_kernel_leases_guard()",
        "CREATE TRIGGER trg_governor_states_guard BEFORE UPDATE OR DELETE ON governor_states "
        "FOR EACH ROW EXECUTE FUNCTION orch_governor_states_guard()",
        "CREATE TRIGGER trg_budget_counters_no_delete BEFORE DELETE ON budget_counters "
        "FOR EACH ROW EXECUTE FUNCTION orch_no_delete_guard()",
    ]
    for table in M3_APPEND_ONLY:
        stmts += [
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()",
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()",
        ]
    for table in M3_NO_DELETE:
        stmts.append(
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION orch_no_delete_guard()"
        )
    return stmts


M3_PG_FUNCTIONS: tuple[str, ...] = (
    "orch_packets_transition_guard",
    "orch_generation_runs_m3_guard",
    "orch_kernel_leases_guard",
    "orch_governor_states_guard",
    "orch_no_delete_guard",
)


def m3_statements(dialect: str) -> list[str]:
    """The M3 trigger DDL, per dialect, from the M3 transition tables."""
    from orchestrator.transitions import packet_pairs, run_pairs

    if dialect == "sqlite":
        return _m3_sqlite(packet_pairs(), run_pairs())
    if dialect == "postgresql":
        return _m3_postgresql(packet_pairs(), run_pairs())
    raise NotImplementedError(f"no M3 DDL for dialect {dialect!r}")


def is_transition_violation(exc: BaseException) -> bool:
    """True if a database error was raised by the transition-table triggers."""
    return TRANSITION_MARKER in str(exc)


# --------------------------------------------------------------------------- M3a (R6)
# The genPacket loop: revision capture, append-only history, sealed bundles and queue records.
#
# R6 OQ32: every guard (immutability, lineage, transition table) is a BEFORE trigger and the
# outbox capture is an AFTER UPDATE trigger, so a rejected change aborts before any outbox row
# is written, on both dialects. R6 OQ33: ``content_revision`` moves only with an audited column.

#: table → audited text-comparable columns (an UPDATE OF them is captured into the outbox)
AUDITED_COLUMNS: dict[str, tuple[str, ...]] = {
    "packets": (
        "title",
        "prompt_description",
        "response_structure",
        "response_contract",
        "repo_target_path",
        "priority_level",
        "manual_boost",
    ),
    "projects": ("name", "brief", "industry", "scope"),
    "objectives": ("name", "brief", "priority_level"),
}
ENTITY_OF: dict[str, str] = {"packets": "packet", "projects": "project", "objectives": "objective"}
M3A_APPEND_ONLY: tuple[str, ...] = (
    "content_blobs",
    "field_revisions",
    "sealed_bundles",
    "eva_discard_log",
    "queue_epoch_snapshot",
    "feedback_events",
    "crash_events",
)
M3A_NO_DELETE: tuple[str, ...] = ("route_health", "eva_context_links")
#: eva_context_links columns frozen from insert (only ``is_questioned`` may go false → true)
LINK_FROZEN: tuple[str, ...] = (
    "id",
    "eva_packet_id",
    "packet_id",
    "field_name",
    "source_type",
    "source_id",
    "source_ref",
    "source_hash",
    "source_revision",
    "link_strength",
    "link_reason",
    "quote",
    "created_at",
)
LINK_MSG = "eva_context_links are append-only; only is_questioned may go from false to true"


def _m3a_sqlite() -> list[str]:
    t = _create_sqlite_trigger
    stmts = [s for table in M3A_APPEND_ONLY for s in _sqlite_append_only(table)]
    stmts += [
        t(
            f"trg_{table}_no_delete",
            "BEFORE DELETE",
            table,
            "1 = 1",
            f"{table} rows cannot be deleted",
        )  # fmt: skip
        for table in M3A_NO_DELETE
    ]
    stmts.append(
        t(
            "trg_eva_context_links_frozen",
            "BEFORE UPDATE",
            "eva_context_links",
            " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in LINK_FROZEN)
            + " OR (OLD.is_questioned = 1 AND NEW.is_questioned IS NOT 1)",
            LINK_MSG,
        )
    )
    for table, columns in AUDITED_COLUMNS.items():
        cols = ", ".join(columns)
        changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in columns)
        same = "OLD.content_revision = NEW.content_revision"
        stmts.append(
            f"CREATE TRIGGER trg_{table}_content_revision AFTER UPDATE OF {cols} ON {table} "
            f"FOR EACH ROW WHEN ({changed}) AND {same} "
            f"BEGIN UPDATE {table} SET content_revision = OLD.content_revision + 1, "
            "last_producer = 'user_sql' WHERE id = NEW.id; END"
        )
        producer = f"CASE WHEN {same} THEN 'user_sql' ELSE NEW.last_producer END"
        revision = f"CASE WHEN {same} THEN OLD.content_revision + 1 ELSE NEW.content_revision END"
        selects = "\nUNION ALL\n".join(
            f"SELECT ctx.tx, '{ENTITY_OF[table]}', NEW.id, '{c}', CAST(OLD.{c} AS TEXT), "
            f"CAST(NEW.{c} AS TEXT), {producer}, {revision}, CURRENT_TIMESTAMP FROM ctx "
            f"WHERE OLD.{c} IS NOT NEW.{c}"
            for c in columns
        )
        stmts.append(
            f"CREATE TRIGGER trg_{table}_outbox AFTER UPDATE OF {cols} ON {table} "
            f"FOR EACH ROW WHEN {changed}\nBEGIN\n"
            "INSERT INTO raw_revision_outbox (tx_id, entity_type, entity_id, field_name, "
            "old_value, new_value, producer, content_revision, captured_at)\n"
            f"WITH ctx AS (SELECT lower(hex(randomblob(8))) AS tx)\n{selects};\nEND"
        )
    return stmts


def _m3a_postgresql() -> list[str]:
    stmts: list[str] = [
        _pg_function(
            "orch_eva_context_links_guard",
            "  IF TG_OP = 'DELETE' THEN\n"
            f"    {_pg_raise('eva_context_links rows cannot be deleted')}\n"
            "  END IF;\n"
            "  IF "
            + "\n     OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in LINK_FROZEN)
            + "\n     OR (OLD.is_questioned AND NOT NEW.is_questioned) THEN\n"
            f"    {_pg_raise(LINK_MSG)}\n"
            "  END IF;",
        )
    ]
    for table, columns in AUDITED_COLUMNS.items():
        changed = "\n     OR ".join(f"OLD.{c} IS DISTINCT FROM NEW.{c}" for c in columns)
        stmts.append(
            _pg_function(
                f"orch_{table}_content_revision",
                f"  IF ({changed})\n"
                "     AND OLD.content_revision = NEW.content_revision THEN\n"
                "    NEW.content_revision := OLD.content_revision + 1;\n"
                "    NEW.last_producer := 'user_sql';\n"
                "  END IF;",
            )
        )
        inserts = "\n".join(
            f"  IF OLD.{c} IS DISTINCT FROM NEW.{c} THEN\n"
            "    INSERT INTO raw_revision_outbox (tx_id, entity_type, entity_id, field_name, "
            "old_value, new_value, producer, content_revision, captured_at)\n"
            f"    VALUES (v_tx, '{ENTITY_OF[table]}', NEW.id, '{c}', OLD.{c}::text, "
            f"NEW.{c}::text, NEW.last_producer, NEW.content_revision, now());\n"
            "  END IF;"
            for c in columns
        )
        stmts.append(
            f"CREATE OR REPLACE FUNCTION orch_{table}_outbox() RETURNS trigger LANGUAGE plpgsql "
            "AS $fn$\nDECLARE v_tx text := pg_current_xact_id()::text;\nBEGIN\n"
            f"{inserts}\n  RETURN NULL;\nEND;\n$fn$"
        )
        cols = ", ".join(columns)
        stmts += [
            f"CREATE TRIGGER trg_{table}_content_revision BEFORE UPDATE OF {cols} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION orch_{table}_content_revision()",
            f"CREATE TRIGGER trg_{table}_outbox AFTER UPDATE OF {cols} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION orch_{table}_outbox()",
        ]
    for table in M3A_APPEND_ONLY:
        stmts += [
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()",
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()",
        ]
    stmts += [
        "CREATE TRIGGER trg_route_health_no_delete BEFORE DELETE ON route_health "
        "FOR EACH ROW EXECUTE FUNCTION orch_no_delete_guard()",
        "CREATE TRIGGER trg_eva_context_links_guard BEFORE UPDATE OR DELETE ON eva_context_links "
        "FOR EACH ROW EXECUTE FUNCTION orch_eva_context_links_guard()",
    ]
    return stmts


def m3a_statements(dialect: str) -> list[str]:
    """The M3a trigger DDL, per dialect."""
    if dialect == "sqlite":
        return _m3a_sqlite()
    if dialect == "postgresql":
        return _m3a_postgresql()
    raise NotImplementedError(f"no M3a DDL for dialect {dialect!r}")


# --------------------------------------------------------------------------- assembly


def _lineage(dialect: str) -> list[str]:
    if dialect == "sqlite":
        stmts = [s for t in REGISTRY_APPEND_ONLY for s in _sqlite_append_only(t)]
        return stmts + [s for t in LINEAGE for s in _sqlite_lineage(t)]
    stmts = []
    for table in REGISTRY_APPEND_ONLY:
        stmts += [
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON "
            f"{table} FOR EACH ROW EXECUTE FUNCTION orch_append_only_guard()",
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON "
            f"{table} FOR EACH STATEMENT EXECUTE FUNCTION orch_append_only_guard()",
        ]
    return stmts + [s for t in LINEAGE for s in _pg_lineage(t)]


def trigger_statements(dialect: str) -> list[str]:
    """Every trigger (and, on PostgreSQL, every trigger function) of the schema, in install
    order: functions before the triggers that use them. Each trigger appears once."""
    if dialect == "sqlite":
        groups = [_sqlite_statements(), _lineage("sqlite"), _m2_sqlite(), _m21_sqlite()]
        groups += [m3_statements("sqlite"), m3a_statements("sqlite")]
    elif dialect == "postgresql":
        groups = [_postgresql_statements(), _lineage("postgresql"), _m2_postgresql()]
        groups += [_m21_postgresql(), m3_statements("postgresql"), m3a_statements("postgresql")]
    else:
        raise NotImplementedError(f"no DDL for dialect {dialect!r}")
    return [s for group in groups for s in group if not s.startswith("DROP ")]
