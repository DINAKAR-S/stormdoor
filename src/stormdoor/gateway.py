"""The request path. Admission, limits, the upstream call, the ledger write.

Order matters and is deliberate:

1. **Authenticate** the virtual key.
2. **Resolve** the route into an ordered list of targets to try.
3. **Authorise** the model against the key's allow-list.
4. **Rate limit**, requests first and then tokens. Requests first because it is
   the cheaper check and the one more likely to reject.
5. **Admit against budget**, pricing the worst case the request could cost.
6. **Inject a fault**, if this request was selected for one.
7. **Call upstream**, retrying and falling back down the chain.
8. **Record**, always, whatever happened, including what was tried on the way.

Steps 1 to 5 are free: no upstream call has been made, so a refused request
costs nothing but a SQLite write. That is the entire argument for doing
admission before the call rather than reconciling spend afterwards.

Every terminal outcome writes exactly one ledger row, including refusals and
including streams that died half way. A request that leaves no trace is a
request you cannot bill, debug, or learn from.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .attempts import AttemptLog, backoff_delay
from .breaker import CircuitBreaker
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
from .routing import Complexity, RouteTable, score
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


@dataclass(frozen=True, slots=True)
class Candidate:
    """One thing the gateway can actually call: a provider and a model id."""

    provider: Provider
    model: str

    @property
    def key(self) -> str:
        """The breaker key. Provider and model together, because one model being
        overloaded while the rest of the account is fine is the common case."""
        return f"{self.provider.name}/{self.model}"

    def outbound(self, req: ChatCompletionRequest) -> ChatCompletionRequest:
        """The request as this provider should see it."""
        if self.model == req.model:
            return req
        return req.model_copy(update={"model": self.model})


@dataclass(slots=True)
class Admission:
    # In the order they will be tried. Always at least one.
    candidates: list[Candidate]
    prompt_tokens_estimate: int
    max_output_tokens: int
    estimated_cost_usd: float | None  # None when no candidate has a verified rate
    complexity: Complexity | None = None
    # What was claimed against the budget and must be given back when the
    # request ends, whichever way it ends.
    reserved_usd: float = 0.0

    @property
    def first(self) -> Candidate:
        return self.candidates[0]


class Gateway:
    def __init__(
        self,
        *,
        settings,
        store: Store,
        limiter: Limiter,
        registry: ProviderRegistry,
        prices: PriceBook,
        routes: RouteTable | None = None,
        breaker: CircuitBreaker | None = None,
    ):
        self.settings = settings
        self.store = store
        self.limiter = limiter
        self.registry = registry
        self.prices = prices
        self.routes = routes or RouteTable({})
        self.breaker = breaker or CircuitBreaker()

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

    async def admit(
        self, key: VirtualKey, req: ChatCompletionRequest, *, tier_hint: str | None = None
    ) -> Admission:
        complexity = None
        route = self.routes.get(req.model)
        if route is not None and route.strategy == "complexity":
            complexity = score(req, hint=tier_hint)

        targets = self.routes.candidates(req.model, complexity)

        # Every target is resolved up front. A chain with a typo in its third
        # entry should fail now, while it is still a configuration error, not in
        # six weeks during an outage when the gateway finally reaches for it.
        candidates: list[Candidate] = []
        for target in targets:
            provider, upstream = self.registry.resolve(target.model)
            candidates.append(Candidate(provider=provider, model=upstream))

        # The allow-list is checked against what the caller asked for and against
        # every model that could actually serve it. A key restricted to one model
        # must not be quietly failed over onto another.
        allowed = key.allows_model(req.model) or all(
            key.allows_model(c.model) for c in candidates
        )
        if not allowed:
            raise ForbiddenError(
                f"key {key.name!r} is not allowed to use model {req.model!r}", param="model"
            )

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

        # Priced across the whole chain, not just the first target. A request
        # that falls back onto a pricier model must not be able to exceed a
        # ceiling that was checked against a cheaper one. This can refuse a
        # request the first target could have afforded, which is the safe
        # direction to be wrong in when the alternative is overspending.
        priced = [
            self.prices.max_cost_usd(c.model, prompt_tokens, max_output) for c in candidates
        ]
        known = [p for p in priced if p is not None]
        estimate = max(known) if known else None

        # Claim the worst case atomically before the call, rather than comparing
        # against a spend figure other in-flight requests are about to change.
        # See Store.reserve for why a read-then-decide let a $0.20 key spend $1.50.
        reserved = 0.0
        if key.budget_usd is not None and estimate is not None:
            granted, spent, committed = await self.store.reserve(key.id, estimate)
            if not granted:
                raise BudgetExceeded(
                    f"this request could cost up to ${estimate:.4f}, which would take "
                    f"key {key.name!r} past its ${key.budget_usd:.2f} budget "
                    f"(${spent:.4f} spent, ${committed - spent:.4f} reserved by requests "
                    f"already in flight)",
                    spent_usd=spent,
                    budget_usd=key.budget_usd,
                    estimate_usd=estimate,
                )
            reserved = estimate

        return Admission(
            candidates=candidates,
            prompt_tokens_estimate=prompt_tokens,
            max_output_tokens=max_output,
            estimated_cost_usd=estimate,
            complexity=complexity,
            reserved_usd=reserved,
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
        reservation: float = 0.0,
        log: AttemptLog | None = None,
    ) -> float:
        cost, priced = self.prices.cost_usd(
            model, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens
        )
        trail = log or AttemptLog()
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
            reservation=reservation,
            attempts=trail.tried,
            failed_over_from=trail.failed_over_from,
        )
        if not priced:
            logging.getLogger("stormdoor.gateway").warning(
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
            attempts=0,
            failed_over_from=None,
        )

    # ── 6 and 7. the call, with retries and failover ─────────────────────

    async def _run_chain(
        self,
        req: ChatCompletionRequest,
        admission: Admission,
        chaos: ChaosGate,
        log: AttemptLog,
    ) -> tuple[Candidate, Completion]:
        """Walk the chain until one target answers, or none can.

        Three rules, and the middle one is the one that is easy to get wrong:

        * A **non-retryable** failure stops everything. A malformed request will
          be malformed at every provider, so trying the rest turns one 400 into
          four, more slowly.
        * An **open circuit** is skipped rather than tried. That is the whole
          value of a breaker: not spending a timeout to rediscover that
          something is down.
        * Retries against one target come **before** moving to the next,
          because a single 503 is more often a blip than an outage.
        """
        last: ProviderError | None = None

        # Decided once, up front. Filtering inside the loop was wrong: a target
        # whose circuit was open got skipped with `continue`, which jumped past
        # the failover check at the bottom and quietly used the next target even
        # with failover switched off. The bench drill caught it as a 99% success
        # rate in a run that was supposed to fail every request.
        chain = (
            admission.candidates
            if self.settings.failover_enabled
            else admission.candidates[:1]
        )

        for target in chain:
            if not self.breaker.allow(target.key):
                log.skipped(target.key, "circuit open")
                continue

            for attempt in range(self.settings.max_retries + 1):
                try:
                    await chaos.before_call(target.key)
                    result = await target.provider.complete(
                        target.outbound(req), timeout_s=self.settings.request_timeout_s
                    )
                except ProviderError as err:
                    last = err
                    self.breaker.record_failure(
                        target.key, retryable=err.retryable, error=err.code
                    )
                    log.failed(target.key, err.code)

                    if not err.retryable:
                        raise
                    if attempt < self.settings.max_retries:
                        await asyncio.sleep(
                            backoff_delay(
                                attempt,
                                base=self.settings.retry_base_delay_s,
                                cap=self.settings.retry_max_delay_s,
                            )
                        )
                        continue
                    break
                else:
                    self.breaker.record_success(target.key)
                    log.succeeded(target.key)
                    return target, result

        if last is not None:
            raise last
        raise ProviderError(
            "every target for this model has an open circuit",
            provider="stormdoor",
            status_code=503,
            retryable=True,
        )

    async def complete(
        self,
        key: VirtualKey,
        req: ChatCompletionRequest,
        admission: Admission,
        chaos: ChaosGate,
        ctx: RequestContext,
    ) -> dict:
        ctx.chaos_fault = chaos.label
        log = AttemptLog()

        try:
            target, result = await self._run_chain(req, admission, chaos, log)
        except ProviderError as err:
            await self._record(
                key=key, ctx=ctx, model=admission.first.model,
                provider=admission.first.provider.name,
                usage=TokenUsage(), status="error", streamed=False, error_code=err.code,
                reservation=admission.reserved_usd, log=log,
            )
            raise

        ctx.mark_first_token()
        cost = await self._record(
            key=key, ctx=ctx, model=result.model, provider=target.provider.name,
            usage=result.usage, status="ok", streamed=False,
            reservation=admission.reserved_usd, log=log,
        )
        return _completion_body(req, result, cost, ctx, log)

    async def _open_stream(
        self,
        req: ChatCompletionRequest,
        admission: Admission,
        chaos: ChaosGate,
        log: AttemptLog,
    ):
        """Get a stream that has already produced its first event.

        This is the reason failover works for streams at all. The first event is
        pulled here, before anything reaches the caller, so a target that fails
        at the start can be abandoned quietly and the next one tried.

        Once that first event has gone out, failover is over. Switching provider
        mid-sentence would stitch two models' words into one answer and bill it
        as a single response, which is not a recovery, it is a lie about what
        wrote the text.
        """
        last: ProviderError | None = None

        # Decided once, up front. Filtering inside the loop was wrong: a target
        # whose circuit was open got skipped with `continue`, which jumped past
        # the failover check at the bottom and quietly used the next target even
        # with failover switched off. The bench drill caught it as a 99% success
        # rate in a run that was supposed to fail every request.
        chain = (
            admission.candidates
            if self.settings.failover_enabled
            else admission.candidates[:1]
        )

        for target in chain:
            if not self.breaker.allow(target.key):
                log.skipped(target.key, "circuit open")
                continue

            for attempt in range(self.settings.max_retries + 1):
                try:
                    await chaos.before_call(target.key)
                    iterator = target.provider.stream(
                        target.outbound(req), timeout_s=self.settings.request_timeout_s
                    ).__aiter__()
                    first = await iterator.__anext__()
                except StopAsyncIteration:
                    last = ProviderError(
                        "the provider returned an empty stream",
                        provider=target.provider.name, status_code=502, retryable=True,
                    )
                    self.breaker.record_failure(target.key, retryable=True,
                                                error="empty_stream")
                    log.failed(target.key, "empty_stream")
                    break
                except ProviderError as err:
                    last = err
                    self.breaker.record_failure(
                        target.key, retryable=err.retryable, error=err.code
                    )
                    log.failed(target.key, err.code)
                    if not err.retryable:
                        raise
                    if attempt < self.settings.max_retries:
                        await asyncio.sleep(
                            backoff_delay(
                                attempt,
                                base=self.settings.retry_base_delay_s,
                                cap=self.settings.retry_max_delay_s,
                            )
                        )
                        continue
                    break
                else:
                    self.breaker.record_success(target.key)
                    log.succeeded(target.key)
                    return target, iterator, first

        if last is not None:
            raise last
        raise ProviderError(
            "every target for this model has an open circuit",
            provider="stormdoor", status_code=503, retryable=True,
        )

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
        a ``Last-Event-ID`` resume will need, and adding it now costs a line
        while retrofitting it later would change the wire format under clients.

        Failover happens before the first frame leaves. After that the status
        line already says 200, so a failure cannot be reported by status code.
        It is sent as an SSE ``error`` event and recorded as ``aborted`` rather
        than ``error``, because the caller did receive part of an answer and was
        charged for the tokens that were really produced.
        """
        ctx.chaos_fault = chaos.label
        log = AttemptLog()
        cid = completion_id()
        created = int(time.time())
        index = 0
        usage = TokenUsage()
        model_used = admission.first.model
        finish_reason = "stop"
        status = "ok"
        error_code: str | None = None
        text_seen = 0
        provider_name = admission.first.provider.name

        def frame(payload: dict, *, event: str | None = None) -> str:
            nonlocal index
            head = f"id: {index}\n"
            if event:
                head += f"event: {event}\n"
            index += 1
            return head + f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"

        try:
            # Nothing has been sent yet, so a failure in here can still be
            # answered by trying somebody else.
            target, iterator, first = await self._open_stream(req, admission, chaos, log)
            provider_name = target.provider.name
            model_used = target.model

            yield frame(_chunk(cid, created, model_used, delta={"role": "assistant"}))

            async def replayed():
                # The first event was already pulled to decide failover, so it
                # is put back at the front rather than lost.
                yield first
                async for event in iterator:
                    yield event

            async for event in replayed():
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
                key=key, ctx=ctx, model=model_used, provider=provider_name,
                usage=usage, status=status, streamed=True, error_code=error_code,
                reservation=admission.reserved_usd, log=log,
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
    req: ChatCompletionRequest, result: Completion, cost_usd: float,
    ctx: RequestContext, trail: AttemptLog,
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
            **trail.public(),
        },
    }
