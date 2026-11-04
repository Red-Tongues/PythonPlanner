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

_LOCKING_SQL = ", ".join(f"'{s.value}'" for s in sorted(LOCKING_STATUSES))


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
