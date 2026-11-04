from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.contracts import (
    CandidateFieldDefinition,
    FieldKind,
    MarkdownDocument,
    ProjectTree,
    SubtaskList,
    TaskCore,
    get_response_structure,
    known_response_structures,
    response_structure_id,
    validate_response,
)
from orchestrator.errors import UnknownResponseStructureError
from tests.conftest import SMALL_TREE


def test_project_tree_validates_and_counts_tasks() -> None:
    tree = ProjectTree.model_validate(SMALL_TREE)
    assert tree.project.name == "Demo project"
    assert tree.task_count == 3


def test_project_tree_needs_an_objective() -> None:
    with pytest.raises(ValidationError):
        ProjectTree.model_validate({"project": {"name": "x"}, "objectives": []})


def test_contracts_forbid_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        TaskCore.model_validate({"title": "t", "prompt_description": "d", "bogus": 1})


def test_contracts_are_strict() -> None:
    # A7: no silent coercion of types
    with pytest.raises(ValidationError):
        TaskCore.model_validate({"title": 123, "prompt_description": "d"})
    with pytest.raises(ValidationError):
        CandidateFieldDefinition.model_validate(
            {
                "name": "x",
                "label": "X",
                "level": "task",
                "kind": "score",
                "scale": "0-10",
                "prompt_hint": "h",
                "is_key_field": "yes",
            }
        )


def test_blank_strings_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCore(title="   ", prompt_description="d")


def test_contract_versions_are_exposed() -> None:
    assert ProjectTree.contract_id() == f"ProjectTree@{ProjectTree.CONTRACT_VERSION}"
    assert response_structure_id("markdown_document").startswith("markdown_document@")


def test_response_structure_registry() -> None:
    assert {"markdown_document", "json_object", "subtask_list", "project_tree"} <= set(
        known_response_structures()
    )
    assert get_response_structure("subtask_list") is SubtaskList
    assert get_response_structure("project_tree") is ProjectTree
    doc = validate_response("markdown_document", '{"content": "# hi"}')
    assert isinstance(doc, MarkdownDocument)
    with pytest.raises(ValidationError):
        validate_response("subtask_list", {"subtasks": []})


def test_unknown_response_structure_is_a_clear_error() -> None:
    with pytest.raises(UnknownResponseStructureError, match="registered structures"):
        get_response_structure("haiku")
    with pytest.raises(UnknownResponseStructureError):
        TaskCore(title="t", prompt_description="d", response_structure="haiku")


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"kind": "tagset"}, "vocabulary"),
        ({"kind": "tagset", "vocabulary": ["Bad Tag"]}, "snake_case"),
        ({"kind": "tagset", "vocabulary": ["a", "a"]}, "duplicates"),
        ({"kind": "score"}, "scale"),
        ({"kind": "score", "scale": "1-5"}, "scale"),
        ({"kind": "text"}, "max_chars"),
        ({"kind": "text", "max_chars": 200, "max_passes": 6}, "capped"),
        ({"kind": "bool", "vocabulary": ["a"]}, "only tagset"),
        ({"kind": "bool", "name": "BadName"}, "snake_case"),
    ],
)
def test_candidate_field_definition_rules(entry: dict, message: str) -> None:
    base = {"name": "some_field", "label": "Some field", "level": "task", "prompt_hint": "h"}
    with pytest.raises(ValidationError, match=message):
        CandidateFieldDefinition.model_validate({**base, **entry})


def test_text_field_defaults_to_three_passes() -> None:
    d = CandidateFieldDefinition.model_validate(
        {
            "name": "summary",
            "label": "Summary",
            "level": "task",
            "kind": "text",
            "max_chars": 280,
            "prompt_hint": "h",
        }
    )
    assert d.kind is FieldKind.TEXT
    assert d.max_passes == 3
