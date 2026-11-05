"""HTTP providers via httpx.MockTransport — never the network (M2 §1, B8, B11)."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from orchestrator.cli import app
from orchestrator.config import Settings
from orchestrator.db.models import (
    Event,
)
from orchestrator.evaluation import evaluate_packet
from orchestrator.ingest.llm_extract import extract_tree_with_llm
from orchestrator.llm import (
    FakeProvider,
    ModelPinningError,
    ModelSpec,
    OllamaProvider,
    OpenAICompatibleProvider,
    make_provider,
)
from tests.govfixtures import (
    event_actions,
    ollama_with_tags,
)

DIGEST = "a" * 64


def test_openai_compatible_sends_json_schema_and_parses() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": '{"value": 3}'}}],
                "usage": {"prompt_tokens": 5},
            },
        )

    provider = OpenAICompatibleProvider("http://x/v1", transport=httpx.MockTransport(handler))
    response = provider.complete(
        "p",
        model="m",
        max_tokens=9,
        temperature=0.0,
        json_mode=True,
        schema={"type": "object"},
        options={"seed": 1},
    )
    assert response.text == '{"value": 3}' and response.prompt_tokens == 5
    assert seen["response_format"]["type"] == "json_schema" and seen["seed"] == 1
    with pytest.raises(ModelPinningError):  # cannot verify digests → refuses pinned specs
        provider.verify_models([ModelSpec("m", digest=DIGEST)])


def test_ollama_sends_num_ctx_think_false_and_schema_format() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, json={"model": "m", "message": {"content": "{}"}, "eval_count": 3}
        )

    provider = OllamaProvider("http://x:11434/v1", transport=httpx.MockTransport(handler))
    provider.complete(
        "p",
        model="m",
        max_tokens=9,
        temperature=0.0,
        json_mode=True,
        schema={"type": "object"},
        num_ctx=2048,
    )
    assert seen["think"] is False and seen["format"] == {"type": "object"}
    assert seen["options"]["num_ctx"] == 2048 and seen["options"]["num_predict"] == 9


def _tags(digest: str) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "models": [
                    {"name": "eva-a", "digest": f"sha256:{digest}"},
                    {"name": "eva-b", "digest": DIGEST},
                ]
            },
        )
    )


def test_ollama_digest_pinning() -> None:
    specs = [ModelSpec("eva-a", digest=DIGEST), ModelSpec("eva-b", digest=f"sha256:{DIGEST}")]
    assert OllamaProvider(transport=_tags(DIGEST)).verify_models(specs) == {
        "eva-a": DIGEST,
        "eva-b": DIGEST,
    }
    with pytest.raises(ModelPinningError, match="digest mismatch"):
        OllamaProvider(transport=_tags("b" * 64)).verify_models(specs)
    with pytest.raises(ModelPinningError, match="no pinned digest"):
        OllamaProvider(transport=_tags(DIGEST)).verify_models([ModelSpec("eva-a")])


def test_pinning_mismatch_refuses_evaluation_and_is_visible(session: Session, tree, db_url) -> None:  # type: ignore[no-untyped-def]
    provider = OllamaProvider(transport=_tags("b" * 64))
    specs = {r: ModelSpec(f"eva-{r}", digest=DIGEST) for r in ("a", "b", "c")}
    with pytest.raises(ModelPinningError):
        evaluate_packet(session, tree.packets[0], provider, specs=specs)
    session.commit()
    event = session.scalar(select(Event).where(Event.action == "evaluation.model_pinning_failed"))
    assert event is not None and "mismatch" in event.after["error"]
    result = CliRunner().invoke(app, ["--database-url", db_url, "status"])
    assert result.exit_code == 0 and "model pinning: REFUSED" in result.output


def test_auto_provider_is_fake_without_an_endpoint() -> None:
    assert isinstance(make_provider(Settings(llm_base_url=None)), FakeProvider)
    assert isinstance(make_provider(Settings(llm_base_url="http://h:11434")), OllamaProvider)


def test_llm_tree_extraction_validates_and_retries() -> None:
    provider = FakeProvider().fail_next(2)
    tree = extract_tree_with_llm(
        "# Demo\n\n## Build\n- Write the parser\n- Write the CLI\n", provider, name="Demo"
    )
    assert tree.task_count == 2 and len(provider.calls) == 3


# ---------------------------------------------------------- model pinning fails closed (M2.1 fix 8)


def test_unpinned_or_unverifiable_evaluators_are_refused(session, tree) -> None:  # type: ignore[no-untyped-def]
    unpinned = {r: ModelSpec(f"eva-{r}") for r in ("a", "b", "c")}
    with pytest.raises(ModelPinningError, match="no pinned digest"):
        evaluate_packet(session, tree.packets[0], ollama_with_tags("d" * 64), specs=unpinned)
    pinned = {r: ModelSpec(f"eva-{r}", digest="d" * 64) for r in ("a", "b", "c")}
    with pytest.raises(ModelPinningError, match="cannot verify"):
        evaluate_packet(
            session, tree.packets[0], OpenAICompatibleProvider("http://x/v1"), specs=pinned
        )
    assert "evaluation.models_verified" not in event_actions(session)
    assert "evaluation.model_pinning_failed" in event_actions(session)


def test_pinned_and_verified_models_are_recorded(session, tree) -> None:  # type: ignore[no-untyped-def]
    from orchestrator.evaluation.evaluator import Evaluator

    pinned = {r: ModelSpec(f"eva-{r}", digest="d" * 64) for r in ("a", "b", "c")}
    Evaluator(session, ollama_with_tags("sha256:" + "d" * 64), specs=pinned).verify_models()
    event = session.scalar(select(Event).where(Event.action == "evaluation.models_verified"))
    assert event is not None and event.after["digests"] == {"eva-a": "d" * 64, "eva-b": "d" * 64}


def test_fake_provider_is_never_recorded_as_verified(session, tree) -> None:  # type: ignore[no-untyped-def]
    evaluate_packet(session, tree.packets[0], FakeProvider(), fields=["coding_intensity"])
    assert "evaluation.models_verified" not in event_actions(session)
    assert "evaluation.fake_provider" in event_actions(session)
