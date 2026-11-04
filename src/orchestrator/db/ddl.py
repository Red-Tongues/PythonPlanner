"""All dialect-specific DDL lives here (CLAUDE.md golden rule 7): triggers and views.

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
* ``evaluation_passes``, ``events``: UPDATE and DELETE always rejected (PostgreSQL also TRUNCATE).
* ``autonomy_scopes``: fixed scopes (``is_fixed``) cannot be changed or deleted.

Views (portable SQL, same text on both dialects): ``v_project_heads``, ``v_objective_heads``,
``v_packet_heads`` — for every row, the current head of its lineage, following ``supersedes_id``.

Since R1 fix 4 the migration revisions hold **frozen copies** of this SQL and never import this
module (``tests/test_migrations_frozen.py`` checks both the imports and the resulting triggers,
views and functions per revision). This module is the *authoring* reference: to change a rule,
edit it here, then write a NEW revision containing the literal statements and regenerate the
manifest. ``is_immutability_violation`` is the runtime helper the CLI uses.
"""

from __future__ import annotations

import logging

from sqlalchemy import Connection

from orchestrator.contracts import LOCKING_STATUSES

log = logging.getLogger(__name__)

IMMUTABLE_MARKER = "orchestrator-immutable"
APPEND_ONLY_TABLES: tuple[str, ...] = ("evaluation_passes", "events")
LOCKABLE_TABLES: tuple[str, ...] = ("projects", "objectives", "packets")
HEAD_VIEWS: dict[str, str] = {
    "v_project_heads": "projects",
    "v_objective_heads": "objectives",
    "v_packet_heads": "packets",
}

#: The locking statuses as frozen in 0002. ``closed_unverified`` (M2.1) is guarded by its own
#: trigger in 0006 (``m21_statements``), so 0002's reference text never changes.
_LOCKING_SQL = "'archived', 'completed'"
assert {"archived", "completed"} <= {s.value for s in LOCKING_STATUSES}


# --------------------------------------------------------------------------- views (portable)


def _head_view(view: str, table: str) -> str:
    """Each row → head of its lineage. ``is_head`` is 1/0 on SQLite, true/false on PostgreSQL."""
    return (
        f"CREATE VIEW {view} AS\n"
        "WITH RECURSIVE chain(row_id, current_id) AS (\n"
        f"  SELECT id, id FROM {table}\n"
        "  UNION ALL\n"
        f"  SELECT chain.row_id, nxt.id FROM chain JOIN {table} nxt "
        "ON nxt.supersedes_id = chain.current_id\n"
        ")\n"
        "SELECT chain.row_id AS id, head.id AS head_id, head.ref AS head_ref,\n"
        "       head.version AS head_version, (chain.row_id = head.id) AS is_head\n"
        f"FROM chain JOIN {table} head ON head.id = chain.current_id\n"
        f"WHERE NOT EXISTS (SELECT 1 FROM {table} s WHERE s.supersedes_id = head.id)"
    )


def _view_statements() -> list[str]:
    stmts: list[str] = []
    for view, table in HEAD_VIEWS.items():
        stmts += [f"DROP VIEW IF EXISTS {view}", _head_view(view, table)]
    return stmts


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
            "completed/archived packets must be locked with completed_at set",
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
            f"    {_pg_raise('completed/archived packets must be locked with completed_at set')}\n"
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


# --------------------------------------------------------------------------- entry points


def statements_for(dialect: str) -> list[str]:
    if dialect == "sqlite":
        return _sqlite_statements() + _view_statements()
    if dialect == "postgresql":
        return _postgresql_statements() + _view_statements()
    raise NotImplementedError(f"no immutability DDL for dialect {dialect!r}")


def install(connection: Connection) -> int:
    """Install (or reinstall) all triggers and views. Idempotent. Returns the statement count."""
    stmts = statements_for(connection.dialect.name)
    # A raw DBAPI cursor called *without* parameters: neither SQLAlchemy nor psycopg then parse
    # the '%' (RAISE format) or ':' characters inside trigger bodies as placeholders.
    cursor = connection.connection.cursor()
    try:
        for stmt in stmts:
            cursor.execute(stmt)
    finally:
        cursor.close()
    log.info("installed %d %s DDL statements", len(stmts), connection.dialect.name)
    return len(stmts)


def is_immutability_violation(exc: BaseException) -> bool:
    """True if a database error was raised by one of these triggers."""
    return IMMUTABLE_MARKER in str(exc)


# --------------------------------------------------------------------------- M2.1 (revision 0006)
# Authoring reference for the triggers in migration 0006 (review R2, fixes 5 and 6). The
# revision holds frozen literal copies; tests/test_sql_views.py asserts they are equal.

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
#: PostgreSQL tables that get a statement-level BEFORE TRUNCATE guard in 0006
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
    """Trigger DDL installed by migration 0006, per dialect."""
    if dialect == "sqlite":
        return _m21_sqlite()
    if dialect == "postgresql":
        return _m21_postgresql()
    raise NotImplementedError(f"no M2.1 DDL for dialect {dialect!r}")


# --------------------------------------------------------------------------- M3 (revision 0007)
# Authoring reference for the triggers in migration 0007 (C22–C26). The revision holds frozen
# literal copies; tests/test_m3_schema.py asserts they are equal.

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
RUN_FROZEN: tuple[str, ...] = ("claimed_by", "channel", "fencing_token", "created_at")
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
    """Trigger DDL installed by migration 0007, per dialect, from the M3 transition tables."""
    from orchestrator.transitions import packet_pairs, run_pairs

    if dialect == "sqlite":
        return _m3_sqlite(packet_pairs(), run_pairs())
    if dialect == "postgresql":
        return _m3_postgresql(packet_pairs(), run_pairs())
    raise NotImplementedError(f"no M3 DDL for dialect {dialect!r}")


def is_transition_violation(exc: BaseException) -> bool:
    """True if a database error was raised by the 0007 transition-table triggers."""
    return TRANSITION_MARKER in str(exc)


#: D10 Q3: the console's view of each generation run's latest attempt and its outcome
#: (portable SQL, identical on both dialects; frozen in 0007).
M3_VIEWS: dict[str, str] = {
    "v_run_attempts": (
        "CREATE VIEW v_run_attempts AS\n"
        "SELECT r.id AS generation_run_id, r.packet_id, r.packet_version, r.state, r.channel,\n"
        "       r.fencing_token, r.attempts, r.max_attempts, c.id AS channel_run_id, c.run_id,\n"
        "       c.attempt, c.operation_id, c.created_at AS authorised_at, o.outcome,\n"
        "       o.exit_code, o.error, o.created_at AS outcome_at\n"
        "FROM generation_runs r\n"
        "LEFT JOIN channel_runs c ON c.generation_run_id = r.id\n"
        "  AND NOT EXISTS (SELECT 1 FROM channel_runs c2\n"
        "                  WHERE c2.generation_run_id = r.id AND c2.attempt > c.attempt)\n"
        "LEFT JOIN channel_run_outcomes o ON o.channel_run_id = c.id"
    ),
}
