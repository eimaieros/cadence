"""Application configuration.

Everything the app needs to run comes from the environment, validated once at
import time by Pydantic. If a required value is missing or malformed the process
refuses to start rather than failing later on a request -- a config error should
be a deploy-time failure, not a 3am pager.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core -------------------------------------------------------------
    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False

    # --- Database ---------------------------------------------------------
    # asyncpg driver: the whole request path is async, so a sync driver here
    # would block the event loop and serialise every request.
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/cadence"
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # --- Auth -------------------------------------------------------------
    # MUST be overridden in production. The validator below enforces it.
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14

    # --- LLM --------------------------------------------------------------
    # No key -> the app transparently falls back to a scripted provider so the
    # whole product is demoable (and testable) with zero credentials.
    anthropic_api_key: str | None = None
    llm_model_interviewer: str = "claude-sonnet-4-6"
    llm_model_scorer: str = "claude-haiku-4-5-20251001"
    llm_max_tokens: int = 1024

    # Hard ceiling per session. An autonomous loop against a paid API does not
    # fail loudly -- it fails expensively and quietly. This is the circuit breaker.
    session_cost_ceiling_usd: float = 0.50

    # --- CORS -------------------------------------------------------------
    # NoDecode stops pydantic-settings from trying to JSON-parse this before the
    # validator below runs -- otherwise a plain comma-separated env var, which is
    # how everyone actually writes these, raises at import time.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _reject_default_secret_in_prod(cls, v: str, info) -> str:
        env = (info.data or {}).get("environment")
        if env == "production" and v == "dev-only-secret-change-me":
            raise ValueError(
                "jwt_secret must be set to a real value when environment=production"
            )
        return v

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    """Cached so the environment is read once per process, not per request."""
    return Settings()


settings = get_settings()
