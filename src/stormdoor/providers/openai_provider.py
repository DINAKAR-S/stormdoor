"""OpenAI upstream, through the official SDK.

Thinner than the Anthropic adapter because the gateway's public surface is
already the OpenAI shape, so there is little to translate. The work is in the
same two places: honest error classification for the fallback engine, and
making sure a streamed response still reports its token usage, which requires
asking for it explicitly with ``stream_options``.

``base_url`` is configurable, so this adapter also serves any OpenAI-compatible
endpoint: a local vLLM or Ollama server, Groq, Together, OpenRouter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..errors import ProviderError
from ..types import (
    ChatCompletionRequest,
    Completion,
    StreamDone,
    StreamEvent,
    TextDelta,
    TokenUsage,
)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "the openai provider needs the extra: pip install 'stormdoor[openai]'"
            ) from exc
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self._extra_models: list[str] = []

    def handles(self, model: str) -> bool:
        return model.startswith(_PREFIXES)

    def models(self) -> list[str]:
        # Not enumerated locally: the set depends on the account and, for an
        # OpenAI-compatible base_url, on someone else's server entirely.
        return self._extra_models

    def _payload(self, req: ChatCompletionRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": [
                {"role": m.role, "content": m.text()} for m in req.messages
            ],
            "stream": stream,
        }
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.top_p is not None:
            payload["top_p"] = req.top_p
        if req.stop is not None:
            payload["stop"] = req.stop
        if stream:
            # Without this a streamed response carries no token counts at all,
            # which would silently zero out both the ledger and the budget.
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _fail(self, exc: Exception) -> ProviderError:
        import openai

        if isinstance(exc, openai.APIStatusError):
            status = exc.status_code
            return ProviderError(
                str(exc),
                provider=self.name,
                status_code=status if status < 500 else 502,
                retryable=status in RETRYABLE_STATUS,
                upstream_status=status,
            )
        if isinstance(exc, openai.APITimeoutError):
            return ProviderError(
                "openai request timed out", provider=self.name,
                status_code=504, retryable=True,
            )
        if isinstance(exc, openai.APIConnectionError):
            return ProviderError(
                f"could not reach openai: {exc}", provider=self.name,
                status_code=502, retryable=True,
            )
        return ProviderError(str(exc), provider=self.name, status_code=502, retryable=False)

    @staticmethod
    def _usage(raw: Any) -> TokenUsage:
        if raw is None:
            return TokenUsage()
        details = getattr(raw, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
        return TokenUsage(
            input_tokens=getattr(raw, "prompt_tokens", 0) or 0,
            output_tokens=getattr(raw, "completion_tokens", 0) or 0,
            cached_input_tokens=cached,
        )

    async def complete(self, req: ChatCompletionRequest, *, timeout_s: float) -> Completion:
        try:
            resp = await self._client.with_options(timeout=timeout_s).chat.completions.create(
                **self._payload(req, stream=False)
            )
        except Exception as exc:
            raise self._fail(exc) from exc

        choice = resp.choices[0]
        return Completion(
            text=choice.message.content or "",
            usage=self._usage(resp.usage),
            model=resp.model,
            finish_reason=choice.finish_reason or "stop",
        )

    async def stream(
        self, req: ChatCompletionRequest, *, timeout_s: float
    ) -> AsyncIterator[StreamEvent]:
        usage = TokenUsage()
        model = req.model
        finish_reason = "stop"
        try:
            raw = await self._client.with_options(timeout=timeout_s).chat.completions.create(
                **self._payload(req, stream=True)
            )
            async for chunk in raw:
                if chunk.usage is not None:
                    usage = self._usage(chunk.usage)
                if getattr(chunk, "model", None):
                    model = chunk.model
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = getattr(choice.delta, "content", None)
                if delta:
                    yield TextDelta(text=delta)
        except Exception as exc:
            raise self._fail(exc) from exc

        yield StreamDone(usage=usage, model=model, finish_reason=finish_reason)
