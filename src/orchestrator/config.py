"""Runtime configuration (environment variables prefixed ``ORCH_``)."""

from __future__ import annotations

import logging

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Kernel settings. Every value can be overridden with an ``ORCH_<NAME>`` env variable."""

    model_config = SettingsConfigDict(env_prefix="ORCH_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./orchestrator.db"
    production_repo_path: str = "./production_repo"
    workbench_path: str = "./workbench"
    log_level: str = "WARNING"  # INFO shows kernel activity; WARNING keeps CLI output readable

    # M2 — evaluator models (B11: pinned by digest; D17: configuration, not code)
    llm_provider: str = "auto"  # auto | fake | ollama | openai ("auto": fake unless a URL is set)
    llm_base_url: str | None = None  # e.g. http://localhost:11434 (Ollama)
    llm_api_key: str | None = None
    llm_timeout: float = 120.0
    model_a: str = "fake-a"
    model_b: str = "fake-b"
    model_c: str = "fake-c"  # aggregator / local generation (subtask lists)
    model_a_digest: str | None = None
    model_b_digest: str | None = None
    model_c_digest: str | None = None
    model_a_quant: str | None = None
    model_b_quant: str | None = None
    model_c_quant: str | None = None


def get_settings() -> Settings:
    """Return freshly loaded settings (cheap; keeps tests free of cached global state)."""
    return Settings()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Alembic logs every migration context at INFO; only surface its warnings.
    logging.getLogger("alembic").setLevel(max(logging.WARNING, logging.getLogger().level))
