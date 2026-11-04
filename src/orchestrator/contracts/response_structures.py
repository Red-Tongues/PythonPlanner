"""Response-structure registry: every name maps to a Pydantic contract.

"All possible response contracts need to be recognised" — an unknown name is rejected with
``UnknownResponseStructureError`` wherever it appears (ingestion, supersede, channel runs).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from orchestrator.contracts.models import Contract, ProjectTree, TaskCore
from orchestrator.errors import UnknownResponseStructureError


class MarkdownDocument(Contract):
    title: str | None = None
    content: str = Field(min_length=1)


class JsonObject(Contract):
    data: dict[str, Any]


class SubtaskList(Contract):
    """Output of a split: independent subtasks that become child packets."""

    subtasks: list[TaskCore] = Field(min_length=1)


_REGISTRY: dict[str, type[BaseModel]] = {
    "markdown_document": MarkdownDocument,
    "json_object": JsonObject,
    "subtask_list": SubtaskList,
    "project_tree": ProjectTree,
}


def response_structure_id(name: str) -> str:
    """``"name@version"`` of a registered structure — stored alongside every use of it."""
    model = get_response_structure(name)
    return f"{name}@{getattr(model, 'CONTRACT_VERSION', '1.0')}"


def known_response_structures() -> list[str]:
    return sorted(_REGISTRY)


def get_response_structure(name: str) -> type[BaseModel]:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(known_response_structures())
        raise UnknownResponseStructureError(
            f"unknown response structure {name!r}; registered structures: {known}"
        ) from None


def register_response_structure(name: str, model: type[BaseModel]) -> None:
    """Register an additional contract. Re-registering an existing name is an error."""
    if name in _REGISTRY and _REGISTRY[name] is not model:
        raise ValueError(f"response structure {name!r} is already registered")
    _REGISTRY[name] = model


def validate_response(name: str, data: Any) -> BaseModel:
    """Validate ``data`` (dict or JSON string) against the named contract."""
    model = get_response_structure(name)
    if isinstance(data, str | bytes):
        return model.model_validate_json(data)
    return model.model_validate(data)
