"""Seed defaults: autonomy scopes (§11), settings, threshold rules (§8), control state.

FROZEN (R1 fix 4): the seed rows are literal data and the tables are local ``sa.table``
definitions, so this revision never depends on the live ORM or seed modules. Never edit this
file; put changes in a new revision.

Only *missing* rows are inserted, so values the user has edited survive a replay, and each table
that receives rows gets one ``seed.defaults`` event. The candidate-field registry is synced from
YAML by ``init-db`` after migrations (the YAML is the source of truth for field definitions).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR = "system:init-db"

# (stage, mode, is_fixed, description) — PROJECT_PLAN §11
AUTONOMY_SCOPES = (
    ("ingestion", "confirm", False, "User approves the extracted tree"),
    (
        "objective_llm_values",
        "confirm",
        True,
        "LLM values at objective level must always be confirmed manually (fixed)",
    ),
    ("candidate_promotion", "auto", False, "Deterministic SQL"),
    ("branching", "confirm", False, "Splitting packets into children"),
    ("routing", "auto", False, "Channel selection"),
    ("local_generation", "auto", False, "Simple local-model generation"),
    ("workbench_production", "confirm", False, "Costly/complex work"),
    ("repo_write", "confirm", False, "Until trust is established"),
    ("quality_scan", "auto", False, "SonarQube scan"),
)

# (key, value, description)
SETTINGS = (
    ("aggregation.cross_weight", 0.6, "Weight of the cross-check score in model confidence"),
    ("aggregation.self_weight", 0.4, "Weight of the self-check score in model confidence"),
    (
        "aggregation.dispute_margin",
        0.15,
        "Confidence margin below which a disagreement is disputed",
    ),
    ("attempts.quickfire_evaluation", 4, "Max attempts per Quickfire Evaluation run (QE)"),
    ("attempts.local_generation", 3, "Max attempts per Local Generation run (LG)"),
    ("attempts.workbench_production", 2, "Max attempts per Workbench Production run (WP)"),
)

# (name, field_name, operator, value, action, precedence, description) — PROJECT_PLAN §8
THRESHOLDS = (
    (
        "approval_high_ambiguity",
        "ambiguity_quotient",
        ">=",
        "8",
        "require_approval",
        10,
        "Highly ambiguous prompts go to the approval queue before routing",
    ),
    (
        "approval_key_field_disputed",
        "@key_fields",
        "status_is",
        "disputed",
        "require_approval",
        10,
        "Any disputed key field goes to the approval queue before routing",
    ),
    (
        "split_high_parsability",
        "task_parsability",
        ">=",
        "7",
        "split",
        20,
        "Split into independent subtasks (child packets via subtask_list)",
    ),
    (
        "workbench_coding_intensity",
        "coding_intensity",
        ">=",
        "7",
        "route_workbench_production",
        30,
        "Code-heavy work goes to the workbench",
    ),
    (
        "workbench_reasoning_intensity",
        "reasoning_intensity",
        ">=",
        "7",
        "route_workbench_production",
        30,
        "Complex reasoning goes to the workbench",
    ),
    (
        "workbench_context_volume",
        "context_volume",
        ">=",
        "7",
        "route_workbench_production",
        30,
        "Large-context work goes to the workbench",
    ),
    (
        "workbench_output_wordcount",
        "output_wordcount",
        ">=",
        "8",
        "route_workbench_production",
        30,
        "Lengthy outputs go to the workbench",
    ),
)


def _ts() -> tuple[sa.ColumnClause, sa.ColumnClause]:
    return (
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


autonomy_scopes = sa.table(
    "autonomy_scopes",
    sa.column("stage", sa.String),
    sa.column("mode", sa.String),
    sa.column("is_fixed", sa.Boolean),
    sa.column("description", sa.Text),
    *_ts(),
)
settings = sa.table(
    "settings",
    sa.column("key", sa.String),
    sa.column("value", sa.JSON),
    sa.column("description", sa.Text),
    sa.column("version", sa.Integer),
    *_ts(),
)
thresholds = sa.table(
    "thresholds",
    sa.column("id", sa.String),
    sa.column("name", sa.String),
    sa.column("field_name", sa.String),
    sa.column("operator", sa.String),
    sa.column("value", sa.String),
    sa.column("action", sa.String),
    sa.column("precedence", sa.Integer),
    sa.column("enabled", sa.Boolean),
    sa.column("description", sa.Text),
    *_ts(),
)
control_state = sa.table(
    "control_state",
    sa.column("id", sa.Integer),
    sa.column("paused", sa.Boolean),
    *_ts(),
)
events = sa.table(
    "events",
    sa.column("id", sa.String),
    sa.column("actor", sa.String),
    sa.column("action", sa.String),
    sa.column("entity_type", sa.String),
    sa.column("after", sa.JSON),
    sa.column("reason", sa.Text),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def _existing(table: sa.TableClause, column: str) -> set:
    return set(op.get_bind().execute(sa.select(table.c[column])).scalars())


def _seed(table: sa.TableClause, key: str, rows: list[dict], entity_type: str) -> None:
    present = _existing(table, key)
    missing = [r for r in rows if r[key] not in present]
    if not missing:
        return
    op.bulk_insert(table, missing)
    op.bulk_insert(
        events,
        [
            {
                "id": str(uuid.uuid4()),
                "actor": ACTOR,
                "action": "seed.defaults",
                "entity_type": entity_type,
                "after": {"inserted": [str(r[key]) for r in missing]},
                "reason": "init-db default seeding",
                "created_at": datetime.now(UTC),
            }
        ],
    )


def upgrade() -> None:
    now = datetime.now(UTC)
    ts = {"created_at": now, "updated_at": now}
    _seed(
        autonomy_scopes,
        "stage",
        [
            {"stage": s, "mode": m, "is_fixed": f, "description": d, **ts}
            for s, m, f, d in AUTONOMY_SCOPES
        ],
        "autonomy_scope",
    )
    _seed(
        settings,
        "key",
        [{"key": k, "value": v, "description": d, "version": 1, **ts} for k, v, d in SETTINGS],
        "setting",
    )
    _seed(
        thresholds,
        "name",
        [
            {
                "id": str(uuid.uuid4()),
                "name": n,
                "field_name": f,
                "operator": o,
                "value": v,
                "action": a,
                "precedence": p,
                "enabled": True,
                "description": d,
                **ts,
            }
            for n, f, o, v, a, p, d in THRESHOLDS
        ],
        "threshold",
    )
    _seed(control_state, "id", [{"id": 1, "paused": False, **ts}], "control_state")


def downgrade() -> None:
    # Seed rows are governance state: they stay (re-upgrading inserts only missing rows) and are
    # removed with their tables by 0001's downgrade. Events are append-only by design.
    pass
