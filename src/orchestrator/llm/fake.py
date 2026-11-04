"""``FakeProvider``: a deterministic stand-in for local models (tests and offline demos).

Golden rule 4: tests need no network, GPU or real model. The fake reads the machine-readable
header that every prompt from ``orchestrator.evaluation.prompts`` carries (``### TASK: ...``,
``### FIELD: ...``) plus the delimited source document, and answers with plausible values:

* **rule-based default** — keyword heuristics per field, quotes copied verbatim from the
  document as SUPPORT evidence, accuracy 8, so a full six-pass run reaches consensus;
* **scripted responses** — ``script(model, pattern, response)``: the first matching rule wins;
  ``response`` may be a string or ``callable(prompt) -> str`` (a callable can also simulate an
  edit arriving mid-evaluation);
* **invalid output** — ``fail_next(n)`` returns non-JSON for the next ``n`` calls;
* **noisy mode** (B7) — code fences, a chatty preamble, trailing commas and typographic quotes
  inside quoted evidence, as small models produce.

Every call is recorded in ``calls`` (model, task, field) so tests can assert batching order.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orchestrator.llm.base import Capabilities, LLMResponse, ModelSpec

Responder = str | Callable[[str], str]
HEADER = re.compile(r"^### ([A-Z_]+): ?(.*)$", re.MULTILINE)
DOCUMENT = re.compile(r"<<<DOCUMENT\n(.*?)\nDOCUMENT>>>", re.DOTALL)
CANDIDATE = re.compile(r"<<<CANDIDATE\n(.*?)\nCANDIDATE>>>", re.DOTALL)

KEYWORDS: dict[str, tuple[str, ...]] = {
    "coding_intensity": (
        "code",
        "implement",
        "api",
        "function",
        "script",
        "python",
        "sql",
        "database",
        "barcode",
        "camera",
        "app",
        "endpoint",
        "parser",
        "cli",
        "sync",
    ),
    "reasoning_intensity": (
        "design",
        "plan",
        "analy",
        "decide",
        "architecture",
        "strategy",
        "evaluate",
        "research",
    ),
    "ambiguity_quotient": ("?", "maybe", "tbd", "etc", "not sure", "somehow", "check w/", "??"),
    "task_parsability": (";", " and ", "\n- ", "\n* ", ", then", " also ", " plus ", "/", " + "),
}
UNSAFE = ("rm -rf", "drop database", "delete all", "exploit", "malware", "disable the triggers")


@dataclass
class Call:
    model: str
    task: str
    field: str | None
    prompt: str


@dataclass
class FakeProvider:
    noisy: bool = False
    #: per-model offsets added to extracted scores (e.g. {"fake-b": 3}) to provoke disagreement
    score_offsets: dict[str, int] = field(default_factory=dict)
    #: fixed accuracy score returned by evaluation passes
    accuracy: int = 8
    name: str = "fake"
    capabilities: Capabilities = field(
        default_factory=lambda: Capabilities(json_mode=True, json_schema=True)
    )
    calls: list[Call] = field(default_factory=list)
    _scripts: list[tuple[str | None, re.Pattern[str], Responder]] = field(default_factory=list)
    _fail_remaining: int = 0

    # ------------------------------------------------------------------ configuration

    def script(self, model: str | None, pattern: str, response: Responder) -> FakeProvider:
        self._scripts.append((model, re.compile(pattern, re.DOTALL), response))
        return self

    def fail_next(self, n: int) -> FakeProvider:
        self._fail_remaining = n
        return self

    def verify_models(self, specs: list[ModelSpec]) -> dict[str, str]:
        return {s.name: s.digest or "fake" for s in specs}

    # ------------------------------------------------------------------ the call

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        json_mode: bool = True,
        schema: dict[str, Any] | None = None,
        num_ctx: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMResponse:
        headers = dict(HEADER.findall(prompt))
        task = headers.get("TASK", "unknown")
        self.calls.append(Call(model, task, headers.get("FIELD"), prompt))
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            return LLMResponse("Sure! Here is my answer: value is probably seven.", model, 1)
        for script_model, pattern, response in self._scripts:
            if (script_model is None or script_model == model) and pattern.search(prompt):
                text = response(prompt) if callable(response) else response
                return LLMResponse(text, model, 1)
        payload = self._default(task, headers, prompt, model)
        return LLMResponse(self._render(payload), model, 1, len(prompt) // 4, 50)

    # ------------------------------------------------------------------ rule-based answers

    def _default(self, task: str, h: dict[str, str], prompt: str, model: str) -> Any:
        doc_match = DOCUMENT.search(prompt)
        document = doc_match.group(1) if doc_match else ""
        if task == "extract":
            return {"value": self._extract(h, document, model)}
        if task in ("evaluate", "accuracy", "evidence"):
            items = [i for i in h.get("ITEMS", "").split(",") if i.strip()]
            quote = self._quote(document)

            def one() -> dict[str, Any]:
                result: dict[str, Any] = {}
                if task in ("evaluate", "accuracy"):
                    result["accuracy_score"] = self.accuracy
                if task in ("evaluate", "evidence"):
                    result["evidence"] = [{"quote": quote, "tag": "SUPPORT"}] if quote else []
                if task in ("evaluate", "accuracy"):
                    result["reason"] = "The candidate is consistent with the task description."
                return result

            if h.get("KIND") == "tagset":
                applicable = set(h.get("APPLICABLE", "").replace(" ", "").split(","))
                return {
                    "items": {
                        i.strip(): (one() if i.strip() in applicable else None) for i in items
                    }
                }
            return one()
        if task == "subtasks":
            return {"subtasks": self._subtasks(document)}
        if task == "project_tree":
            from orchestrator.ingest.parser import parse_text

            tree = parse_text(document, fallback_name=h.get("NAME") or "Untitled project")
            return tree.model_dump(mode="json")
        return {"error": f"unknown task {task}"}

    def _task_text(self, document: str) -> str:
        _, _, rest = document.partition("TASK: ")
        return (rest or document).lower()

    def _extract(self, h: dict[str, str], document: str, model: str) -> Any:
        name, kind = h.get("FIELD", ""), h.get("KIND", "score")
        text = self._task_text(document)
        if kind == "bool":
            return not any(word in text for word in UNSAFE)
        if kind == "tagset":
            vocabulary = [v.strip() for v in h.get("VOCABULARY", "").split(",") if v.strip()]
            tags = [t for t in vocabulary if t != "other" and t.replace("_", " ") in text]
            return tags or (["other"] if "other" in vocabulary else vocabulary[:1])
        if kind == "text":
            first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
            return first[: int(h.get("MAX_CHARS", "200"))] or None
        words = len(text.split())
        if name in KEYWORDS:
            score = 2 + 2 * sum(text.count(k) for k in KEYWORDS[name])
        elif name == "context_volume":
            score = words // 40
        elif name == "output_wordcount":
            score = 2 + words // 30
        else:
            digest = hashlib.sha256(f"{name}|{text}".encode()).digest()
            score = 3 + digest[0] % 6
        score += self.score_offsets.get(model, 0)
        return max(0, min(10, score))

    @staticmethod
    def _quote(document: str) -> str:
        _, _, rest = document.partition("TASK: ")
        line = (rest or document).strip().splitlines()[0] if (rest or document).strip() else ""
        return " ".join(line.split()[:8])

    @staticmethod
    def _subtasks(document: str) -> list[dict[str, str]]:
        _, _, rest = document.partition("TASK DESCRIPTION:\n")
        parts = [
            p.strip(" -*\t.")
            for p in re.split(r";|\n|\. | and ", rest or document)
            if len(p.strip(" -*\t.").split()) >= 2
        ]
        if len(parts) < 2:
            parts = [f"Part {i} of the task" for i in (1, 2)]
        return [
            {"title": " ".join(p.split()[:8])[:200], "prompt_description": p} for p in parts[:4]
        ]

    # ------------------------------------------------------------------ rendering (B7 noise)

    def _render(self, payload: Any) -> str:
        if not self.noisy:
            return json.dumps(payload)
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        # typographic quotes and dashes inside evidence quotes (the quote stays verifiable
        # after conservative normalisation)
        text = re.sub(
            r'"quote": "([^"]*)"',
            lambda m: '"quote": "' + m.group(1).replace("'", "’").replace("-", "–") + '"',
            text,
        )
        text = re.sub(r"(\S)\n(\s*[}\]])", r"\1,\n\2", text)  # trailing commas
        return f"Sure! Here is the JSON you asked for:\n```json\n{text}\n```\nHope this helps."
