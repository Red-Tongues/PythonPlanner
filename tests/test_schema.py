"""Schema, migrations (A4), seeds and SQLite pragmas (A5)."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session

from orchestrator.contracts import AutonomyMode, Stage
from orchestrator.db import init_db, make_engine
from orchestrator.db.ddl import HEAD_VIEWS
from orchestrator.db.migrate import VERSION_TABLE, current_revision, head_revision, upgrade
from orchestrator.db.models import (
    TABLE_NAMES,
    AutonomyScope,
    Base,
    ControlState,
    Setting,
    Threshold,
)
from orchestrator.db.seed import DEFAULT_AUTONOMY, DEFAULT_SETTINGS, DEFAULT_THRESHOLDS


def test_upgrade_from_empty_reaches_head(db_url: str) -> None:
    engine = make_engine(db_url)
    try:
        assert current_revision(engine) is None
        assert upgrade(engine) == head_revision()
        tables = set(inspect(engine).get_table_names())
        assert set(TABLE_NAMES) | {VERSION_TABLE} <= tables
        assert set(HEAD_VIEWS) <= set(inspect(engine).get_view_names())
        # re-running is a no-op
        assert upgrade(engine) == head_revision()
    finally:
        engine.dispose()


def test_models_and_migrations_do_not_drift(engine: Engine) -> None:
    """The ORM models and the migration history describe the same schema."""
    with engine.connect() as connection:
        ctx = MigrationContext.configure(
            connection, opts={"compare_type": True, "version_table": VERSION_TABLE}
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_init_db_is_idempotent(engine: Engine) -> None:
    summary = init_db(engine)
    assert summary["revision_before"] == summary["revision"] == head_revision()
    assert summary["fields"] == {"inserted": [], "updated": [], "disabled": []}


def test_default_seeds(session: Session) -> None:
    scopes = {s.stage: s for s in session.scalars(select(AutonomyScope))}
    assert set(scopes) == set(Stage)
    assert len(scopes) == 9
    assert scopes[Stage.OBJECTIVE_LLM_VALUES].mode is AutonomyMode.CONFIRM
    assert scopes[Stage.OBJECTIVE_LLM_VALUES].is_fixed
    assert scopes[Stage.INGESTION].mode is AutonomyMode.CONFIRM
    assert scopes[Stage.CANDIDATE_PROMOTION].mode is AutonomyMode.AUTO
    assert {k: v[0] for k, v in DEFAULT_AUTONOMY.items()} == {k: s.mode for k, s in scopes.items()}

    settings = {s.key: s.value for s in session.scalars(select(Setting))}
    assert settings["aggregation.cross_weight"] == 0.6
    assert settings["aggregation.self_weight"] == 0.4
    assert settings["aggregation.dispute_margin"] == 0.15
    assert settings["attempts.quickfire_evaluation"] == 4
    assert settings["attempts.local_generation"] == 3
    assert settings["attempts.workbench_production"] == 2
    assert set(settings) == set(DEFAULT_SETTINGS)

    names = set(session.scalars(select(Threshold.name)))
    assert names == set(DEFAULT_THRESHOLDS)
    split = session.scalar(select(Threshold).where(Threshold.name == "split_high_parsability"))
    assert (split.field_name, split.operator.value, split.value) == ("task_parsability", ">=", "7")
    assert session.get(ControlState, 1).paused is False


def test_seeding_does_not_overwrite_user_values(engine: Engine, session: Session) -> None:
    row = session.get(Setting, "aggregation.dispute_margin")
    row.value = 0.2
    session.commit()
    init_db(engine)
    session.expire_all()
    assert session.get(Setting, "aggregation.dispute_margin").value == 0.2


def test_sqlite_pragmas(db_url: str) -> None:
    if not db_url.startswith("sqlite"):
        return
    engine = make_engine(db_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()
