"""The request path. Admission, limits, the upstream call, the ledger write.

Order matters and is deliberate:

1. **Authenticate** the virtual key.
2. **Authorise** the model against the key's allow-list.
3. **Resolve** the provider, so an unroutable model fails before it consumes
   any of the caller's rate-limit budget.
4. **Rate limit**, requests first and then tokens. Requests first because it is
   the cheaper check and the one more likely to reject.
5. **Admit against budget**, pricing the worst case the request could cost.
6. **Inject a fault**, if this request was selected for one.
7. **Call upstream.**
8. **Record**, always, whatever happened.

Steps 1 to 5 are free: no upstream call has been made, so a refused request
costs nothing but a SQLite write. That is the entire argument for doing
admission before the call rather than reconciling spend afterwards.

Every terminal outcome writes exactly one ledger row, including refusals and
including streams that died half way. A request that leaves no trace is a
request you cannot bill, debug, or learn from.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .chaos import ChaosGate
from .errors import (
    AuthError,
    BudgetExceeded,
    ForbiddenError,
    ProviderError,
    RateLimited,
    StormdoorError,
)
from .limits import Limiter
from .pricing import PriceBook
from .providers import Provider, ProviderRegistry
from .store import Store, VirtualKey
from .tokens import estimate_prompt_tokens
from .types import (
    ChatCompletionRequest,
    Completion,
    RequestContext,
    StreamDone,
    TextDelta,
    TokenUsage,
    completion_id,
)

log = logging.getLogger("stormdoor.gateway")


@dataclass(slots=True)
class Admission:
    provider: Provider
    prompt_tokens_estimate: int
    max_output_tokens: int
    estimated_cost_usd: float | None  # None when the model has no verified rate


class Gateway:
    def __init__(
        self,
        *,
        settings,
        store: Store,
        limiter: Limiter,
        registry: ProviderRegistry,
        prices: PriceBook,
    ):
        self.settings = settings
        self.store = store
        self.limiter = limiter
        self.registry = registry
        self.prices = prices

    # ── 1. authenticate ──────────────────────────────────────────────────

    async def authenticate(self, secret: str | None) -> VirtualKey:
        if not secret:
            raise AuthError("missing bearer token: send Authorization: Bearer sd-...")
        key = await self.store.key_by_secret(secret)
        if key is None:
            raise AuthError("unknown API key")
        if not key.enabled:
            raise AuthError("this API key is disabled", code="key_disabled")
        if key.is_expired():
            raise AuthError("this API key has expired", code="key_expired")
        return key

    # ── 2 to 5. admission ────────────────────────────────────────────────

    async def admit(self, key: VirtualKey, req: ChatCompletionRequest) -> Admission:
        if not key.allows_model(req.model):
            raise ForbiddenError(
                f"key {key.name!r} is not allowed to use model {req.model!r}", param="model"
            )

        provider = self.registry.resolve(req.model)

        prompt_tokens = estimate_prompt_tokens([m.text() for m in req.messages])
        max_output = req.max_tokens or self.settings.default_max_tokens

        if key.rpm is not None:
            decision = await self.limiter.take(f"{key.id}:rpm", 1.0, per_minute=key.rpm)
            if not decision.allowed:
                raise RateLimited(
                    f"request rate limit of {key.rpm}/min exceeded",
                    retry_after_s=decision.retry_after_s,
                    limit="rpm",
                )

        if key.tpm is not None:
            cost = float(prompt_tokens + max_output)
            decision = await self.limiter.take(f"{key.id}:tpm", cost, per_minute=key.tpm)
            if not decision.allowed:
                raise RateLimited(
                    f"token rate limit of {key.tpm}/min exceeded "
                    f"(this request needs up to {int(cost)} tokens)",
                    retry_after_s=decision.retry_after_s,
                    limit="tpm",
                )

        estimate = self.prices.max_cost_usd(req.model, prompt_tokens, max_output)

        if (
            key.budget_usd is not None
            and estimate is not None
            and key.spent_usd + estimate > key.budget_usd
        ):
            raise BudgetExceeded(
                f"this request could cost up to ${estimate:.4f}, which would take "
                f"key {key.name!r} past its ${key.budget_usd:.2f} budget "
                f"(${key.spent_usd:.4f} already spent)",
                spent_usd=key.spent_usd,
                budget_usd=key.budget_usd,
                estimate_usd=estimate,
            )

        return Admission(
            provider=provider,
            prompt_tokens_estimate=prompt_tokens,
            max_output_tokens=max_output,
            estimated_cost_usd=estimate,
        )

    # ── 8. ledger ────────────────────────────────────────────────────────

    async def _record(
        self,
        *,
        key: VirtualKey,
        ctx: RequestContext,
        model: str,
        provider: str,
        usage: TokenUsage,
        status: str,
        streamed: bool,
        error_code: str | None = None,
    ) -> float:
        cost, priced = self.prices.cost_usd(
            model, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens
        )
        await self.store.record_usage(
            key_id=key.id,
            request_id=ctx.request_id,
            model=model,
            provider=provider,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cost_usd=cost,
            pricing_known=priced,
            status=status,
            error_code=error_code,
            latency_ms=ctx.latency_ms,
            ttft_ms=ctx.ttft_ms,
            streamed=streamed,
            chaos_fault=ctx.chaos_fault,
        )
        if not priced:
            log.warning(
                "no verified rate for model %r: request %s recorded at $0.00 and flagged",
                model, ctx.request_id,
            )
        return cost

    async def record_refusal(
        self, key: VirtualKey, ctx: RequestContext, req: ChatCompletionRequest,
        err: StormdoorError,
    ) -> None:
        """A request turned away at the door still belongs in the history."""
        await self.store.record_usage(
            key_id=key.id,
            request_id=ctx.request_id,
            model=req.model,
            provider="stormdoor",
            input_tokens=0,
            output_tokens=0,
            cached_input_tokens=0,
            cost_usd=0.0,
            pricing_known=True,
            status="refused",
            error_code=err.code,
            latency_ms=ctx.latency_ms,
            ttft_ms=None,
            streamed=bool(req.stream),
            chaos_fault=ctx.chaos_fault,
        )

    # ── 6 and 7. the call ────────────────────────────────────────────────

    async def complete(
        self,
        key: VirtualKey,
        req: ChatCompletionRequest,
        admission: Admission,
        chaos: ChaosGate,
        ctx: RequestContext,
    ) -> dict:
        ctx.chaos_fault = chaos.label
        provider = admission.provider
        try:
            await chaos.before_call()
            result: Completion = await provider.complete(
                req, timeout_s=self.settings.request_timeout_s
            )
        except ProviderError as err:
            await self._record(
                key=key, ctx=ctx, model=req.model, provider=provider.name,
                usage=TokenUsage(), status="error", streamed=False, error_code=err.code,
            )
            raise

        ctx.mark_first_token()
        cost = await self._record(
            key=key, ctx=ctx, model=result.model, provider=provider.name,
            usage=result.usage, status="ok", streamed=False,
        )
        return _completion_body(req, result, cost, ctx)

    async def stream(
        self,
        key: VirtualKey,
        req: ChatCompletionRequest,
        admission: Admission,
        chaos: ChaosGate,
        ctx: RequestContext,
    ) -> AsyncIterator[str]:
        """Yield raw SSE frames.

        Every frame carries an ``id:``. Nothing reads it yet; it is the anchor
        week 4's ``Last-Event-ID`` resume needs, and adding it now costs a line
        while retrofitting it later would change the wire format under clients.

        Once the first byte is out the HTTP status is already 200, so a failure
        after that point cannot be signalled by a status code. It is sent as an
        SSE ``error`` event instead, and recorded as ``aborted`` rather than
        ``error``, because the caller did receive part of an answer and was
        charged for the tokens that were actually produced.
        """
        ctx.chaos_fault = chaos.label
        provider = admission.provider
        cid = completion_id()
        created = int(time.time())
        index = 0
        usage = TokenUsage()
        model_used = req.model
        finish_reason = "stop"
        status = "ok"
        error_code: str | None = None
        text_seen = 0

        def frame(payload: dict, *, event: str | None = None) -> str:
            nonlocal index
            head = f"id: {index}\n"
            if event:
                head += f"event: {event}\n"
            index += 1
            return head + f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"

        try:
            await chaos.before_call()

            yield frame(_chunk(cid, created, model_used, delta={"role": "assistant"}))

            async for event in provider.stream(req, timeout_s=self.settings.request_timeout_s):
                if isinstance(event, TextDelta):
                    ctx.mark_first_token()
                    text_seen += 1
                    if chaos.should_abort_stream(text_seen):
                        raise chaos.abort_error()
                    yield frame(_chunk(cid, created, model_used, delta={"content": event.text}))
                elif isinstance(event, StreamDone):
                    usage = event.usage
                    model_used = event.model
                    finish_reason = event.finish_reason

            yield frame(_chunk(cid, created, model_used, delta={}, finish_reason=finish_reason))
            yield frame(
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_used,
                    "choices": [],
                    "usage": usage.as_openai(),
                }
            )
            yield "data: [DONE]\n\n"

        except ProviderError as err:
            status = "aborted" if text_seen else "error"
            error_code = err.code
            yield frame(err.envelope(), event="error")
            yield "data: [DONE]\n\n"

        finally:
            await self._record(
                key=key, ctx=ctx, model=model_used, provider=provider.name,
                usage=usage, status=status, streamed=True, error_code=error_code,
            )


def _chunk(
    cid: str, created: int, model: str, *, delta: dict, finish_reason: str | None = None
) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _completion_body(
    req: ChatCompletionRequest, result: Completion, cost_usd: float, ctx: RequestContext
) -> dict:
    return {
        "id": completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text},
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": result.usage.as_openai(),
        # Namespaced so an OpenAI client that validates the response ignores it.
        "stormdoor": {
            "request_id": ctx.request_id,
            "cost_usd": round(cost_usd, 8),
            "latency_ms": ctx.latency_ms,
            "chaos_fault": ctx.chaos_fault,
        },
    }
