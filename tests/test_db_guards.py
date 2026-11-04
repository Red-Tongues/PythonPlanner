"""The M3 run, fencing and control schema (C22, C24) and its database-level guards.

Every test runs on SQLite and, with ORCH_TEST_POSTGRES_URL, on PostgreSQL. The triggers are
never disabled; rows in arbitrary starting states are created with raw INSERTs (the transition
triggers guard UPDATEs), then every UPDATE is tried inside a savepoint.
"""

from __future__ import annotations

import itertools

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text

from orchestrator.contracts import GenerationRunState, PacketStatus
from orchestrator.db import init_db, make_engine
from orchestrator.db.ddl import (
    M3_APPEND_ONLY,
    is_immutability_violation,
    is_transition_violation,
    m3_statements,
)
from orchestrator.db.migrate import alembic_config
from orchestrator.sql.views import RUN_VIEWS
from orchestrator.transitions import PACKET_TRANSITIONS, RUN_TRANSITIONS

NOW = "2026-11-04 12:00:00"
LOCKING = {"completed", "archived", "closed_unverified"}


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_the_baseline_carries_the_run_and_control_triggers(dialect: str) -> None:
    from tests.test_schema import baseline

    statements = baseline().STATEMENTS[dialect]
    assert all(s in statements for s in m3_statements(dialect))


def test_the_run_attempts_view_is_installed_and_answers(engine: Engine) -> None:
    from tests.test_schema import baseline

    assert set(RUN_VIEWS) <= set(baseline().VIEWS)
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM v_run_attempts")).scalar() == 0


# --------------------------------------------------------------------------- raw helpers


def _project(c: Connection, pid: str = "p1") -> None:
    c.execute(
        text(
            "INSERT INTO projects (id, ref, name, brief, sectors, core_values, goals,"
            " reference_list, source, version, locked, created_at, updated_at) "
            "VALUES (:id, :ref, 'Legacy', '', '[]', '[]', '[]', '[]', 'm', 1, :f, :t, :t)"
        ),
        {"id": pid, "ref": f"P-{pid}", "t": NOW, "f": False},
    )


def _packet(c: Connection, pid: str, status: str, ref: str, project: str = "p1") -> None:
    locked = status in LOCKING
    c.execute(
        text(
            "INSERT INTO packets (id, ref, project_id, position, packet_kind, title,"
            " prompt_description, response_structure, response_contract, manual_boost, status,"
            " evaluations_count, locked, completed_at, version, depth, source, created_at,"
            " updated_at, gate_revision) VALUES (:id, :ref, :p, 0, 'task', 'T', 'x',"
            " 'markdown_document', 'markdown_document@1.0', 0, :s, 0, :l, :c, 1, 0, 'm', :t,"
            " :t, 0)"
        ),
        {
            "id": pid,
            "ref": ref,
            "p": project,
            "s": status,
            "l": locked,
            "c": NOW if locked else None,
            "t": NOW,
        },
    )


def _run(c: Connection, rid: str, packet: str, state: str, **extra: object) -> None:
    columns = {
        "id": rid,
        "packet_id": packet,
        "packet_version": 1,
        "input_hash": "h",
        "claimed_by": "k",
        "state": state,
        "evaluation_snapshot": "{}",
        "created_at": NOW,
        "updated_at": NOW,
        **extra,
    }
    names = ", ".join(columns)
    values = ", ".join(f":{k}" for k in columns)
    c.execute(text(f"INSERT INTO generation_runs ({names}) VALUES ({values})"), columns)


def _try(c: Connection, sql: str, **params: object) -> BaseException | None:
    savepoint = c.begin_nested()
    try:
        c.execute(text(sql), params)
    except Exception as exc:  # the trigger fired
        savepoint.rollback()
        return exc
    savepoint.rollback()  # never keep the change: every attempt starts from the same row
    return None


# --------------------------------------------------------------------------- transition triggers


