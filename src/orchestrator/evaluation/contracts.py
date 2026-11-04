"""Pydantic contracts for evaPacket passes (M2 §3, B8, B10 ruling 4, D5).

* ``CandidateValue`` — the extraction contract, typed by the field definition: a score is an
  integer 0–10, a bool a bool, text at most ``max_chars``, a tag set a list of vocabulary tags
  (no duplicates; an empty list means "no tag" and is stored as NULL). ``null`` = insufficient
  information.
* ``PassEvaluation`` — combined evaluation: ``accuracy_score`` 0–10, ``evidence`` quotes tagged
  SUPPORT/CONTRADICTION, ``reason`` ≤ 280 characters.
* ``AccuracyOnly`` / ``EvidenceOnly`` — the two halves of a split-plan evaluation (B12).
* Tag sets evaluate a map ``{"items": {tag: <evaluation> | null}}`` keyed by the union of both
  candidates' tags; entries for tags the evaluated value does not contain must be ``null``
  (skipped), every other entry must be present (D5 point 2).

Every contract forbids unknown keys and does not coerce types (A7). ``json_schema()`` produces a
conservative JSON-Schema subset (no ``$ref``) for structured decoding (D5 point 4); anything the
subset cannot express stays enforced by Pydantic.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator

from orchestrator.contracts import FieldKind

REASON_MAX = 280
QUOTE_MAX = 500
EVIDENCE_MAX = 8
CONTRACT_VERSION = "1.0"

Score = Annotated[int, Field(ge=0, le=10)]


class EvidenceTag(StrEnum):
    SUPPORT = "SUPPORT"
    CONTRADICTION = "CONTRADICTION"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(_Strict):
    quote: Annotated[str, Field(min_length=1, max_length=QUOTE_MAX)]
    tag: Literal["SUPPORT", "CONTRADICTION"]


class PassEvaluation(_Strict):
    accuracy_score: Score
    evidence: Annotated[list[Evidence], Field(max_length=EVIDENCE_MAX)]
    reason: Annotated[str, Field(max_length=REASON_MAX)]


class AccuracyOnly(_Strict):
    accuracy_score: Score
    reason: Annotated[str, Field(max_length=REASON_MAX)]


class EvidenceOnly(_Strict):
    evidence: Annotated[list[Evidence], Field(max_length=EVIDENCE_MAX)]


EVALUATION_CONTRACTS: dict[str, type[_Strict]] = {
    "evaluate": PassEvaluation,
    "accuracy": AccuracyOnly,
    "evidence": EvidenceOnly,
}


def candidate_model(
    kind: FieldKind, vocabulary: list[str] | None = None, max_chars: int | None = None
) -> type[BaseModel]:
    """The ``CandidateValue`` contract for one field definition."""
    value_type: Any
    if kind is FieldKind.SCORE:
        value_type = Score | None
    elif kind is FieldKind.BOOL:
        value_type = bool | None
    elif kind is FieldKind.TEXT:
        value_type = Annotated[str, Field(min_length=1, max_length=max_chars or 2000)] | None
    else:
        tag = Literal[tuple(vocabulary or ())]  # type: ignore[misc]
        value_type = Annotated[list[tag], Field(max_length=len(vocabulary or ()))] | None  # type: ignore[valid-type]

    model = create_model(
        "CandidateValue",
        __base__=_Strict,
        value=(value_type, ...),
    )
    if kind is FieldKind.TAGSET:

        def _unique(self: Any) -> Any:
            if self.value is not None and len(set(self.value)) != len(self.value):
                raise ValueError("tags must not repeat")
            return self

        model = create_model(
            "CandidateValue",
            __base__=model,
            __validators__={"unique": model_validator(mode="after")(_unique)},
        )
    return model


def tag_map_model(task: str, keys: list[str], applicable: set[str]) -> type[BaseModel]:
    """``{"items": {tag: evaluation | null}}`` with exactly ``keys`` and nulls only where the
    evaluated value lacks the tag (ruling 4, D5)."""
    inner = EVALUATION_CONTRACTS[task]

    def _keys(self: Any) -> Any:
        got = set(self.items)
        missing, extra = set(keys) - got, got - set(keys)
        if missing or extra:
            raise ValueError(f"items must have exactly the keys {sorted(keys)}")
        for key, entry in self.items.items():
            if key in applicable and entry is None:
                raise ValueError(f"items.{key} must be evaluated (it is in the candidate)")
            if key not in applicable and entry is not None:
                raise ValueError(f"items.{key} must be null (not in the candidate)")
        return self

    return create_model(
        "TagEvaluations",
        __base__=_Strict,
        items=(dict[str, inner | None], ...),  # type: ignore[valid-type]
        __validators__={"keys": model_validator(mode="after")(_keys)},
    )


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A conservative, ``$ref``-free JSON schema for structured decoding (D5 point 4)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].split("/")[-1]])
            return {k: inline(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(schema)


def concise_error(exc: ValidationError | ValueError) -> str:
    """One short line a small model can act on (B7) — never a raw traceback."""
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors()[:3]:
            location = ".".join(str(p) for p in error["loc"]) or "output"
            parts.append(f"{location}: {error['msg']}")
        message = "; ".join(parts)
    else:
        message = str(exc)
    return message.replace("\n", " ")[:240]
