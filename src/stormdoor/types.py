"""The wire types on the way in, and the normalised types every provider speaks.

The public surface is the OpenAI chat completions shape, because that is what
existing clients already send. Internally each provider is reduced to two
things: a ``Completion`` or a stream of ``TextDelta`` ending in ``StreamDone``.
Keeping the internal shape this small is what lets the gateway fail over between
providers mid-stream without the client noticing.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool", "developer"]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Role
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None

    def text(self) -> str:
        """Flatten content to plain text for estimation and for the echo provider."""
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        parts: list[str] = []
        for block in self.content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)


class ChatCompletionRequest(BaseModel):
    # extra="allow" so provider-specific parameters survive the trip rather
    # than being silently dropped by the gateway.
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    stream: bool = False
    user: str | None = None

    def system_prompt(self) -> str | None:
        parts = [m.text() for m in self.messages if m.role in ("system", "developer")]
        joined = "\n\n".join(p for p in parts if p)
        return joined or None

    def conversation(self) -> list[ChatMessage]:
        """Messages minus the system turns, which most providers take separately."""
        return [m for m in self.messages if m.role not in ("system", "developer")]

    def prompt_text(self) -> str:
        return "\n".join(m.text() for m in self.messages)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_openai(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: TokenUsage
    model: str
    finish_reason: str = "stop"


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class StreamDone:
    usage: TokenUsage
    model: str
    finish_reason: str = "stop"


StreamEvent = TextDelta | StreamDone


@dataclass(slots=True)
class RequestContext:
    """Everything the ledger needs to know about one request.

    Created at the door and carried through routing, limiting, the provider
    call and the usage write, so a single row can answer "what happened".
    """

    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:24]}")
    started_at: float = field(default_factory=time.perf_counter)
    first_token_at: float | None = None
    chaos_fault: str | None = None

    def mark_first_token(self) -> None:
        if self.first_token_at is None:
            self.first_token_at = time.perf_counter()

    @property
    def latency_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)

    @property
    def ttft_ms(self) -> int | None:
        if self.first_token_at is None:
            return None
        return int((self.first_token_at - self.started_at) * 1000)


def completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"
