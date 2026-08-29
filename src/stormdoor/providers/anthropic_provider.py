"""Anthropic upstream, through the official SDK.

Two things this adapter is careful about.

**Message shape.** The Messages API takes the system prompt as its own
parameter and expects the conversation to alternate, starting with a user turn.
An OpenAI-shaped request satisfies neither guarantee, so the conversation is
normalised here rather than being passed through and failing upstream with a
400 the caller cannot act on.

**Error classification.** Every failure is turned into a ``ProviderError``
carrying an honest ``retryable`` flag. That flag is the input to week 2's
fallback engine, so getting it wrong means either retrying a malformed request
forever or giving up on a transient overload. Connection failures, timeouts,
408, 409, 429 and 5xx are retryable. A 400, 401, 403 or 404 is not: the request
will fail the same way on every provider you send it to.
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

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

# Model ids this adapter claims. Anything else with a "claude-" prefix is still
# routed here, so a model released after this list was written still works.
KNOWN_MODELS = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
)


def _normalise_messages(req: ChatCompletionRequest) -> list[dict[str, Any]]:
    """Flatten to alternating user/assistant turns beginning with a user turn."""
    out: list[dict[str, Any]] = []
    for msg in req.conversation():
        role = "assistant" if msg.role == "assistant" else "user"
        text = msg.text()
        if not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})

    if not out:
        out = [{"role": "user", "content": ""}]
    elif out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": "(continue)"})
    return out


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, *, default_max_tokens: int = 4096):
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "the anthropic provider needs the extra: pip install 'stormdoor[anthropic]'"
            ) from exc
        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        self._default_max_tokens = default_max_tokens

    def handles(self, model: str) -> bool:
        return model.startswith("claude-")

    def models(self) -> list[str]:
        return list(KNOWN_MODELS)

    # ── request assembly ─────────────────────────────────────────────────

    def _kwargs(self, req: ChatCompletionRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens or self._default_max_tokens,
            "messages": _normalise_messages(req),
        }
        system = req.system_prompt()
        if system:
            kwargs["system"] = system
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if req.top_p is not None:
            kwargs["top_p"] = req.top_p
        if req.stop is not None:
            kwargs["stop_sequences"] = [req.stop] if isinstance(req.stop, str) else list(req.stop)
        return kwargs

    def _fail(self, exc: Exception) -> ProviderError:
        import anthropic

        if isinstance(exc, anthropic.APIStatusError):
            status = exc.status_code
            return ProviderError(
                str(exc),
                provider=self.name,
                status_code=status if status < 500 else 502,
                retryable=status in RETRYABLE_STATUS,
                upstream_status=status,
            )
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderError(
                "anthropic request timed out", provider=self.name,
                status_code=504, retryable=True,
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(
                f"could not reach anthropic: {exc}", provider=self.name,
                status_code=502, retryable=True,
            )
        return ProviderError(str(exc), provider=self.name, status_code=502, retryable=False)

    @staticmethod
    def _usage(raw: Any) -> TokenUsage:
        cached = getattr(raw, "cache_read_input_tokens", 0) or 0
        return TokenUsage(
            input_tokens=(getattr(raw, "input_tokens", 0) or 0) + cached,
            output_tokens=getattr(raw, "output_tokens", 0) or 0,
            cached_input_tokens=cached,
        )

    @staticmethod
    def _finish_reason(stop_reason: str | None) -> str:
        return {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
            "refusal": "content_filter",
        }.get(stop_reason or "", "stop")

    # ── provider interface ───────────────────────────────────────────────

    async def complete(self, req: ChatCompletionRequest, *, timeout_s: float) -> Completion:
        try:
            message = await self._client.with_options(timeout=timeout_s).messages.create(
                **self._kwargs(req)
            )
        except Exception as exc:
            raise self._fail(exc) from exc

        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        return Completion(
            text=text,
            usage=self._usage(message.usage),
            model=message.model,
            finish_reason=self._finish_reason(getattr(message, "stop_reason", None)),
        )

    async def stream(
        self, req: ChatCompletionRequest, *, timeout_s: float
    ) -> AsyncIterator[StreamEvent]:
        client = self._client.with_options(timeout=timeout_s)
        try:
            async with client.messages.stream(**self._kwargs(req)) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and getattr(event.delta, "type", None) == "text_delta"
                    ):
                        yield TextDelta(text=event.delta.text)
                final = await stream.get_final_message()
        except Exception as exc:
            raise self._fail(exc) from exc

        yield StreamDone(
            usage=self._usage(final.usage),
            model=final.model,
            finish_reason=self._finish_reason(getattr(final, "stop_reason", None)),
        )