def test_db_rejects_every_packet_status_change_not_in_the_table(engine: Engine) -> None:
    with engine.connect() as c, c.begin():
        _project(c)
        for i, status in enumerate(PacketStatus):
            _packet(c, f"k{i}", status.value, f"PP-{i:06d}")
        for (i, src), dst in itertools.product(enumerate(PacketStatus), PacketStatus):
            if src is dst:
                continue
            locks = dst.value in LOCKING
            error = _try(
                c,
                "UPDATE packets SET status = :s, locked = :l, completed_at = :ca WHERE id = :id",
                s=dst.value,
                l=locks or src.value in LOCKING,
                ca=NOW if locks or src.value in LOCKING else None,
                id=f"k{i}",
            )
            allowed = (src.value, dst.value) in PACKET_TRANSITIONS
            if allowed:
                assert error is None, (src, dst, error)
            else:
                assert error is not None, (src, dst)
                assert is_transition_violation(error) or is_immutability_violation(error), error


def test_db_rejects_every_run_state_change_not_in_the_table(engine: Engine) -> None:
    with engine.connect() as c, c.begin():
        _project(c)
        _packet(c, "k1", "running", "PP-000001")
        for i, state in enumerate(GenerationRunState):
            _run(c, f"r{i}", "k1", state.value, gate_revision=0, fencing_token=1)
        for (i, src), dst in itertools.product(enumerate(GenerationRunState), GenerationRunState):
            if src is dst:
                continue
            error = _try(
                c, "UPDATE generation_runs SET state = :s WHERE id = :id", s=dst, id=f"r{i}"
            )
            if (src.value, dst.value) in RUN_TRANSITIONS:
                assert error is None, (src, dst, error)
            else:
                assert error is not None, (src, dst)


def test_db_run_bindings_are_frozen_and_final(engine: Engine) -> None:
    with engine.connect() as c, c.begin():
        _project(c)
        _packet(c, "k1", "running", "PP-000001")
        _run(
            c, "r1", "k1", "accepted", gate_revision=4, fencing_token=7, channel="local_generation"
        )
        upd = "UPDATE generation_runs SET {} WHERE id = 'r1'"
        for change in (
            "fencing_token = 8",
            "channel = 'workbench_production'",
            "claimed_by = 'other'",
            "gate_revision = 6",  # +2
            "gate_revision = 3",  # backwards
        ):
            error = _try(c, upd.format(change))
            assert error is not None and is_immutability_violation(error), change
        assert _try(c, upd.format("gate_revision = 5")) is None  # R1: +1 only
        c.execute(text(upd.format("manifest_hash = 'm1', commit_sha = 'c1'")))
        # recorded commit → the accepted run is final (R4): not even stale, not the hashes
        for change in ("state = 'stale'", "commit_sha = 'c2'", "heartbeat_at = NULL"):
            error = _try(c, upd.format(change))
            assert error is not None and is_immutability_violation(error), change
        _run(c, "r2", "k1", "failed")
        assert is_immutability_violation(
            _try(c, "UPDATE generation_runs SET stale_reason = 'x' WHERE id = 'r2'")
        )
        _run(c, "r3", "k1", "accepted", gate_revision=0)
        assert _try(c, "UPDATE generation_runs SET state = 'stale' WHERE id = 'r3'") is None


