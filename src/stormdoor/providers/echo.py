"""A deterministic local provider. No API key, no network, no cost.

This exists so that every reliability claim in the README can be reproduced by
someone who just cloned the repo. Chaos drills, rate-limit behaviour, budget
refusals and the load test all run against ``echo-small`` with nothing
installed and nothing to pay for. A benchmark you cannot rerun is a screenshot.

Output is a function of the prompt alone, so the same request always produces
the same text and the same token count. That is what lets the tests assert on
exact numbers instead of on "roughly".
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import AsyncIterator

from ..tokens import CHARS_PER_TOKEN, estimate_prompt_tokens, estimate_tokens
from ..types import (
    ChatCompletionRequest,
    Completion,
    StreamDone,
    StreamEvent,
    TextDelta,
    TokenUsage,
)

MODELS = ("echo-small", "echo-large")

_LEXICON = [
    "gateway", "request", "token", "stream", "budget", "provider", "retry",
    "failover", "latency", "cache", "ledger", "bucket", "window", "upstream",
    "fallback", "circuit", "breaker", "chunk", "resume", "header", "quota",
    "tenant", "route", "timeout", "signal", "replay", "drill",
]

_DEFAULT_WORDS = {"echo-small": 24, "echo-large": 160}


class EchoProvider:
    """Deterministic text generator used for local development and drills."""

    name = "echo"

    def handles(self, model: str) -> bool:
        return model in MODELS

    def models(self) -> list[str]:
        return list(MODELS)

    # ── generation ───────────────────────────────────────────────────────

    def _word_count(self, req: ChatCompletionRequest) -> int:
        base = _DEFAULT_WORDS.get(req.model, 24)
        if req.max_tokens is not None:
            # Roughly one token per word for this lexicon, so cap by max_tokens.
            base = min(base, max(1, req.max_tokens))
        return base

    def _text(self, req: ChatCompletionRequest) -> str:
        prompt = req.prompt_text()
        digest = hashlib.sha256(f"{req.model}\x00{prompt}".encode()).hexdigest()
        rng = random.Random(int(digest[:16], 16))
        words = [rng.choice(_LEXICON) for _ in range(self._word_count(req))]
        text = f"[echo:{digest[:8]}] " + " ".join(words)

        # Hard-truncate to max_tokens. Words are not tokens, and generating
        # `max_tokens` words produced roughly twice `max_tokens` tokens, which
        # made actual cost exceed the pre-flight worst case and quietly broke
        # the guarantee budget admission is supposed to provide. The provider
        # used to demonstrate the invariant must not be the one that violates it.
        if req.max_tokens is not None and req.max_tokens >= 1:
            max_chars = int(req.max_tokens * CHARS_PER_TOKEN)
            if len(text) > max_chars:
                text = text[:max_chars]
        return text

    def _usage(self, req: ChatCompletionRequest, output: str) -> TokenUsage:
        return TokenUsage(
            input_tokens=estimate_prompt_tokens([m.text() for m in req.messages]),
            output_tokens=estimate_tokens(output),
        )

    def _chunk_delay_s(self, req: ChatCompletionRequest) -> float:
        """Optional per-chunk delay, set with a non-standard ``echo_delay_ms`` field.

        Useful for exercising time-to-first-token and slow-stream handling
        without needing a real slow provider.
        """
        raw = getattr(req, "echo_delay_ms", None)
        try:
            return max(0.0, float(raw) / 1000.0) if raw is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    # ── provider interface ───────────────────────────────────────────────

    async def complete(self, req: ChatCompletionRequest, *, timeout_s: float) -> Completion:
        text = self._text(req)
        delay = self._chunk_delay_s(req)
        if delay:
            await asyncio.sleep(delay)
        return Completion(
            text=text,
            usage=self._usage(req, text),
            model=req.model,
            finish_reason="stop",
        )

    async def stream(
        self, req: ChatCompletionRequest, *, timeout_s: float
    ) -> AsyncIterator[StreamEvent]:
        text = self._text(req)
        delay = self._chunk_delay_s(req)
        pieces = text.split(" ")
        for i, piece in enumerate(pieces):
            if delay:
                await asyncio.sleep(delay)
            yield TextDelta(text=piece if i == 0 else " " + piece)
        yield StreamDone(usage=self._usage(req, text), model=req.model, finish_reason="stop")
