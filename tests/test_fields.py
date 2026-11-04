from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.contracts import FieldKind, FieldLevel
from orchestrator.db.models import CandidateFieldDefinitionRow, Event
from orchestrator.db.seed import sync_candidate_fields
from orchestrator.fields import RegistryError, fields_for_level, load_candidate_fields

# Every candidate field listed in docs/archive/Python_Planner_v0_draft.md.
DRAFT_TASK_TAGSETS = {
    "artifact_request_classes": {
        "source_code",
        "configuration",
        "documentation",
        "test",
        "dataset",
        "schema",
        "prompt",
    },
    "representation_formats": {
        "plain_text",
        "markdown",
        "json",
        "yaml",
        "xml",
        "csv",
        "sql",
        "binary",
    },
    "repository_roles": {
        "executable",
        "declarative",
        "reference",
        "validation",
        "build",
        "deployment",
    },
    "domains": {
        "education",
        "data_analysis",
        "software_development",
        "it_generic",
        "law",
        "start_up",
        "music",
        "care",
    },
    "styles": {
        "creative",
        "brainstorm",
        "technical",
        "draft",
        "prototype",
        "analysis",
        "corrective",
        "plan",
        "production",
    },
    "tools_needed": {"natural_language", "csv_creation", "csv_extraction", "python", "json_parse"},
}
DRAFT_TASK_SCORES = {
    "task_parsability",
    "context_volume",
    "context_independence",
    "web_independence",
    "coding_intensity",
    "reasoning_intensity",
    "output_wordcount",
    "ambiguity_quotient",
}
DRAFT_CONTENT_SCORES = {
    "testability",
    "requirement_alignment",
    "correctness",
    "internal_consistency",
    "functional_completeness",
    "repo_consistency",
    "maintainability",
    "security_risk",
    "duplication_quotient",
    "technical_debt",
    "acceptance_criteria_coverage",
    "dependency_validity",
    "determinism",
    "observability",
    "reversibility",
    "documentation_adequacy",
    "performance_risk",
    "portability",
    "data_integrity_risk",
    "privacy_risk",
    "licence_and_provenance_risk",
}


def test_registry_contains_every_draft_field() -> None:
    defs = {d.name: d for d in load_candidate_fields()}
    expected = set(DRAFT_TASK_TAGSETS) | DRAFT_TASK_SCORES | DRAFT_CONTENT_SCORES
    assert expected <= set(defs)
    # M2 adds one gate field beyond the draft (D1 tier 1 "safety"): safety_clear.
    assert set(defs) - expected == {"safety_clear"}
    assert len(defs) == len(expected) + 1 == 36

    for name, vocabulary in DRAFT_TASK_TAGSETS.items():
        d = defs[name]
        assert (d.level, d.kind) == (FieldLevel.TASK, FieldKind.TAGSET)
        assert vocabulary <= set(d.vocabulary or [])
    for name in DRAFT_TASK_SCORES:
        assert (defs[name].level, defs[name].kind, defs[name].scale) == (
            FieldLevel.TASK,
            FieldKind.SCORE,
            "0-10",
        )
    for name in DRAFT_CONTENT_SCORES:
        assert (defs[name].level, defs[name].kind, defs[name].scale) == (
            FieldLevel.CONTENT,
            FieldKind.SCORE,
            "0-10",
        )
    assert all(d.prompt_hint for d in defs.values())


def test_key_fields_cover_routing_and_branching_rules() -> None:
    keys = {d.name for d in load_candidate_fields() if d.is_key_field}
    assert {
        "task_parsability",
        "coding_intensity",
        "reasoning_intensity",
        "context_volume",
        "output_wordcount",
        "ambiguity_quotient",
    } <= keys


def test_fields_for_level() -> None:
    defs = load_candidate_fields()
    assert len(fields_for_level(defs, FieldLevel.TASK)) == 15
    assert len(fields_for_level(defs, FieldLevel.CONTENT)) == 21


def test_invalid_registry_yaml(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.yaml"
    bad.write_text("fields:\n  - name: x\n    label: X\n    level: task\n    kind: score\n")
    with pytest.raises(RegistryError, match="invalid field definition"):
        load_candidate_fields(bad)
    dup = tmp_path / "dup.yaml"
    entry = "  - {name: x, label: X, level: task, kind: score, scale: '0-10', prompt_hint: h}\n"
    dup.write_text("fields:\n" + entry + entry)
    with pytest.raises(RegistryError, match="duplicate"):
        load_candidate_fields(dup)
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}")
    with pytest.raises(RegistryError):
        load_candidate_fields(empty)


def test_init_db_synced_registry_into_db(session: Session) -> None:
    rows = list(session.scalars(select(CandidateFieldDefinitionRow)))
    assert len(rows) == 36
    assert all(r.version == 1 and r.contract.startswith("CandidateFieldDefinition@") for r in rows)


def test_sync_is_idempotent_and_versions_changes(session: Session) -> None:
    defs = load_candidate_fields()
    assert sync_candidate_fields(session, defs) == {"inserted": [], "updated": [], "disabled": []}

    changed = [
        d.model_copy(update={"label": "Renamed"}) if d.name == "styles" else d
        for d in defs
        if d.name != "music"
    ]
    changed = [d for d in changed if d.name != "privacy_risk"]
    result = sync_candidate_fields(session, changed)
    assert result == {"inserted": [], "updated": ["styles"], "disabled": ["privacy_risk"]}
    styles = session.scalar(
        select(CandidateFieldDefinitionRow).where(CandidateFieldDefinitionRow.name == "styles")
    )
    assert (styles.label, styles.version) == ("Renamed", 2)
    assert session.scalar(
        select(Event).where(Event.action == "fields.synced").order_by(Event.created_at.desc())
    ).after["updated"] == ["styles"]
