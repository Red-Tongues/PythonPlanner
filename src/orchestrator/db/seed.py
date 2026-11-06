"""Default seeds, candidate-field registry sync and registry revisions.

Default settings, scopes, thresholds, control state and evaPacket presets are seeded by the
inserted by ``ensure_defaults`` on every ``init-db`` (GE6); the schema migration holds no seed rows.
The field registry is synced from YAML by ``init-db`` (the YAML is the source
of truth for definitions, per the M1 spec): changed fields are updated with a version bump,
removed ones disabled, and every change is evented. Each distinct registry content is also
snapshotted as an append-only ``field_registry_revisions`` row (R1 fix 12), which M2 evaluation
cycles reference.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import CandidateFieldDefinition
from orchestrator.db.models import (
    AutonomyScope,
    CandidateFieldDefinitionRow,
    ControlState,
    EvaPreset,
    FieldRegistryRevision,
    Setting,
    Threshold,
    new_id,
)
from orchestrator.events import record_event

SYSTEM_ACTOR = Actor.system("init-db")
KEY_FIELDS = "@key_fields"  # threshold pseudo-field: "any candidate field with is_key_field"

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
    session: Session, definitions: list[CandidateFieldDefinition], actor: Actor = SYSTEM_ACTOR
) -> dict[str, list[str]]:
    """Upsert the registry into the DB; disable rows no longer in the YAML. Evented."""
    rows = {r.name: r for r in session.scalars(select(CandidateFieldDefinitionRow))}
    result: dict[str, list[str]] = {"inserted": [], "updated": [], "disabled": []}
    changes: dict[str, dict[str, Any]] = {}
    contract = CandidateFieldDefinition.contract_id()

    for definition in definitions:
        values = definition.model_dump(include=set(_FIELD_COLUMNS), mode="json")
        row = rows.get(definition.name)
        if row is None:
            session.add(
                CandidateFieldDefinitionRow(
                    name=definition.name, enabled=True, version=1, contract=contract, **values
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
            row.contract = contract
            result["updated"].append(definition.name)
            changes[definition.name] = {"before": before, "after": wanted}

    wanted_names = {d.name for d in definitions}
    for name, row in rows.items():
        if name not in wanted_names and row.enabled:
            row.enabled = False
            row.version += 1
            result["disabled"].append(name)

    if any(result.values()):
        record_event(  # a batch event covering every row changed above (R1 fix 7)
            session,
            actor=actor,
            action="fields.synced",
            entity_type="candidate_field_definition",
            before={"changes": {n: c["before"] for n, c in changes.items()}} if changes else None,
            after={**result, "changes": {n: c["after"] for n, c in changes.items()}},
            reason="candidate-field registry synced from YAML",
        )
    session.flush()
    return result


def registry_fingerprint(definitions: list[CandidateFieldDefinition]) -> tuple[str, list[dict]]:
    """SHA-256 of the canonical JSON of the definitions (order-independent)."""
    snapshot = sorted((d.model_dump(mode="json") for d in definitions), key=lambda d: d["name"])
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest(), snapshot


def current_registry_revision(session: Session) -> FieldRegistryRevision | None:
    stmt = select(FieldRegistryRevision).order_by(FieldRegistryRevision.revision.desc()).limit(1)
    return session.scalar(stmt)


def record_registry_revision(
    session: Session, definitions: list[CandidateFieldDefinition], actor: Actor = SYSTEM_ACTOR
) -> FieldRegistryRevision:
    """Snapshot the registry if its content differs from the latest revision. Evented."""
    sha256, snapshot = registry_fingerprint(definitions)
    latest = current_registry_revision(session)
    if latest is not None and latest.sha256 == sha256:
        return latest
    row = FieldRegistryRevision(
        id=new_id(),
        revision=(latest.revision + 1) if latest else 1,
        sha256=sha256,
        field_count=len(snapshot),
        definitions=snapshot,
    )
    session.add(row)
    record_event(
        session,
        actor=actor,
        action="fields.registry_revision",
        entity_type="field_registry_revision",
        entity_id=row.id,
        entity_version=row.revision,
        before={"revision": latest.revision, "sha256": latest.sha256} if latest else None,
        after={"revision": row.revision, "sha256": sha256, "field_count": row.field_count},
        reason="candidate-field registry content changed",
    )
    session.flush()
    return row


# --------------------------------------------------------------------------- default seeds (GE6)
# The one copy of every default. ``ensure_defaults`` runs on every ``init-db`` and inserts any
# that are missing, so a deleted default comes back. It never overwrites a row that exists (user
# edits survive).

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
SETTINGS_M1 = (
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

SETTINGS_M2 = (
    ("consensus.agreement_min", 0.6, "Agreement is accepted at or above this confidence (B2)"),
    ("consensus.winner_min", 0.7, "A disagreement winner needs at least this confidence (B2)"),
    ("consensus.one_sided_min", 0.7, "A one-sided (other NULL) value needs this confidence (B2)"),
    ("consensus.min_supports", 1, "Verified SUPPORT quotes needed for acceptance (B2)"),
    ("evaluation.repair_attempts", 1, "Targeted repairs per call before escalation (B12)"),
    ("evaluation.max_escalations", 1, "Split-plan re-runs per field before needs_review (B12)"),
    ("evaluation.num_ctx", 4096, "Context window sent explicitly to the evaluator (B11)"),
    ("evaluation.max_tokens", 512, "Max completion tokens per evaluation call"),
    ("evaluation.max_exemplars", 3, "Exemplars per field included in a prompt"),
    ("queue.lease_minutes", 30, "Claim lease length"),
    ("branching.max_depth", 2, "Maximum branch depth (B5)"),
    ("branching.max_descendants", 12, "Maximum descendants per trunk (B5)"),
    ("stats.score_threshold", 7, "Scores at or above this count as 'above threshold'"),
)

#: M3a (R6): every constant of the genPacket loop is a versioned setting (OQ7)
SETTINGS_M3A = (
    ("qctx.w_direct_e4", 750, "OQ4: field weight when a direct context link is questioned"),
    ("qctx.w_periph_e4", 250, "OQ4: field weight when only peripheral links are questioned"),
    ("qctx.cap_e4", 2250, "OQ4: cap of a packet's question_score_e4"),
    ("gen_queue.w_grade_e4", 6000, "OQ7: weight of (10000 - parent grade), x 10^4"),
    ("gen_queue.w_centrality_e4", 7000, "OQ7: weight of relational centrality, x 10^4"),
    ("gen_queue.root_parent_grade_e4", 5000, "OQ6: effective parent grade of a root packet"),
    ("gen_queue.ageing_per_minute_e4", 10, "OQ6: ageing per full minute in ready"),
    ("gen_queue.ageing_cap_e4", 1500, "OQ6: ageing cap"),
    ("admission.floor_e4", 3500, "OQ7: relational admission floor (ready -> queued)"),
    ("admission.low_floor_e4", 2500, "OQ7: floor while queued depth is below admission.low_depth"),
    ("admission.low_depth", 3, "OQ7: queued depth under which the low floor applies"),
    ("admission.bypass_wait_sec", 7200, "OQ7: wait in ready after which the floor is bypassed"),
    ("centrality.direct_e4", 3000, "OQ8: per unsealed direct dependent"),
    ("centrality.two_hop_e4", 1500, "OQ8: per unsealed two-hop dependent"),
    ("centrality.critical_e4", 2500, "OQ8: on a critical objective path"),
    ("dep_satisfaction_grade_e4", 7000, "OQ13: a sealed lineage member at this grade satisfies"),
    ("lineage_stop_grade_e4", 8500, "OQ14: a child of a parent at this grade is created held"),
    ("feedback.boost_e4", 1500, "OQ9: BOOST"),
    ("feedback.nudge_e4", 500, "OQ9: NUDGE"),
    ("feedback.demote_e4", -1500, "OQ9: DEMOTE"),
    ("feedback.pin_e4", 3000, "OQ9: PIN"),
    ("feedback.clamp_e4", 3000, "OQ9: packet feedback sum clamped to +/- this"),
    ("crash.limit", 5, "OQ18: crash_count at which the packet is held (crash_limit)"),
    ("crash.cooldown_base_sec", 30, "OQ18: cooldown = min(max, base * 2^(crash_count - 1))"),
    ("crash.cooldown_max_sec", 3600, "OQ18: cooldown cap"),
    ("route.crash_window_sec", 300, "OQ18: window for counting a route's crashes"),
    ("route.crash_threshold", 3, "OQ18: crashes in the window that make a route unhealthy"),
    ("route.unhealthy_sec", 600, "OQ18: how long a route stays unhealthy"),
    ("dispatch.oldest_every", 5, "OQ25: every Nth dispatch takes the oldest queued packet"),
    ("dispatch.incumbency_margin_e4", 250, "OQ25: a challenger must beat the incumbent by this"),
    ("gen_eva.synth_below_e4", 5000, "OQ17: below this grade the child prompt is LLM-rewritten"),
    ("readiness.max_ambiguity_e4", 4000, "OQ11: ambiguity_quotient x 1000 must not exceed this"),
)

SETTINGS = SETTINGS_M1 + SETTINGS_M2 + SETTINGS_M3A

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

# field → (phase, tier, required, hard, gate_operator, gate_value)
PRESETS: dict[str, tuple[str, int, bool, bool, str | None, str | None]] = {
    "safety_clear": ("pre", 1, True, True, "=", "true"),
    "ambiguity_quotient": ("pre", 1, True, False, None, None),
    "task_parsability": ("pre", 1, True, False, None, None),
    "artifact_request_classes": ("pre", 2, True, False, None, None),
    "tools_needed": ("pre", 2, True, False, None, None),
    "coding_intensity": ("pre", 2, True, False, None, None),
    "reasoning_intensity": ("pre", 2, True, False, None, None),
    "context_volume": ("pre", 2, True, False, None, None),
    "output_wordcount": ("pre", 2, True, False, None, None),
    "representation_formats": ("pre", 3, False, False, None, None),
    "repository_roles": ("pre", 3, False, False, None, None),
    "domains": ("pre", 3, False, False, None, None),
    "styles": ("pre", 3, False, False, None, None),
    "context_independence": ("pre", 3, False, False, None, None),
    "web_independence": ("pre", 3, False, False, None, None),
    "requirement_alignment": ("post", 1, True, False, ">=", "6"),
    "correctness": ("post", 1, True, False, ">=", "6"),
    "security_risk": ("post", 1, True, True, "<=", "6"),
    "testability": ("post", 3, False, False, None, None),
    "internal_consistency": ("post", 3, False, False, None, None),
    "functional_completeness": ("post", 3, False, False, None, None),
    "repo_consistency": ("post", 3, False, False, None, None),
    "maintainability": ("post", 3, False, False, None, None),
    "duplication_quotient": ("post", 3, False, False, None, None),
    "technical_debt": ("post", 3, False, False, None, None),
    "acceptance_criteria_coverage": ("post", 3, False, False, None, None),
    "dependency_validity": ("post", 3, False, False, None, None),
    "determinism": ("post", 3, False, False, None, None),
    "observability": ("post", 3, False, False, None, None),
    "reversibility": ("post", 3, False, False, None, None),
    "documentation_adequacy": ("post", 3, False, False, None, None),
    "performance_risk": ("post", 3, False, False, None, None),
    "portability": ("post", 3, False, False, None, None),
    "data_integrity_risk": ("post", 3, False, False, None, None),
    "privacy_risk": ("post", 3, False, False, None, None),
    "licence_and_provenance_risk": ("post", 3, False, False, None, None),
}


#: M3a (R6 OQ10, OQ16): pre-phase presets for the universal and relational mandatory fields (their
#: mandatoriness is relational, computed per packet, so ``required`` stays false) and post-phase
#: presets for the composite gen_eva grade. (field, phase) → (tier, required, hard, op, value)
M3A_PRESETS: dict[tuple[str, str], tuple[int, bool, bool, str | None, str | None]] = {
    **{
        (name, "pre"): (2, False, False, None, None)
        for name in (
            "requirement_alignment",
            "testability",
            "acceptance_criteria_coverage",
            "dependency_validity",
            "interface_stability",
            "downstream_contract_clarity",
            "risk_assessment",
            "sonar_hotspot_alignment",
        )
    },
    ("risk_assessment", "post"): (3, False, False, None, None),
    ("sonar_hotspot_alignment", "post"): (3, False, False, None, None),
}


def all_presets() -> list[tuple[str, str, int, bool, bool, str | None, str | None]]:
    rows = [(f, *spec) for f, spec in PRESETS.items()]
    rows += [(f, ph, *spec) for (f, ph), spec in M3A_PRESETS.items()]
    return rows


def ensure_defaults(session: Session, actor: Actor = SYSTEM_ACTOR) -> dict[str, list[str]]:
    """Insert every missing default row (never overwrite one); one event per table (GE6)."""
    inserted: dict[str, list[str]] = {}
    with session.no_autoflush:  # rows and their events must reach one flush together (fix 7)

        def note(entity: str, key: str, row: object) -> None:
            session.add(row)
            inserted.setdefault(entity, []).append(key)

        present = {
            s.stage.value if hasattr(s.stage, "value") else s.stage
            for s in session.scalars(select(AutonomyScope))
        }
        for stage, mode, fixed, description in AUTONOMY_SCOPES:
            if stage not in present:
                note(
                    "autonomy_scope",
                    stage,
                    AutonomyScope(stage=stage, mode=mode, is_fixed=fixed, description=description),
                )
        present = set(session.scalars(select(Setting.key)))
        for key, value, description in SETTINGS:
            if key not in present:
                note(
                    "setting",
                    key,
                    Setting(key=key, value=value, description=description, version=1),
                )
        present = set(session.scalars(select(Threshold.name)))
        for name, field_name, operator, value, action, precedence, description in THRESHOLDS:
            if name not in present:
                note(
                    "threshold",
                    name,
                    Threshold(
                        id=new_id(),
                        name=name,
                        field_name=field_name,
                        operator=operator,
                        value=value,
                        action=action,
                        precedence=precedence,
                        enabled=True,
                        description=description,
                        version=1,
                    ),
                )
        if session.get(ControlState, 1) is None:
            note("control_state", "1", ControlState(id=1, paused=False))
        present = {
            (p.field_name, p.phase.value if hasattr(p.phase, "value") else p.phase)
            for p in session.scalars(select(EvaPreset))
        }
        for field_name, phase, tier, required, hard, gate_op, gate_value in all_presets():
            if (field_name, phase) not in present:
                note(
                    "eva_preset",
                    field_name,
                    EvaPreset(
                        id=new_id(),
                        field_name=field_name,
                        phase=phase,
                        revision=1,
                        tier=tier,
                        required=required,
                        hard=hard,
                        gate_operator=gate_op,
                        gate_value=gate_value,
                        plan="combined",
                        prompt_template="quickfire.v1",
                        schema_version="1.0",
                        policy_version="1.0",
                        enabled=True,
                    ),
                )
        for entity, keys in inserted.items():
            record_event(
                session,
                actor=actor,
                action="seed.defaults",
                entity_type=entity,
                after={"inserted": keys},
                reason="init-db re-seeded missing defaults",
            )
    session.flush()
    return inserted
