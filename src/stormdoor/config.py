"""Runtime settings.

Every value can be set through the environment with the ``STORMDOOR_`` prefix,
for example ``STORMDOOR_PORT=9000``. Nothing here requires Docker, Postgres or
Redis: the defaults run the whole gateway off a single SQLite file and an
in-process rate limiter, which is what makes the failure demos runnable on a
laptop with no infrastructure at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_db_path() -> Path:
    return Path.cwd() / "stormdoor.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STORMDOOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── server ────────────────────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "info"

    # ── storage ───────────────────────────────────────────────────────────
    db_path: Path = Field(default_factory=_default_db_path)

    # ── pricing ───────────────────────────────────────────────────────────
    # JSON file that overrides or extends the built-in rate card. See
    # stormdoor/pricing.py for the format and for why unpriced models are
    # flagged rather than guessed.
    pricing_file: Path | None = None

    # ── rate limiting ─────────────────────────────────────────────────────
    # "memory" keeps buckets in this process. "redis" shares them across
    # replicas and needs the `redis` extra plus redis_url.
    limiter_backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None

    # ── admin plane ───────────────────────────────────────────────────────
    # Required to reach /admin/*. Generated on first run if left unset and
    # printed once to the log.
    admin_token: str | None = None

    # ── providers ─────────────────────────────────────────────────────────
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    # Upstream call timeout in seconds.
    request_timeout_s: float = 120.0

    # Sent upstream when the caller omits max_tokens. Anthropic requires the
    # field, and pre-flight budget admission prices the worst case, so a
    # gateway-wide default of 64k would refuse almost every request against a
    # small budget. 4096 keeps admission useful; callers who want more say so.
    default_max_tokens: int = 4096

    # ── failure injection ─────────────────────────────────────────────────
    # Off unless explicitly enabled. When off, the X-Stormdoor-Chaos header is
    # ignored entirely, so a production deployment cannot be poked by a caller.
    chaos_enabled: bool = False

    # Applied to every request when set, e.g. "fault=error;status=503;p=0.1".
    # A per-request header overrides it.
    chaos_default: str | None = None

    @property
    def db_url(self) -> str:
        return str(self.db_path)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Replace the singleton. Used by tests and by the bench harness."""
    global _settings
    _settings = settings
