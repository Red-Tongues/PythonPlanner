"""Database layer: schema, dialect DDL, sessions, refs and seeding."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Engine

from orchestrator.db import migrate
from orchestrator.db.seed import ensure_defaults, record_registry_revision, sync_candidate_fields
from orchestrator.db.session import make_engine, make_session_factory, session_scope

log = logging.getLogger(__name__)


def init_db(engine: Engine) -> dict[str, Any]:
    """Bring the database to the head revision and sync the candidate-field registry.

    The baseline migration (A4) builds the master schema (``orchestrator.db.schema``: tables,
    triggers, views); then every missing default is seeded (GE6).
    Idempotent: safe to run on every start.
    """
    from orchestrator.fields import load_candidate_fields

    before = migrate.current_revision(engine)
    after = migrate.upgrade(engine)
    factory = make_session_factory(engine)
    definitions = load_candidate_fields()
    with session_scope(factory) as session:
        reseeded = ensure_defaults(session)
        fields = sync_candidate_fields(session, definitions)
        registry = record_registry_revision(session, definitions)
        registry_revision = registry.revision
    summary: dict[str, Any] = {
        "revision_before": before,
        "revision": after,
        "fields": fields,
        "registry_revision": registry_revision,
        "reseeded": reseeded,
    }
    log.info("database initialised: %s", summary)
    return summary


__all__ = ["init_db", "make_engine", "make_session_factory", "session_scope"]
