"""Seeded defaults (GE6): exact on a fresh database; init-db re-seeds what is missing."""

from __future__ import annotations

import json

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from orchestrator.db import init_db
from orchestrator.db.models import Event
from orchestrator.db.seed import (
    AUTONOMY_SCOPES,
    M3A_PRESETS,
    SETTINGS,
    SETTINGS_M3A,
    THRESHOLDS,
    all_presets,
    ensure_defaults,
)
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
    assert presets == {tuple(p) for p in all_presets()}


def test_init_db_restores_deleted_defaults_and_keeps_edits(
    engine: Engine, session: Session
) -> None:
    set_setting(session, "queue.lease_minutes", 45, actor=USER, reason="longer leases")
    session.commit()
    with engine.begin() as c:
        c.execute(
            text("DELETE FROM settings WHERE key IN ('consensus.winner_min', 'admission.floor_e4')")
        )
        c.execute(text("DELETE FROM thresholds WHERE name = 'split_high_parsability'"))
        c.execute(text("DELETE FROM autonomy_scopes WHERE stage = 'routing'"))
        c.execute(text("DELETE FROM control_state"))
    seen = set(session.scalars(select(Event.id)))  # the fresh database's own seeding events
    summary = init_db(engine)
    assert summary["reseeded"] == {
        "autonomy_scope": ["routing"],
        "setting": ["consensus.winner_min", "admission.floor_e4"],
        "threshold": ["split_high_parsability"],
        "control_state": ["1"],
    }
    assert init_db(engine)["reseeded"] == {}  # idempotent
    session.expire_all()
    with engine.connect() as c:
        budget = c.execute(text("SELECT value FROM settings WHERE key = 'queue.lease_minutes'"))
        assert _value(budget.scalar_one()) == 45  # the user's edit survives
    events = [
        e
        for e in session.scalars(
            select(Event).where(
                Event.action == "seed.defaults", Event.reason.like("init-db re-seeded%")
            )
        )
        if e.id not in seen
    ]
    assert {e.entity_type for e in events} == {
        "autonomy_scope",
        "setting",
        "threshold",
        "control_state",
    }


# --------------------------------------------------------------------------- exact seeds

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
    # 0005 (M2)
    ("consensus.agreement_min", 0.6),
    ("consensus.winner_min", 0.7),
    ("consensus.one_sided_min", 0.7),
    ("consensus.min_supports", 1),
    ("evaluation.repair_attempts", 1),
    ("evaluation.max_escalations", 1),
    ("evaluation.num_ctx", 4096),
    ("evaluation.max_tokens", 512),
    ("evaluation.max_exemplars", 3),
    ("queue.lease_minutes", 30),
    ("branching.max_depth", 2),
    ("branching.max_descendants", 12),
    ("stats.score_threshold", 7),
} | {(key, value) for key, value, _ in SETTINGS_M3A}  # M3a settings (R6)
#: 0005 eva presets: (field, phase, tier, required, hard, gate operator, gate value)
EXPECTED_GATING_PRESETS = {
    ("safety_clear", "pre", 1, True, True, "=", "true"),
    ("ambiguity_quotient", "pre", 1, True, False, None, None),
    ("task_parsability", "pre", 1, True, False, None, None),
    ("artifact_request_classes", "pre", 2, True, False, None, None),
    ("tools_needed", "pre", 2, True, False, None, None),
    ("coding_intensity", "pre", 2, True, False, None, None),
    ("reasoning_intensity", "pre", 2, True, False, None, None),
    ("context_volume", "pre", 2, True, False, None, None),
    ("output_wordcount", "pre", 2, True, False, None, None),
    ("requirement_alignment", "post", 1, True, False, ">=", "6"),
    ("correctness", "post", 1, True, False, ">=", "6"),
    ("security_risk", "post", 1, True, True, "<=", "6"),
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


def test_seeds_on_a_fresh_database_are_exact(engine) -> None:  # type: ignore[no-untyped-def]
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
    # one seeding path (``ensure_defaults``): one batch event per seeded table
    assert sorted(seed_events) == [
        "autonomy_scope",
        "control_state",
        "eva_preset",
        "setting",
        "threshold",
    ]
    with engine.connect() as c:
        presets = c.execute(
            text(
                "SELECT field_name, phase, tier, required, hard, gate_operator, gate_value, plan, "
                "revision FROM eva_presets"
            )
        ).all()
    assert len(presets) == 46 and {(p.plan, p.revision) for p in presets} == {("combined", 1)}
    gating = {(p[0], p[1], p[2], bool(p[3]), bool(p[4]), p[5], p[6]) for p in presets if p[2] < 3}
    assert gating == EXPECTED_GATING_PRESETS | {
        (f, ph, *spec) for (f, ph), spec in M3A_PRESETS.items() if spec[0] < 3
    }