def test_db_new_append_only_tables_reject_update_and_delete(engine: Engine, tree) -> None:  # type: ignore[no-untyped-def]
    with engine.connect() as c, c.begin():
        pid = tree.packets[0].id
        c.execute(
            text(
                "INSERT INTO channel_runs (id, run_id, channel, packet_id, attempt, max_attempts,"
                " contract, state, status, created_at, updated_at, operation_id) VALUES ('c1',"
                " 'LG000001', 'local_generation', :p, 1, 3, 'x@1', 'dispatched', 'running', :t,"
                " :t, 'op-1')"
            ),
            {"p": pid, "t": NOW},
        )
        c.execute(
            text(
                "INSERT INTO channel_run_outcomes (id, channel_run_id, outcome, created_at) "
                "VALUES ('o1', 'c1', 'succeeded', :t)"
            ),
            {"t": NOW},
        )
        c.execute(
            text(
                "INSERT INTO governor_events (id, scope_type, scope_id, signal, observed, action,"
                " details, created_at) VALUES ('g1', 'global', '*', 's', 'clean', 'log', '{}', :t)"
            ),
            {"t": NOW},
        )
        c.execute(
            text(
                "INSERT INTO budget_counters (id, scope_type, scope_id, metric, period,"
                " period_start, used, updated_at) VALUES ('b1', 'channel', 'local_generation',"
                " 'runs', 'hour', :t, 1, :t)"
            ),
            {"t": NOW},
        )
        c.execute(
            text(
                "INSERT INTO budget_reservations (id, operation_id, counter_id, amount,"
                " created_at) VALUES ('br1', 'op-1', 'b1', 1, :t)"
            ),
            {"t": NOW},
        )
        c.execute(
            text(
                "INSERT INTO budget_reconciliations (id, reservation_id, actual, created_at)"
                " VALUES ('rc1', 'br1', 1, :t)"
            ),
            {"t": NOW},
        )
        c.execute(
            text(
                "INSERT INTO budget_topups (id, scope_type, scope_id, metric, amount, actor,"
                " reason, created_at) VALUES ('t1', 'objective', 'o', 'tokens', 5, 'user:x', 'r',"
                " :t)"
            ),
            {"t": NOW},
        )
        ids = {
            "channel_runs": "c1",
            "channel_run_outcomes": "o1",
            "governor_events": "g1",
            "budget_reservations": "br1",
            "budget_reconciliations": "rc1",
            "budget_topups": "t1",
        }
        assert set(ids) == set(M3_APPEND_ONLY)
        for table, row in ids.items():
            for sql in (
                f"UPDATE {table} SET created_at = created_at WHERE id = :id",
                f"DELETE FROM {table} WHERE id = :id",
            ):
                error = _try(c, sql, id=row)
                assert error is not None and is_immutability_violation(error), (table, sql)
        assert is_immutability_violation(_try(c, "DELETE FROM budget_counters WHERE id = 'b1'"))
        if engine.dialect.name == "postgresql":
            for table in (*M3_APPEND_ONLY, "budget_counters", "kernel_leases", "governor_states"):
                error = _try(c, f"TRUNCATE {table} CASCADE")
                assert error is not None and is_immutability_violation(error), table


def test_db_control_rows_only_move_forward(engine: Engine) -> None:
    with engine.connect() as c, c.begin():
        c.execute(
            text(
                "INSERT INTO kernel_leases (name, holder, epoch, updated_at) "
                "VALUES ('kernel', 'a', 3, :t)"
            ),
            {"t": NOW},
        )
        c.execute(
            text(
                "INSERT INTO governor_states (id, scope_type, scope_id, status, revision,"
                " created_at, updated_at) VALUES ('s1', 'global', '*', 'normal', 2, :t, :t)"
            ),
            {"t": NOW},
        )
        assert is_immutability_violation(_try(c, "UPDATE kernel_leases SET epoch = 2"))
        assert is_immutability_violation(_try(c, "DELETE FROM kernel_leases"))
        assert _try(c, "UPDATE kernel_leases SET epoch = 4, holder = 'b'") is None
        assert is_immutability_violation(_try(c, "UPDATE governor_states SET status = 'paused'"))
        assert is_immutability_violation(_try(c, "DELETE FROM governor_states"))
        assert _try(c, "UPDATE governor_states SET status = 'paused', revision = 3") is None


# --------------------------------------------------------------------------- populated database


