"""R1 deferral GE6: init-db idempotently re-seeds every default; edits survive."""

from __future__ import annotations

import json

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from orchestrator.db import init_db
from orchestrator.db.models import Event
from orchestrator.db.seed import AUTONOMY_SCOPES, PRESETS, SETTINGS, THRESHOLDS, ensure_defaults
from orchestrator.repository import set_setting
from tests.conftest import USER


def _value(v):  # type: ignore[no-untyped-def]
    return json.loads(v) if isinstance(v, str) else v


def test_live_defaults_equal_the_frozen_migration_seeds(engine: Engine, session: Session) -> None:
    assert ensure_defaults(session) == {}  # a fresh database already has every default
    with engine.connect() as c:
        settings = {
            (r.key, _value(r.value)) for r in c.execute(text("SELECT key, value FROM settings"))
        }
        scopes = {tuple(r) for r in c.execute(text("SELECT stage, mode FROM autonomy_scopes"))}
        thresholds = {
            tuple(r)
            for r in c.execute(text("SELECT name, field_name, operator, value FROM thresholds"))
        }
        presets = {
            (
                r.field_name,
                r.phase,
                r.tier,
                bool(r.required),
                bool(r.hard),
                r.gate_operator,
                r.gate_value,
            )
            for r in c.execute(text("SELECT * FROM eva_presets"))
        }
    assert settings == {(k, v) for k, v, _ in SETTINGS}
    assert scopes == {(s, m) for s, m, _, _ in AUTONOMY_SCOPES}
    assert thresholds == {(n, f, o, v) for n, f, o, v, *_ in THRESHOLDS}
    assert presets == {(f, *p) for f, p in PRESETS.items()}


def test_init_db_restores_deleted_defaults_and_keeps_edits(
    engine: Engine, session: Session
) -> None:
    set_setting(session, "queue.failure_budget", 5, actor=USER, reason="tolerate more")
    session.commit()
    with engine.begin() as c:
        c.execute(
            text("DELETE FROM settings WHERE key IN ('consensus.winner_min', 'priority.w_age')")
        )
        c.execute(text("DELETE FROM thresholds WHERE name = 'split_high_parsability'"))
        c.execute(text("DELETE FROM autonomy_scopes WHERE stage = 'routing'"))
        c.execute(text("DELETE FROM control_state"))
    summary = init_db(engine)
    assert summary["reseeded"] == {
        "autonomy_scope": ["routing"],
        "setting": ["consensus.winner_min", "priority.w_age"],
        "threshold": ["split_high_parsability"],
        "control_state": ["1"],
    }
    assert init_db(engine)["reseeded"] == {}  # idempotent
    session.expire_all()
    with engine.connect() as c:
        budget = c.execute(text("SELECT value FROM settings WHERE key = 'queue.failure_budget'"))
        assert _value(budget.scalar_one()) == 5  # the user's edit survives
    events = session.scalars(
        select(Event).where(
            Event.action == "seed.defaults", Event.reason.like("init-db re-seeded%")
        )
    ).all()
    assert {e.entity_type for e in events} == {
        "autonomy_scope",
        "setting",
        "threshold",
        "control_state",
    }
