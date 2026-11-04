"""LLM providers (M2 §1): the protocol, a deterministic fake and HTTP providers."""

from __future__ import annotations

from orchestrator.config import Settings
from orchestrator.llm.base import (
    Capabilities,
    LLMProvider,
    LLMResponse,
    ModelPinningError,
    ModelSpec,
    ProviderError,
)
from orchestrator.llm.fake import FakeProvider
from orchestrator.llm.http import OllamaProvider, OpenAICompatibleProvider


def make_provider(settings: Settings) -> LLMProvider:
    """The configured provider. ``auto`` means FakeProvider unless an endpoint is configured
    (M2 acceptance criterion 1: a fresh checkout evaluates with no model server)."""
    kind = settings.llm_provider
    if kind == "auto":
        kind = "ollama" if settings.llm_base_url else "fake"
    if kind == "fake":
        return FakeProvider()
    if kind == "ollama":
        return OllamaProvider(
            base_url=settings.llm_base_url or "http://localhost:11434",
            timeout=settings.llm_timeout,
        )
    if kind == "openai":
        if not settings.llm_base_url:
            raise ValueError("ORCH_LLM_BASE_URL is required for the openai provider")
        return OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout,
        )
    raise ValueError(f"unknown ORCH_LLM_PROVIDER {settings.llm_provider!r}")


def model_specs(settings: Settings, num_ctx: int = 4096) -> dict[str, ModelSpec]:
    """Evaluator models A and B (and aggregator C) with their pins (B11)."""
    return {
        role: ModelSpec(
            name=getattr(settings, f"model_{role}"),
            digest=getattr(settings, f"model_{role}_digest"),
            quantisation=getattr(settings, f"model_{role}_quant"),
            num_ctx=num_ctx,
        )
        for role in ("a", "b", "c")
    }


__all__ = [
    "Capabilities",
    "FakeProvider",
    "LLMProvider",
    "LLMResponse",
    "ModelPinningError",
    "ModelSpec",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "make_provider",
    "model_specs",
]
