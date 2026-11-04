"""The provider contract every model backend implements (M2 §1, B8, B11).

Calls are stateless (golden rule 3): one self-contained prompt in, one text out. Providers
declare what they support (``Capabilities``); callers always embed a compact schema in the prompt
and additionally pass ``schema`` for structured decoding when the provider supports it, so a
provider without structured outputs degrades gracefully (B8).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """A transport or server error (not a validation failure of the model's output)."""


class ModelPinningError(ProviderError):
    """A configured model's digest does not match what the server reports (B11)."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class Capabilities:
    json_mode: bool = False  # syntactically valid JSON guaranteed
    json_schema: bool = False  # schema-constrained decoding (response_format / native format)
    grammar: bool = False  # GBNF or similar
    reports_digests: bool = False  # can verify model digests (B11)


@dataclass(frozen=True)
class ModelSpec:
    """A pinned evaluator model (B11). Everything here is part of the evaPacket content address.

    ``options`` holds decoding options (temperature, seed, top_p, ...).
    """

    name: str
    digest: str | None = None  # SHA-256 of the model as served (e.g. Ollama ``/api/tags``)
    quantisation: str | None = None
    num_ctx: int = 4096
    options: dict[str, Any] = field(default_factory=lambda: {"temperature": 0.0, "seed": 0})

    def config(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "digest": self.digest,
            "quantisation": self.quantisation,
            "num_ctx": self.num_ctx,
            "options": dict(sorted(self.options.items())),
        }

    def config_hash(self) -> str:
        canonical = json.dumps(self.config(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    capabilities: Capabilities

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        schema: dict[str, Any] | None = None,
        num_ctx: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> LLMResponse: ...

    def verify_models(self, specs: list[ModelSpec]) -> dict[str, str]:
        """Check pinned digests; return ``{model: digest}`` or raise ``ModelPinningError``."""
        ...
