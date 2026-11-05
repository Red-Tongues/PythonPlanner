"""LLM tree extraction (M2 §6, Part 1): planning text → validated ``ProjectTree``.

The model proposes; the ``ProjectTree`` contract decides (golden rule 2). Invalid output is
retried with a one-line correction (B7) up to the Quickfire Evaluation attempt budget. The result
goes through exactly the same ``persist_tree`` path, events and ingestion confirmation as the
deterministic parser.
"""

from __future__ import annotations

from pydantic import ValidationError

from orchestrator.config import get_settings
from orchestrator.contracts import ProjectTree
from orchestrator.errors import IngestionError
from orchestrator.llm import LLMProvider

LLM_SOURCE = "llm_extraction"


def extract_tree_with_llm(
    text: str,
    provider: LLMProvider,
    *,
    name: str = "Untitled project",
    model: str | None = None,
    max_attempts: int = 4,
) -> ProjectTree:
    from orchestrator.contracts.evaluation import concise_error, json_schema
    from orchestrator.evaluation.normalise import OutputFormatError, extract_json
    from orchestrator.evaluation.prompts import correction_suffix, tree_prompt

    schema = json_schema(ProjectTree)
    prompt = tree_prompt(text, schema, name)
    current, error = prompt, ""
    for _attempt in range(max_attempts):
        response = provider.complete(
            current,
            model=model or get_settings().model_c,
            max_tokens=4000,
            temperature=0.0,
            json_mode=True,
            schema=schema,
        )
        try:
            return ProjectTree.model_validate(extract_json(response.text))
        except (OutputFormatError, ValidationError) as exc:
            error = concise_error(exc) if isinstance(exc, ValidationError) else str(exc)
            current = prompt + correction_suffix(error)
    raise IngestionError(
        f"LLM extraction gave no valid ProjectTree after {max_attempts} attempts ({error})"
    )
