"""Core Pydantic contracts: the project tree and candidate-field definitions.

``ProjectTree`` is the single contract every ingestion path (deterministic parser now, LLM or
Juggler extraction later) must produce before anything reaches the database.
"""

from __future__ import annotations

import re
from typing import Annotated, ClassVar, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    field_validator,
    model_validator,
)

from orchestrator.contracts.enums import FieldKind, FieldLevel

DEFAULT_RESPONSE_STRUCTURE = "markdown_document"
SCORE_SCALE = "0-10"
TEXT_MAX_PASSES = 3
DEFAULT_MAX_PASSES = 6

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ShortName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


class Contract(BaseModel):
    """Base for every contract: unknown keys are rejected and types are not coerced (A7).

    Each contract carries a ``CONTRACT_VERSION``; ``contract_id()`` (``"Name@version"``) is
    recorded wherever a contract is used (packets, projects, confirmations, channel runs).
    Enum-typed fields accept their string values (see ``LaxEnum``) because the vocabulary is
    closed — everything else is strict (no ``"7"`` → ``7``, no ``1`` → ``True``).
    """

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    CONTRACT_VERSION: ClassVar[str] = "1.0"

    @classmethod
    def contract_id(cls) -> str:
        return f"{cls.__name__}@{cls.CONTRACT_VERSION}"


class ProjectCore(Contract):
    """Project-level core fields (used as context in every evaluation prompt)."""

    name: ShortName
    brief: str = ""
    industry: str | None = None
    sectors: list[NonEmptyStr] = Field(default_factory=list)
    values: list[NonEmptyStr] = Field(default_factory=list)
    goals: list[NonEmptyStr] = Field(default_factory=list)
    scope: str | None = None
    references: list[NonEmptyStr] = Field(default_factory=list)


class ObjectiveCore(Contract):
    """Objective-level core fields."""

    name: ShortName
    brief: str = ""


class TaskCore(Contract):
    """Task (Prompt Packet) core fields."""

    title: ShortName
    prompt_description: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    response_structure: str = DEFAULT_RESPONSE_STRUCTURE
    repo_target_path: str | None = None

    @field_validator("response_structure")
    @classmethod
    def _known_structure(cls, value: str) -> str:
        # Imported lazily: the registry itself references TaskCore / ProjectTree.
        from orchestrator.contracts.response_structures import get_response_structure

        get_response_structure(value)  # raises UnknownResponseStructureError
        return value


class ObjectiveNode(ObjectiveCore):
    """An objective together with its tasks, as it appears inside a ``ProjectTree``."""

    tasks: list[TaskCore] = Field(default_factory=list)


class ProjectTree(Contract):
    """Project > Objective > Task — the contract for every ingestion path."""

    project: ProjectCore
    objectives: list[ObjectiveNode] = Field(min_length=1)

    @property
    def task_count(self) -> int:
        return sum(len(o.tasks) for o in self.objectives)


class CandidateFieldDefinition(Contract):
    """One evaluative field in the candidate-field registry."""

    name: str
    label: NonEmptyStr
    level: Annotated[FieldLevel, Strict(False)]
    kind: Annotated[FieldKind, Strict(False)]
    vocabulary: list[NonEmptyStr] | None = None
    scale: str | None = None
    max_chars: int | None = Field(default=None, gt=0)
    max_passes: int | None = Field(default=None, ge=1, le=DEFAULT_MAX_PASSES)
    prompt_hint: NonEmptyStr
    is_key_field: bool = False

    @field_validator("name")
    @classmethod
    def _snake_case(cls, value: str) -> str:
        if not SNAKE_CASE.match(value):
            raise ValueError(f"field name {value!r} must be snake_case")
        return value

    @model_validator(mode="after")
    def _kind_rules(self) -> Self:
        if self.kind is FieldKind.TAGSET:
            if not self.vocabulary:
                raise ValueError(f"tagset field {self.name!r} needs a non-empty vocabulary")
            bad = [t for t in self.vocabulary if not SNAKE_CASE.match(t)]
            if bad:
                raise ValueError(f"vocabulary of {self.name!r} must be snake_case: {bad}")
            if len(set(self.vocabulary)) != len(self.vocabulary):
                raise ValueError(f"vocabulary of {self.name!r} has duplicates")
        elif self.vocabulary is not None:
            raise ValueError(f"only tagset fields take a vocabulary ({self.name!r})")

        if self.kind is FieldKind.SCORE:
            if self.scale != SCORE_SCALE:
                raise ValueError(f"score field {self.name!r} must use scale {SCORE_SCALE!r}")
        elif self.scale is not None:
            raise ValueError(f"only score fields take a scale ({self.name!r})")

        if self.kind is FieldKind.TEXT:
            if self.max_chars is None:
                raise ValueError(f"text field {self.name!r} needs max_chars")
            if self.max_passes is None:
                self.max_passes = TEXT_MAX_PASSES
            if self.max_passes > TEXT_MAX_PASSES:
                raise ValueError(f"text field {self.name!r} is capped at {TEXT_MAX_PASSES} passes")
        elif self.max_passes is None:
            self.max_passes = DEFAULT_MAX_PASSES
        return self
