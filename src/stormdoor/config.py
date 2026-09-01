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

    # ── routing, retries and circuit breaking ─────────────────────────────
    # JSON file mapping a model name to an ordered list of targets to try. With
    # no file, a model name means itself and there is nothing to fall back to,
    # which is the behaviour with routing switched off.
    routes_file: Path | None = None

    # Off means try the first target once and report what happened. Useful for
    # measuring what failover is worth, which is what the bench harness does.
    failover_enabled: bool = True

    # Retries against the same target before moving to the next one. Only
    # retryable failures are retried; a 400 fails immediately everywhere.
    max_retries: int = 2
    retry_base_delay_s: float = 0.2
    retry_max_delay_s: float = 5.0

    # Consecutive retryable failures before a target's circuit opens, and how
    # long it stays open before one probe is allowed through.
    breaker_failure_threshold: int = 3
    breaker_cooldown_s: float = 30.0

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

    # ── semantic cache ────────────────────────────────────────────────────
    # Off by default: a cache changes what a caller gets back, so it is opt-in.
    # When on, a non-streaming, deterministic (temperature 0 or unset) request
    # whose prompt is close enough to a recent one is served from cache and never
    # reaches a provider.
    cache_enabled: bool = False

    # How close two prompts must be to count as the same question, as a cosine
    # similarity. High on purpose: a low floor serves a subtly-different answer as
    # if it were fresh. This is the one knob that trades hit rate against that risk.
    cache_similarity_floor: float = 0.95

    # How long a cached answer stays valid, in seconds.
    cache_ttl_s: float = 3600.0

    # "sqlite" keeps vectors in the same database and searches them in process,
    # which needs nothing and runs offline. "pinecone" is opt-in for scale and a
    # cache shared across replicas, and needs the pinecone extra plus the two
    # pinecone_* values below.
    cache_backend: Literal["sqlite", "pinecone"] = "sqlite"

    # The SQLite backend compares against at most this many recent entries per
    # scope. A real bound, not an exhaustive search: past it an older cached
    # answer is not found. Pinecone has no such bound.
    cache_max_candidates: int = 200

    # "local" is a hashed bag-of-words embedder that needs no key and runs
    # offline; it matches wording, not meaning. "openai" is a hosted model that
    # matches meaning and needs the openai extra plus an OpenAI key.
    cache_embedder: Literal["local", "openai"] = "local"
    # Width of the local embedder's vector. Wider means fewer hash collisions,
    # and a collision is the local embedder's failure mode: two different prompts
    # hashing to the same vector would be a false hit, serving the wrong answer.
    # 1024 keeps that rare for realistic prompts; a real embedding model does not
    # have this failure at all.
    cache_embedding_dim: int = 1024
    cache_embedding_model: str = "text-embedding-3-small"

    pinecone_api_key: str | None = None
    pinecone_index: str | None = None

    # ── guardrails ────────────────────────────────────────────────────────
    # A comma-separated, ordered chain of hooks. Empty means none, which is the
    # exact behaviour before guardrails existed. Known hooks: pii_redact (redact
    # PII in the prompt), pii_redact_output (redact PII in the answer),
    # injection_flag (annotate an obvious injection attempt), injection_block
    # (refuse one). Example: "pii_redact,injection_flag,pii_redact_output".
    guardrail_hooks: str = ""

    # Which PII shapes the redactors act on. Empty means all of: email, phone,
    # card, ssn, ip, key.
    guardrail_pii_kinds: str = ""

    # How many distinct injection signals must fire before a flag or a block.
    guardrail_injection_threshold: int = 1

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
