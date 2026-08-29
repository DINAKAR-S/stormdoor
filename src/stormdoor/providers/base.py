"""The contract every upstream has to satisfy.

Deliberately tiny. A provider turns a request into either one ``Completion`` or
a stream of ``TextDelta`` ending in exactly one ``StreamDone``. Anything richer
(tool calls, images, thinking blocks) comes later, and anything
provider-specific rides through untouched in the request's extra fields.

The narrowness is the feature. Failover has to abandon a failing provider
mid-response and continue on another one, and that is only tractable while the
thing being resumed is a plain text stream with a token count at the end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ..types import ChatCompletionRequest, Completion, StreamEvent


@runtime_checkable
class Provider(Protocol):
    name: str

    def handles(self, model: str) -> bool:
        """True when this provider can serve the given model id."""
        ...

    def models(self) -> list[str]:
        """Model ids this provider advertises, for GET /v1/models."""
        ...

    async def complete(
        self, req: ChatCompletionRequest, *, timeout_s: float
    ) -> Completion: ...

    def stream(
        self, req: ChatCompletionRequest, *, timeout_s: float
    ) -> AsyncIterator[StreamEvent]: ...
