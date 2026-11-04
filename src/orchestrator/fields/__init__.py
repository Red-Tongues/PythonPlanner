"""Candidate-field registry: YAML seed → validated definitions → database table."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml
from pydantic import ValidationError

from orchestrator.contracts import CandidateFieldDefinition, FieldLevel

SEED_FILE = "candidate_fields.yaml"


class RegistryError(ValueError):
    """The registry YAML is malformed or contains an invalid definition."""


def load_candidate_fields(path: str | Path | None = None) -> list[CandidateFieldDefinition]:
    """Load and validate the registry. Defaults to the packaged seed file."""
    if path is None:
        text = resources.files(__package__).joinpath(SEED_FILE).read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")

    data = yaml.safe_load(text) or {}
    raw = data.get("fields")
    if not isinstance(raw, list) or not raw:
        raise RegistryError("registry YAML must contain a non-empty 'fields' list")

    definitions: list[CandidateFieldDefinition] = []
    for index, entry in enumerate(raw):
        try:
            definitions.append(CandidateFieldDefinition.model_validate(entry))
        except ValidationError as exc:
            raise RegistryError(f"invalid field definition #{index}: {exc}") from exc

    names = [d.name for d in definitions]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise RegistryError(f"duplicate field names in registry: {duplicates}")
    return definitions


def fields_for_level(
    definitions: list[CandidateFieldDefinition], level: FieldLevel
) -> list[CandidateFieldDefinition]:
    return [d for d in definitions if d.level is level]


__all__ = ["RegistryError", "fields_for_level", "load_candidate_fields"]
