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
from dataclasses import dataclass, replace

from .attempts import AttemptLog, backoff_delay
from .breaker import CircuitBreaker
from .cache import SemanticCache
from .chaos import ChaosGate
from .errors import (
    AuthError,
    BudgetExceeded,
    ForbiddenError,
    ProviderError,
    RateLimited,
    StormdoorError,
)
from .hooks import HookChain, HookNotes
from .limits import Limiter
from .pricing import PriceBook
from .providers import Provider, ProviderRegistry
from .resume import StreamBuffer
from .routing import Complexity, RouteTable, score
from .store import Store, VirtualKey
from .tokens import estimate_prompt_tokens
from .tracing import NoopTracer, Tracer, request_attributes
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
    # request ends, whichever way it ends. Zero until the reservation is made,
    # which happens only after the cache misses so a hit costs nothing.
    reserved_usd: float = 0.0
    # The request as it should go upstream: the original, unless a guardrail
    # rewrote it (redacted a card number out of the prompt). Everything after
    # admission uses this, not the caller's original.
    request: ChatCompletionRequest | None = None
    # What the guardrails did, surfaced on the response so a redaction is never
    # silent.
    notes: HookNotes | None = None
    key_id: str | None = None

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
        cache: SemanticCache | None = None,
        hooks: HookChain | None = None,
        tracer: Tracer | None = None,
        stream_buffer: StreamBuffer | None = None,
    ):
        self.settings = settings
        self.store = store
        self.limiter = limiter
        self.registry = registry
        self.prices = prices
        self.routes = routes or RouteTable({})
        self.breaker = breaker or CircuitBreaker()
        self.cache = cache
        self.hooks = hooks or HookChain()
        self.tracer = tracer or NoopTracer()
        self.stream_buffer = stream_buffer

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
        # Guardrails run first, so everything downstream — token estimate,
        # pricing, the prompt sent upstream, and the cache key — sees the
        # redacted request, not the raw one. A blocking guardrail (an obvious
        # injection) raises here and is recorded as a refusal, the same as any
        # other admission failure. Redaction rewrites the request; the rest of
        # the path uses `req` below, which is now the effective one.
        notes = HookNotes()
        req = self.hooks.on_request(req, notes)

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

        # The allow-list gates every model that could actually be contacted, not
        # just the one the caller named. This is the whole boundary: a key
        # restricted to one model must never be failed over onto another, because
        # that is a quiet privilege escalation. The reachable set is exactly the
        # chain _run_chain will walk, so it depends on whether failover is on: with
        # it off only the first target is ever called, with it on any target in
        # the chain can be. Checking `req.model` here instead was the bug — when
        # a route is keyed after a real model the caller is allowed (the common
        # `{"gpt-4o-mini": {targets: [gpt-4o-mini, claude-haiku]}}` form), that
        # short-circuit waved every fallback through the boundary.
        reachable = candidates if self.settings.failover_enabled else candidates[:1]
        denied = next((c for c in reachable if not key.allows_model(c.model)), None)
        if denied is not None:
            named = (
                repr(req.model)
                if denied.model == req.model
                else f"{req.model!r} (via {denied.model!r})"
            )
            raise ForbiddenError(
                f"key {key.name!r} is not allowed to use model {named}", param="model"
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

        # Note: the budget is not reserved here. Reservation is deferred to
        # reserve_budget(), called only once the cache has missed, so a cache hit
        # never touches the budget. Rate limits above are not deferred: a hit
        # still consumes rate-limit quota, because a limiter is abuse protection
        # and a flood of cache hits is still a flood, but it costs no money.
        return Admission(
            candidates=candidates,
            prompt_tokens_estimate=prompt_tokens,
            max_output_tokens=max_output,
            estimated_cost_usd=estimate,
            complexity=complexity,
            reserved_usd=0.0,
            request=req,
            notes=notes,
            key_id=key.id,
        )

    async def reserve_budget(self, key: VirtualKey, admission: Admission) -> None:
        """Claim the worst-case cost atomically, just before the upstream call.

        Split out of admit so it runs only on a cache miss. The atomic claim is
        the fix for the check-then-act race that let a $0.20 key spend $1.50: see
        Store.reserve. Idempotent per admission — it sets reserved_usd, and a
        second call would double-reserve, so it is called exactly once on the
        miss path.
        """
        estimate = admission.estimated_cost_usd
        if key.budget_usd is None or estimate is None:
            return
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
        admission.reserved_usd = estimate

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
        billed: bool = True,
    ) -> float:
        # A cache hit records its real token counts for transparency but is not
        # billed: the model was never called, so pricing those tokens again would
        # charge twice for one answer. `billed=False` forces the cost to zero and
        # marks it priced, so the "no verified rate" warning does not fire on a
        # free row.
        if billed:
            cost, priced = self.prices.cost_usd(
                model, usage.input_tokens, usage.output_tokens, usage.cached_input_tokens
            )
        else:
            cost, priced = 0.0, True
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
        self._trace(ctx, requested_model=ctx.requested_model or model, served_model=model,
                    provider=provider, usage=usage, cost=cost, status=status,
                    attempts=trail.tried, failed_over_from=trail.failed_over_from,
                    error=error_code if status in ("error", "aborted") else None)
        return cost

    def _trace(self, ctx: RequestContext, *, requested_model: str, served_model: str,
               provider: str, usage: TokenUsage, cost: float, status: str,
               attempts: int, failed_over_from: str | None, error: str | None) -> None:
        """Emit the one span for this request. A no-op tracer makes this free, so
        the call is unconditional and the cost of tracing being off is nothing.

        Timestamps are reconstructed from the request's own latency rather than
        wrapping the whole path in a live span: the gateway already records every
        terminal outcome here, in one place, and a retro span keeps instrumentation
        to that one place instead of threading a span through routing and failover.
        """
        end_ns = time.time_ns()
        attrs = request_attributes(
            requested_model=requested_model, served_model=served_model, provider=provider,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cost_usd=cost, status=status, cache_hit=ctx.cache_hit, attempts=attempts,
            failed_over_from=failed_over_from, chaos_fault=ctx.chaos_fault,
            prompt_preview=ctx.prompt_preview, completion_preview=ctx.completion_preview,
        )
        try:
            self.tracer.record_request(
                name="stormdoor.chat", start_unix_ns=end_ns - ctx.latency_ms * 1_000_000,
                end_unix_ns=end_ns, attributes=attrs, error=error,
            )
        except Exception:  # noqa: BLE001
            # Tracing is observability, not the request. A broken exporter must
            # never turn a served answer into a failed one, so its failure is
            # logged and swallowed rather than propagated.
            log.warning("trace export failed for request %s", ctx.request_id, exc_info=True)

    def _maybe_preview(self, ctx: RequestContext, req: ChatCompletionRequest,
                       completion_text: str) -> None:
        """Stash prompt and completion previews on the context, but only when the
        operator has explicitly opted into content in traces. Off by default,
        because a span usually leaves the process and the prompt should not."""
        if getattr(self.settings, "otel_include_content", False):
            ctx.prompt_preview = req.prompt_text()
            ctx.completion_preview = completion_text

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
        self._trace(ctx, requested_model=req.model, served_model=req.model,
                    provider="stormdoor", usage=TokenUsage(), cost=0.0, status="refused",
                    attempts=0, failed_over_from=None, error=err.code)

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

    # ── the cache, in front of the call ──────────────────────────────────

    async def cache_lookup(
        self, key: VirtualKey, admission: Admission, ctx: RequestContext
    ) -> dict | None:
        """A hit returns a finished response body and records it; a miss is None.

        Runs after admission (so the allow-list, rate limits and guardrails have
        already applied) and before any budget reservation (so a hit costs
        nothing). The cache uses wall-clock time for expiry, not the monotonic
        clock the breaker uses, because cache rows outlive the process and a
        monotonic timestamp means nothing after a restart.
        """
        if self.cache is None or not self.cache.enabled:
            return None
        req = admission.request
        hit = await asyncio.to_thread(
            self.cache.lookup, admission.key_id, req, now=time.time()
        )
        if hit is None:
            return None
        ctx.mark_first_token()
        ctx.cache_hit = True
        self._maybe_preview(ctx, req, hit.completion.text)
        await self._record(
            key=key, ctx=ctx, model=hit.completion.model, provider="cache",
            usage=hit.completion.usage, status="cache_hit", streamed=False,
            reservation=0.0, log=AttemptLog(), billed=False,
        )
        body = _completion_body(req, hit.completion, 0.0, ctx, AttemptLog())
        body["stormdoor"]["cache"] = {"hit": True, "similarity": round(hit.similarity, 4)}
        _attach_notes(body, admission.notes)
        return body

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
        req = admission.request or req
        notes = admission.notes or HookNotes()

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
        ctx.cache_hit = False
        # Output guardrails run on the answer before it is billed, cached or
        # returned, so a redacted response is what the caller sees and what the
        # cache stores. Storing the raw answer would leave PII sitting in the
        # cache to be served again later.
        result = replace(result, text=self.hooks.on_response(result.text, notes))
        self._maybe_preview(ctx, req, result.text)
        cost = await self._record(
            key=key, ctx=ctx, model=result.model, provider=target.provider.name,
            usage=result.usage, status="ok", streamed=False,
            reservation=admission.reserved_usd, log=log,
        )
        if self.cache is not None and self.cache.enabled:
            await asyncio.to_thread(
                self.cache.store, admission.key_id, req, result, now=time.time()
            )
        body = _completion_body(req, result, cost, ctx, log)
        if self.cache is not None and self.cache.cacheable(req):
            body["stormdoor"]["cache"] = {"hit": False}
        _attach_notes(body, notes)
        return body

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

        Guardrails on a stream are split: input hooks (redaction, injection
        blocking) already ran in admission, so the prompt sent upstream is the
        redacted one. Output redaction runs per delta as chunks arrive, because
        buffering the whole stream to filter it would defeat streaming. The one
        gap that leaves — a PII token split across two deltas slipping through —
        is stated in the README. The semantic cache does not apply to streams at
        all, also stated: replaying a stream from cache is a separate feature.
        """
        ctx.chaos_fault = chaos.label
        log = AttemptLog()
        req = admission.request or req
        notes = admission.notes or HookNotes()
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

        # Every frame is copied into the resume buffer as it goes out, keyed by
        # the request id the caller has in a response header, so a dropped
        # connection can be picked up from the last id it saw. The buffer is
        # opened only when resume is enabled and only for a stream.
        buf = self.stream_buffer
        if buf is not None:
            buf.open(ctx.request_id, key.id)

        def emit(s: str) -> str:
            if buf is not None:
                buf.append(ctx.request_id, s)
            return s

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

            yield emit(frame(_chunk(cid, created, model_used, delta={"role": "assistant"})))

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
                    piece = self.hooks.on_response(event.text, notes)
                    yield emit(frame(_chunk(cid, created, model_used, delta={"content": piece})))
                elif isinstance(event, StreamDone):
                    usage = event.usage
                    model_used = event.model
                    finish_reason = event.finish_reason

            yield emit(frame(_chunk(cid, created, model_used, delta={},
                                    finish_reason=finish_reason)))
            yield emit(frame(
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_used,
                    "choices": [],
                    "usage": usage.as_openai(),
                }
            ))
            yield emit("data: [DONE]\n\n")

        except ProviderError as err:
            status = "aborted" if text_seen else "error"
            error_code = err.code
            yield emit(frame(err.envelope(), event="error"))
            yield emit("data: [DONE]\n\n")

        finally:
            if buf is not None:
                buf.mark_done(ctx.request_id)
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


def _attach_notes(body: dict, notes: HookNotes | None) -> None:
    """Surface what the guardrails did on the response, so a redaction is
    auditable rather than a silent change to the answer."""
    if notes is None:
        return
    public = notes.public()
    if public is not None:
        body["stormdoor"]["guardrails"] = public
