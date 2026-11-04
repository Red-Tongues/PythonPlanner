"""HTTP providers for local model servers (M2 §1, B8, B11, D17).

* ``OpenAICompatibleProvider`` — ``POST {base_url}/chat/completions`` (llama.cpp server, LM Studio,
  Ollama's ``/v1``). Structured outputs via ``response_format = {"type": "json_schema", ...}``
  when the server supports it, else ``{"type": "json_object"}``, else nothing (the schema is
  always in the prompt too).
* ``OllamaProvider`` — Ollama's native ``POST /api/chat`` with ``format: <schema>``, explicit
  ``options.num_ctx`` and ``think: false`` (B11), and digest pinning against ``GET /api/tags``.

Nothing here assumes CUDA (D17): model choice and placement are server configuration. Tests use
``httpx.MockTransport``; nothing in the test suite touches the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from orchestrator.llm.base import (
    Capabilities,
    LLMResponse,
    ModelPinningError,
    ModelSpec,
    ProviderError,
)


def _normalise_digest(value: str | None) -> str | None:
    if not value:
        return None
    return value.lower().removeprefix("sha256:")


@dataclass
class OpenAICompatibleProvider:
    base_url: str
    api_key: str | None = None
    timeout: float = 120.0
    capabilities: Capabilities = field(
        default_factory=lambda: Capabilities(json_mode=True, json_schema=True)
    )
    transport: httpx.BaseTransport | None = None
    name: str = "openai_compatible"

    def _client(self) -> httpx.Client:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return httpx.Client(
            base_url=self.base_url.rstrip("/"),
            timeout=self.timeout,
            headers=headers,
            transport=self.transport,
        )

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
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if options and "seed" in options:
            body["seed"] = options["seed"]
        if schema is not None and self.capabilities.json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "contract", "schema": schema, "strict": True},
            }
        elif json_mode and self.capabilities.json_mode:
            body["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        try:
            with self._client() as client:
                response = client.post("/chat/completions", json=body)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name}: unexpected response shape") from exc
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=data.get("model", model),
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    def verify_models(self, specs: list[ModelSpec]) -> dict[str, str]:
        """OpenAI-compatible servers do not report model digests, so nothing can be verified:
        refuse outright (B11 fails closed — M2.1 fix 8). Use the ollama provider for pinned
        evaluators."""
        raise ModelPinningError(
            f"{self.name} cannot verify model digests for {[s.name for s in specs]}; "
            "use the ollama provider with pinned digests"
        )


@dataclass
class OllamaProvider:
    base_url: str = "http://localhost:11434"
    timeout: float = 120.0
    capabilities: Capabilities = field(
        default_factory=lambda: Capabilities(json_mode=True, json_schema=True, reports_digests=True)
    )
    transport: httpx.BaseTransport | None = None
    name: str = "ollama"

    def _client(self) -> httpx.Client:
        root = self.base_url.rstrip("/").removesuffix("/v1")
        return httpx.Client(base_url=root, timeout=self.timeout, transport=self.transport)

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
        opts: dict[str, Any] = {**(options or {}), "temperature": temperature}
        opts["num_predict"] = max_tokens
        opts["num_ctx"] = num_ctx or opts.get("num_ctx") or 4096  # always explicit (B11)
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": opts,
        }
        if schema is not None:
            body["format"] = schema
        elif json_mode:
            body["format"] = "json"
        started = time.monotonic()
        try:
            with self._client() as client:
                response = client.post("/api/chat", json=body)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"ollama: {exc}") from exc
        try:
            text = data["message"]["content"] or ""
        except (KeyError, TypeError) as exc:
            raise ProviderError("ollama: unexpected response shape") from exc
        return LLMResponse(
            text=text,
            model=data.get("model", model),
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=int(data.get("prompt_eval_count", 0)),
            completion_tokens=int(data.get("eval_count", 0)),
        )

    def verify_models(self, specs: list[ModelSpec]) -> dict[str, str]:
        """Compare each spec's pinned digest with ``/api/tags``; refuse on any mismatch (B11)."""
        try:
            with self._client() as client:
                response = client.get("/api/tags")
                response.raise_for_status()
                served = {
                    m.get("name") or m.get("model"): _normalise_digest(m.get("digest"))
                    for m in response.json().get("models", [])
                }
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"ollama: cannot list models: {exc}") from exc
        result: dict[str, str] = {}
        problems: list[str] = []
        for spec in specs:
            digest = served.get(spec.name)
            if digest is None:
                problems.append(f"{spec.name} is not installed")
            elif spec.digest is None:
                problems.append(f"{spec.name} has no pinned digest (served {digest[:12]})")
            elif _normalise_digest(spec.digest) != digest:
                problems.append(
                    f"{spec.name} digest mismatch: pinned {_normalise_digest(spec.digest)[:12]}, "  # type: ignore[index]
                    f"served {digest[:12]}"
                )
            else:
                result[spec.name] = digest
        if problems:
            raise ModelPinningError("; ".join(problems))
        return result
