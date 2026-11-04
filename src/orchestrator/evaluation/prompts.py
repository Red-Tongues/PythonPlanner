"""Prompt templates for evaPackets (M2 §2; wording from PROJECT_PLAN §7; B8).

Every prompt is self-contained (golden rule 3): the field definition, the packet's core fields,
the project and objective context, up to N exemplars and a compact JSON schema of the expected
output. A short machine-readable header (``### TASK: ...``) names the task and field; it costs a
few tokens, helps small models and lets ``FakeProvider`` answer deterministically.

The source document is delimited so evidence quotes can be verified against exactly the text the
model saw (``source_document``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from orchestrator.contracts import FieldKind

TEMPLATE_VERSION = "quickfire.v1"


@dataclass(frozen=True)
class FieldInfo:
    name: str
    label: str
    kind: FieldKind
    prompt_hint: str
    vocabulary: tuple[str, ...] = ()
    max_chars: int | None = None


@dataclass(frozen=True)
class Exemplar:
    input_text: str
    expected_value: Any


def source_document(
    *,
    project_name: str,
    project_brief: str,
    objective_name: str | None,
    objective_brief: str | None,
    title: str,
    description: str,
) -> str:
    """The text every evaPacket of a packet version reads; its hash is the target hash."""
    lines = [f"PROJECT: {project_name}"]
    if project_brief:
        lines.append(f"PROJECT BRIEF: {project_brief}")
    if objective_name:
        lines.append(f"OBJECTIVE: {objective_name}")
    if objective_brief:
        lines.append(f"OBJECTIVE BRIEF: {objective_brief}")
    lines += [f"TASK: {title}", "TASK DESCRIPTION:", description.strip()]
    return "\n".join(lines)


def _value_spec(field: FieldInfo) -> str:
    if field.kind is FieldKind.SCORE:
        return "an integer from 0 (not at all) to 10 (completely)"
    if field.kind is FieldKind.BOOL:
        return "true or false"
    if field.kind is FieldKind.TEXT:
        return f"a short text of at most {field.max_chars} characters"
    return "a list of zero or more of these tags: " + ", ".join(field.vocabulary)


def _header(task: str, field: FieldInfo, **extra: str) -> str:
    lines = [f"### TASK: {task}", f"### FIELD: {field.name}", f"### KIND: {field.kind.value}"]
    if field.vocabulary:
        lines.append(f"### VOCABULARY: {', '.join(field.vocabulary)}")
    if field.max_chars:
        lines.append(f"### MAX_CHARS: {field.max_chars}")
    lines += [f"### {k.upper()}: {v}" for k, v in extra.items()]
    return "\n".join(lines)


def _definition(field: FieldInfo) -> str:
    return (
        f"Candidate field '{field.label}' ({field.name}): {field.prompt_hint}\n"
        f"A value is {_value_spec(field)}."
    )


def _document_block(document: str) -> str:
    return f"<<<DOCUMENT\n{document}\nDOCUMENT>>>"


def _schema_block(schema: dict[str, Any]) -> str:
    return "Respond with ONE JSON object matching this schema and nothing else:\n" + json.dumps(
        schema, separators=(",", ":")
    )


def extraction_prompt(
    field: FieldInfo,
    document: str,
    schema: dict[str, Any],
    exemplars: list[Exemplar] = (),  # type: ignore[assignment]
) -> str:
    parts = [
        _header("extract", field),
        _definition(field),
        "Read the project document and respond only with the most appropriate value for "
        f"'{field.label}'. If insufficient information exists, return null as the value.",
    ]
    if exemplars:
        parts.append(
            "Examples:\n"
            + "\n".join(
                f"- {e.input_text[:300]!r} -> {json.dumps({'value': e.expected_value})}"
                for e in exemplars
            )
        )
    parts += [_document_block(document), _schema_block(schema)]
    return "\n\n".join(parts)


_TASK_TEXT = {
    "evaluate": (
        "Determine the accuracy of the candidate value as a '{label}'. Consider applicability, "
        "relevance, prevalence of evidence, contradictions and ambiguity: has the candidate value "
        "been accurately generated? Give an accuracy_score from 0 to 10, the strongest evidence "
        "supporting or contradicting it as EXACT quotations from the document (tag each SUPPORT "
        "or CONTRADICTION; an empty list if there is none) and a reason of at most 280 "
        "characters."
    ),
    "accuracy": (
        "Determine the accuracy of the candidate value as a '{label}'. Consider applicability, "
        "relevance, prevalence of evidence, contradictions and ambiguity: has the candidate value "
        "been accurately generated? Respond only with an accuracy_score from 0 to 10 and a "
        "reason of at most 280 characters."
    ),
    "evidence": (
        "Identify the strongest evidence supporting or contradicting the candidate value as a "
        "value for '{label}'. Return only EXACT quotations from the document, each tagged "
        "SUPPORT or CONTRADICTION. If no relevant evidence exists, return an empty list."
    ),
}


def evaluation_prompt(
    task: str,
    field: FieldInfo,
    document: str,
    candidate: Any,
    schema: dict[str, Any],
    *,
    keys: list[str] | None = None,
    applicable: list[str] | None = None,
) -> str:
    """Passes 3–6 (``task`` = evaluate | accuracy | evidence). Tag sets evaluate per tag."""
    extra: dict[str, str] = {}
    instructions = _TASK_TEXT[task].format(label=field.label)
    if field.kind is FieldKind.TAGSET:
        extra = {"items": ", ".join(keys or []), "applicable": ", ".join(applicable or [])}
        instructions += (
            " Evaluate each tag of the candidate separately (is this tag accurate for the "
            "task?). Answer with an 'items' object keyed by every tag listed below; give null "
            f"for tags not in the candidate. Keys: {', '.join(keys or [])}."
        )
    parts = [
        _header(task, field, **extra),
        _definition(field),
        f"<<<CANDIDATE\n{json.dumps(candidate)}\nCANDIDATE>>>",
        instructions,
        _document_block(document),
        _schema_block(schema),
    ]
    return "\n\n".join(parts)


def correction_suffix(error: str) -> str:
    """The targeted repair appended to a retry (B7: one concise line)."""
    return f"\n\nYour previous answer was invalid: {error}. Reply again with corrected JSON only."


def subtask_prompt(document: str, schema: dict[str, Any]) -> str:
    return "\n\n".join(
        [
            "### TASK: subtasks",
            "Split the task below into 2-4 independent subtasks that together complete it. "
            "Each subtask needs a short title and a self-contained prompt_description.",
            _document_block(document),
            _schema_block(schema),
        ]
    )


def tree_prompt(document: str, schema: dict[str, Any], name: str) -> str:
    return "\n\n".join(
        [
            "### TASK: project_tree",
            f"### NAME: {name}",
            "Turn the planning document into a project with objectives, each with tasks. "
            "Keep the user's wording; do not invent work that is not in the document.",
            _document_block(document),
            _schema_block(schema),
        ]
    )
