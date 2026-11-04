"""Idempotent seeding of defaults and the candidate-field registry.

Seeding only *inserts* missing rows for settings, scopes and thresholds, so values the user has
edited are never overwritten. The field registry is *synced* from YAML (the YAML is the source of
truth for definitions); every change is evented.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.contracts import (
    AutonomyMode,
    CandidateFieldDefinition,
    Stage,
    ThresholdAction,
    ThresholdOperator,
)
from orchestrator.db.models import (
    AutonomyScope,
    CandidateFieldDefinitionRow,
    ControlState,
    Setting,
    Threshold,
)
from orchestrator.events import record_event

SYSTEM_ACTOR = "system:init-db"

#: PROJECT_PLAN §11. (mode, is_fixed, description)
DEFAULT_AUTONOMY: dict[Stage, tuple[AutonomyMode, bool, str]] = {
    Stage.INGESTION: (AutonomyMode.CONFIRM, False, "User approves the extracted tree"),
    Stage.OBJECTIVE_LLM_VALUES: (
        AutonomyMode.CONFIRM,
        True,
        "LLM values at objective level must always be confirmed manually (fixed)",
    ),
    Stage.CANDIDATE_PROMOTION: (AutonomyMode.AUTO, False, "Deterministic SQL"),
    Stage.BRANCHING: (AutonomyMode.CONFIRM, False, "Splitting packets into children"),
    Stage.ROUTING: (AutonomyMode.AUTO, False, "Channel selection"),
    Stage.LOCAL_GENERATION: (AutonomyMode.AUTO, False, "Simple local-model generation"),
    Stage.WORKBENCH_PRODUCTION: (AutonomyMode.CONFIRM, False, "Costly/complex work"),
    Stage.REPO_WRITE: (AutonomyMode.CONFIRM, False, "Until trust is established"),
    Stage.QUALITY_SCAN: (AutonomyMode.AUTO, False, "SonarQube scan"),
}

DEFAULT_SETTINGS: dict[str, tuple[Any, str]] = {
    "aggregation.cross_weight": (0.6, "Weight of the cross-check score in model confidence"),
    "aggregation.self_weight": (0.4, "Weight of the self-check score in model confidence"),
    "aggregation.dispute_margin": (
        0.15,
        "Confidence margin below which a disagreement is disputed",
    ),
    "attempts.quickfire_evaluation": (4, "Max attempts per Quickfire Evaluation run (QE)"),
    "attempts.local_generation": (3, "Max attempts per Local Generation run (LG)"),
    "attempts.workbench_production": (2, "Max attempts per Workbench Production run (WP)"),
}

KEY_FIELDS = "@key_fields"  # threshold pseudo-field: "any candidate field with is_key_field"

#: PROJECT_PLAN §8. (field, operator, value, action, precedence, description)
DEFAULT_THRESHOLDS: dict[str, tuple[str, ThresholdOperator, str, ThresholdAction, int, str]] = {
    "approval_high_ambiguity": (
        "ambiguity_quotient",
        ThresholdOperator.GTE,
        "8",
        ThresholdAction.REQUIRE_APPROVAL,
        10,
        "Highly ambiguous prompts go to the approval queue before routing",
    ),
    "approval_key_field_disputed": (
        KEY_FIELDS,
        ThresholdOperator.STATUS_IS,
        "disputed",
        ThresholdAction.REQUIRE_APPROVAL,
        10,
        "Any disputed key field goes to the approval queue before routing",
    ),
    "split_high_parsability": (
        "task_parsability",
        ThresholdOperator.GTE,
        "7",
        ThresholdAction.SPLIT,
        20,
        "Split into independent subtasks (child packets via subtask_list)",
    ),
    "workbench_coding_intensity": (
        "coding_intensity",
        ThresholdOperator.GTE,
        "7",
        ThresholdAction.ROUTE_WORKBENCH_PRODUCTION,
        30,
        "Code-heavy work goes to the workbench",
    ),
    "workbench_reasoning_intensity": (
        "reasoning_intensity",
        ThresholdOperator.GTE,
        "7",
        ThresholdAction.ROUTE_WORKBENCH_PRODUCTION,
        30,
        "Complex reasoning goes to the workbench",
    ),
    "workbench_context_volume": (
        "context_volume",
        ThresholdOperator.GTE,
        "7",
        ThresholdAction.ROUTE_WORKBENCH_PRODUCTION,
        30,
        "Large-context work goes to the workbench",
    ),
    "workbench_output_wordcount": (
        "output_wordcount",
        ThresholdOperator.GTE,
        "8",
        ThresholdAction.ROUTE_WORKBENCH_PRODUCTION,
        30,
        "Lengthy outputs go to the workbench",
    ),
}


def _seed_event(session: Session, entity_type: str, inserted: list[str]) -> None:
    if inserted:
        record_event(
            session,
            actor=SYSTEM_ACTOR,
            action="seed.defaults",
            entity_type=entity_type,
            after={"inserted": inserted},
            reason="init-db default seeding",
        )


def seed_autonomy_scopes(session: Session) -> list[str]:
    existing = set(session.scalars(select(AutonomyScope.stage)))
    inserted = []
    for stage, (mode, fixed, description) in DEFAULT_AUTONOMY.items():
        if stage not in existing:
            session.add(
                AutonomyScope(stage=stage, mode=mode, is_fixed=fixed, description=description)
            )
            inserted.append(stage.value)
    _seed_event(session, "autonomy_scope", inserted)
    return inserted


def seed_settings(session: Session) -> list[str]:
    existing = set(session.scalars(select(Setting.key)))
    inserted = []
    for key, (value, description) in DEFAULT_SETTINGS.items():
        if key not in existing:
            session.add(Setting(key=key, value=value, description=description))
            inserted.append(key)
    _seed_event(session, "setting", inserted)
    return inserted


def seed_thresholds(session: Session) -> list[str]:
    existing = set(session.scalars(select(Threshold.name)))
    inserted = []
    for name, (field, op, value, action, precedence, description) in DEFAULT_THRESHOLDS.items():
        if name not in existing:
            session.add(
                Threshold(
                    name=name,
                    field_name=field,
                    operator=op,
                    value=value,
                    action=action,
                    precedence=precedence,
                    description=description,
                )
            )
            inserted.append(name)
    _seed_event(session, "threshold", inserted)
    return inserted


def seed_control_state(session: Session) -> bool:
    if session.get(ControlState, 1) is None:
        session.add(ControlState(id=1, paused=False))
        _seed_event(session, "control_state", ["1"])
        return True
    return False


_FIELD_COLUMNS = (
    "label",
    "level",
    "kind",
    "vocabulary",
    "scale",
    "max_chars",
    "max_passes",
    "prompt_hint",
    "is_key_field",
)


def _row_snapshot(row: CandidateFieldDefinitionRow) -> dict[str, Any]:
    snap = {c: getattr(row, c) for c in _FIELD_COLUMNS}
    snap["enabled"] = row.enabled
    return {k: (v.value if hasattr(v, "value") else v) for k, v in snap.items()}


def sync_candidate_fields(
    session: Session, definitions: list[CandidateFieldDefinition], actor: str = SYSTEM_ACTOR
) -> dict[str, list[str]]:
    """Upsert the registry into the DB; disable rows no longer in the YAML. Evented."""
    rows = {r.name: r for r in session.scalars(select(CandidateFieldDefinitionRow))}
    result: dict[str, list[str]] = {"inserted": [], "updated": [], "disabled": []}
    changes: dict[str, dict[str, Any]] = {}

    for definition in definitions:
        values = definition.model_dump(include=set(_FIELD_COLUMNS), mode="json")
        row = rows.get(definition.name)
        if row is None:
            session.add(
                CandidateFieldDefinitionRow(
                    name=definition.name,
                    enabled=True,
                    version=1,
                    contract=CandidateFieldDefinition.contract_id(),
                    **values,
                )
            )
            result["inserted"].append(definition.name)
            continue
        before = _row_snapshot(row)
        wanted = {**values, "enabled": True}
        if before != wanted:
            for column, value in wanted.items():
                setattr(row, column, value)
            row.version += 1
            row.contract = CandidateFieldDefinition.contract_id()
            result["updated"].append(definition.name)
            changes[definition.name] = {"before": before, "after": wanted}

    wanted_names = {d.name for d in definitions}
    for name, row in rows.items():
        if name not in wanted_names and row.enabled:
            row.enabled = False
            row.version += 1
            result["disabled"].append(name)

    if any(result.values()):
        record_event(
            session,
            actor=actor,
            action="fields.synced",
            entity_type="candidate_field_definition",
            before={"changes": {n: c["before"] for n, c in changes.items()}} if changes else None,
            after={**result, "changes": {n: c["after"] for n, c in changes.items()}},
            reason="candidate-field registry synced from YAML",
        )
    return result