def test_a_populated_database_survives_init_db(db_url: str) -> None:
    """Re-running ``init-db`` on a database with history (C22's intent, now that there is one
    baseline): IDs, hashes, lineage, statuses and immutable rows are unchanged; new content_eval
    rows are still rejected; legacy M2 runs stay valid and can only go stale."""
    engine = make_engine(db_url)
    try:
        init_db(engine)
        with engine.begin() as c:
            _project(c)
            _packet(c, "k1", "running", "PP-000001")
            _packet(c, "k2", "completed", "PP-000002")
            c.execute(
                text(
                    "INSERT INTO packets (id, ref, project_id, position, packet_kind, title,"
                    " prompt_description, response_structure, response_contract, manual_boost,"
                    " status, evaluations_count, locked, version, depth, source, created_at,"
                    " updated_at, gate_revision, supersedes_id, input_hash) VALUES ('k3',"
                    " 'PP-000001', 'p1', 0, 'task', 'T2', 'y', 'markdown_document',"
                    " 'markdown_document@1.0', 0, 'draft', 0, :f, 2, 0, 'm', :t, :t, 5, 'k1',"
                    " 'hash-k3')"
                ),
                {"t": NOW, "f": False},
            )
            _run(c, "r1", "k1", "claimed")
            c.execute(
                text(
                    "INSERT INTO channel_runs (id, run_id, channel, packet_id, attempt,"
                    " max_attempts, contract, state, status, created_at, updated_at) VALUES"
                    " ('c1', 'QE000001', 'quickfire_evaluation', 'k1', 1, 4, 'x@1', 'accepted',"
                    " 'succeeded', :t, :t)"
                ),
                {"t": NOW},
            )
            before = c.execute(
                text(
                    "SELECT id, ref, version, supersedes_id, status, locked, input_hash, "
                    "gate_revision FROM packets ORDER BY id"
                )
            ).all()
        init_db(engine)
        with engine.connect() as c, c.begin():
            after = c.execute(
                text(
                    "SELECT id, ref, version, supersedes_id, status, locked, input_hash, "
                    "gate_revision FROM packets ORDER BY id"
                )
            ).all()
            assert after == before
            assert c.execute(text("SELECT fencing_token FROM packets")).scalars().all() == [0] * 3
            run = c.execute(
                text("SELECT state, fencing_token, attempts FROM generation_runs")
            ).one()
            assert tuple(run) == ("claimed", None, 0)
            assert c.execute(text("SELECT state FROM channel_runs")).scalar() == "accepted"
            assert is_immutability_violation(
                _try(c, "UPDATE packets SET title = 'z' WHERE id = 'k2'")
            )
            assert is_immutability_violation(
                _try(c, "UPDATE channel_runs SET status = 'failed' WHERE id = 'c1'")
            )
            assert is_transition_violation(
                _try(c, "UPDATE generation_runs SET state = 'dispatched' WHERE id = 'r1'")
            )
            assert _try(c, "UPDATE generation_runs SET state = 'stale' WHERE id = 'r1'") is None
            error = _try(
                c,
                "INSERT INTO packets (id, ref, project_id, position, packet_kind, title,"
                " prompt_description, response_structure, response_contract, manual_boost,"
                " status, evaluations_count, locked, version, depth, source, created_at,"
                " updated_at, gate_revision) VALUES ('k9', 'PP-000009', 'p1', 0, 'content_eval',"
                " 'E', 'x', 'markdown_document', 'markdown_document@1.0', 0, 'draft', 0, :f, 1,"
                " 0, 'm', :t, :t, 0)",
                t=NOW,
                f=False,
            )
            assert error is not None and is_immutability_violation(error)
    finally:
        engine.dispose()


def test_downgrade_refuses_while_runs_have_history(db_url: str) -> None:
    """A database whose runs left an audit trail is never dropped by a downgrade."""
    engine = make_engine(db_url)
    cfg = alembic_config()
    try:
        with engine.begin() as c:
            cfg.attributes["connection"] = c
            command.upgrade(cfg, "head")
        with engine.begin() as c:
            _project(c)
            _packet(c, "k1", "running", "PP-000001")
            _run(c, "r1", "k1", "planned", fencing_token=1, gate_revision=0)
            c.execute(
                text(
                    "INSERT INTO events (id, actor, action, entity_type, entity_id, created_at)"
                    " VALUES ('e1', 'system:kernel', 'run.transition', 'generation_run', 'r1', :t)"
                ),
                {"t": NOW},
            )
        with pytest.raises(RuntimeError, match="events exist"), engine.begin() as c:
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "base")
        with engine.connect() as c:
            assert c.execute(text("SELECT state FROM generation_runs")).scalar() == "planned"
    finally:
        engine.dispose()
