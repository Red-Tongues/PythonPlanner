"""R1 fix 4: frozen migrations.

* Revisions never import live application modules (ddl, seed, ORM models).
* Upgrading an empty database one revision at a time yields exactly the triggers, views and
  PL/pgSQL functions recorded in ``migration_manifest.json`` (whitespace-normalised SHA-256).
* The seeded defaults after empty → head are exactly the frozen seed data.

Regenerate the manifest deliberately (after adding a NEW revision) with
``ORCH_UPDATE_MANIFEST=1 pytest tests/test_migrations_frozen.py`` on both dialects.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text

from orchestrator.db import make_engine
from orchestrator.db.migrate import MIGRATIONS_DIR, alembic_config

MANIFEST = Path(__file__).with_name("migration_manifest.json")
VERSIONS = MIGRATIONS_DIR / "versions"


def _digest(sql: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", sql).strip().encode()).hexdigest()[:16]


def fingerprint(connection: Connection) -> dict[str, dict[str, str]]:
    if connection.dialect.name == "sqlite":
        rows = connection.execute(
            text("SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'view')")
        ).all()
        return {
            kind + "s": {r.name: _digest(r.sql) for r in rows if r.type == kind}
            for kind in ("trigger", "view")
        }
    triggers = connection.execute(
        text(
            "SELECT t.tgname AS name, pg_get_triggerdef(t.oid) AS sql FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT t.tgisinternal AND n.nspname = 'public'"
        )
    ).all()
    views = connection.execute(
        text("SELECT viewname AS name, definition AS sql FROM pg_views WHERE schemaname = 'public'")
    ).all()
    functions = connection.execute(
        text(
            "SELECT p.proname AS name, p.prosrc AS sql FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
        )
    ).all()
    return {
        "triggers": {r.name: _digest(r.sql) for r in triggers},
        "views": {r.name: _digest(r.sql) for r in views},
        "functions": {r.name: _digest(r.sql) for r in functions},
    }


def _revisions() -> list[str]:
    script = ScriptDirectory.from_config(alembic_config())
    return [rev.revision for rev in reversed(list(script.walk_revisions()))]


def test_revisions_import_no_live_application_modules() -> None:
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("orchestrator"), f"{path.name}: {node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("orchestrator"), f"{path.name}: {alias.name}"


def test_each_revision_produces_the_recorded_schema_objects(db_url: str) -> None:
    engine = make_engine(db_url)
    cfg = alembic_config()
    observed: dict[str, dict] = {}
    try:
        for rev in _revisions():
            with engine.begin() as connection:
                cfg.attributes["connection"] = connection
                command.upgrade(cfg, rev)
            with engine.connect() as connection:
                observed[rev] = fingerprint(connection)
    finally:
        engine.dispose()

    dialect = "sqlite" if db_url.startswith("sqlite") else "postgresql"
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    if os.environ.get("ORCH_UPDATE_MANIFEST"):
        manifest[dialect] = observed
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        pytest.skip("manifest regenerated")
    assert dialect in manifest, "no manifest for this dialect; regenerate it deliberately"
    assert observed == manifest[dialect]


EXPECTED_SCOPES = {
    ("ingestion", "confirm", False),
    ("objective_llm_values", "confirm", True),
    ("candidate_promotion", "auto", False),
    ("branching", "confirm", False),
    ("routing", "auto", False),
    ("local_generation", "auto", False),
    ("workbench_production", "confirm", False),
    ("repo_write", "confirm", False),
    ("quality_scan", "auto", False),
}
EXPECTED_SETTINGS = {
    ("aggregation.cross_weight", 0.6),
    ("aggregation.self_weight", 0.4),
    ("aggregation.dispute_margin", 0.15),
    ("attempts.quickfire_evaluation", 4),
    ("attempts.local_generation", 3),
    ("attempts.workbench_production", 2),
}
EXPECTED_THRESHOLDS = {
    ("approval_high_ambiguity", "ambiguity_quotient", ">=", "8", "require_approval", 10),
    ("approval_key_field_disputed", "@key_fields", "status_is", "disputed", "require_approval", 10),
    ("split_high_parsability", "task_parsability", ">=", "7", "split", 20),
    ("workbench_coding_intensity", "coding_intensity", ">=", "7", "route_workbench_production", 30),
    (
        "workbench_reasoning_intensity",
        "reasoning_intensity",
        ">=",
        "7",
        "route_workbench_production",
        30,
    ),
    ("workbench_context_volume", "context_volume", ">=", "7", "route_workbench_production", 30),
    ("workbench_output_wordcount", "output_wordcount", ">=", "8", "route_workbench_production", 30),
}


def test_seeds_after_empty_to_head_are_exact(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.connect() as c:
        scopes = {
            (r.stage, r.mode, bool(r.is_fixed))
            for r in c.execute(text("SELECT stage, mode, is_fixed FROM autonomy_scopes"))
        }
        settings = {
            (r.key, json.loads(r.value) if isinstance(r.value, str) else r.value)
            for r in c.execute(text("SELECT key, value FROM settings"))
        }
        thresholds = set(
            c.execute(
                text("SELECT name, field_name, operator, value, action, precedence FROM thresholds")
            ).all()
        )
        control = c.execute(text("SELECT id, paused FROM control_state")).all()
        seed_events = (
            c.execute(text("SELECT entity_type FROM events WHERE action = 'seed.defaults'"))
            .scalars()
            .all()
        )
    assert scopes == EXPECTED_SCOPES
    assert settings == EXPECTED_SETTINGS
    assert {tuple(t) for t in thresholds} == EXPECTED_THRESHOLDS
    assert [(r.id, bool(r.paused)) for r in control] == [(1, False)]
    assert sorted(seed_events) == ["autonomy_scope", "control_state", "setting", "threshold"]
