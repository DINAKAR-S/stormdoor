"""HTTP surface.

The routes are thin on purpose: parse, delegate to ``Gateway``, serialise. All
the interesting decisions live in ``gateway.py``, which has no idea it is
behind HTTP and can therefore be driven directly by the bench harness and the
tests without a socket in the way.

Admission runs before a streaming response is returned, so a refusal is still a
real HTTP status code (402, 403, 429) rather than an error event buried inside
a 200 stream. Only failures that happen after the first byte have to be
reported inside the stream, and those are the ones a future resume feature has
to learn to recover.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .breaker import BreakerConfig, CircuitBreaker
from .cache import SemanticCache
from .chaos import HEADER as CHAOS_HEADER
from .chaos import ChaosGate, parse_spec
from .config import Settings, get_settings
from .embeddings import build_embedder
from .errors import AuthError, BadRequest, StormdoorError, UnknownModel
from .gateway import Gateway
from .hooks import build_hook_chain
from .limits import build_limiter
from .metering import build_meter_sink, push_usage
from .pricing import PriceBook
from .providers import build_registry
from .resume import StreamBuffer
from .routing import TIER_HEADER, RouteTable
from .store import Store, VirtualKey
from .tracing import build_tracer
from .types import ChatCompletionRequest, RequestContext
from .vectorstore import build_vector_store

log = logging.getLogger("stormdoor")

# One file, no build step, no CDN. It ships inside the wheel.
DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"

# A day filter reaches a SQL comparison, so it is validated in shape before it
# gets there rather than trusted because the dashboard happened to send it.
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# An ISO date, optionally with a time, since usage windows are compared against
# the ledger's own ISO timestamps. Validated in shape before it reaches a query.
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)?)?$")


# ── admin request bodies ─────────────────────────────────────────────────────


class CreateKeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    budget_usd: float | None = Field(default=None, ge=0)
    rpm: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=1)
    allowed_models: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    tenant: str | None = Field(default=None, max_length=120)

    @field_validator("name")
    @classmethod
    def _name_is_not_only_whitespace(cls, value: str) -> str:
        # min_length counts "   " as three characters, and a key whose name
        # renders as an empty cell in the dashboard cannot be identified later.
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped


class DrillBody(BaseModel):
    """One request fired through the real path, on behalf of a key.

    The dashboard needs a way to run a failure drill without the operator
    holding a key's plaintext secret, which the gateway deliberately does not
    keep. This runs the same admission, chaos and ledger code the public
    endpoint runs, so what the dashboard shows is what a real caller would get.
    """

    key_id: str
    model: str = "echo-small"
    prompt: str = "hello from the drill"
    chaos: str | None = None
    stream: bool = False
    max_tokens: int | None = 64


# ── dependencies ─────────────────────────────────────────────────────────────


def _gateway(request: Request) -> Gateway:
    return request.app.state.gateway


async def _require_key(
    request: Request, authorization: str | None = Header(default=None)
) -> VirtualKey:
    secret = None
    if authorization and authorization.lower().startswith("bearer "):
        secret = authorization[7:].strip()
    return await request.app.state.gateway.authenticate(secret)


def _require_admin(request: Request, x_stormdoor_admin: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.admin_token
    if not expected or not x_stormdoor_admin or not secrets.compare_digest(
        x_stormdoor_admin, expected
    ):
        raise AuthError("admin token missing or wrong: send X-Stormdoor-Admin",
                        code="admin_forbidden")


# ── app ──────────────────────────────────────────────────────────────────────


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="stormdoor",
        version="0.1.0",
        description="An LLM gateway that proves itself under failure.",
        docs_url="/docs",
    )

    store = Store(settings.db_path)

    if not settings.admin_token:
        # Kept in the database rather than regenerated per process, so the token
        # you write down still works tomorrow, and so `stormdoor admin-token`
        # can tell you what it is when you forget.
        settings.admin_token, created = store.ensure_admin_token()
        log.warning(
            "\n%s\n  No STORMDOOR_ADMIN_TOKEN was set, so the gateway %s one:\n\n"
            "      %s\n\n"
            "  Use it to sign in to the dashboard. It is stored in %s and stays\n"
            "  the same across restarts. Look it up any time with:\n\n"
            "      stormdoor admin-token\n%s",
            "=" * 72,
            "generated" if created else "is using the stored",
            settings.admin_token,
            settings.db_path,
            "=" * 72,
        )
    limiter = build_limiter(settings.limiter_backend, settings.redis_url)
    registry = build_registry(settings)
    prices = PriceBook.load(settings.pricing_file)
    routes = RouteTable.load(settings.routes_file)
    breaker = CircuitBreaker(
        BreakerConfig(
            failure_threshold=settings.breaker_failure_threshold,
            cooldown_s=settings.breaker_cooldown_s,
        )
    )
    hooks = build_hook_chain(settings)
    cache = None
    if settings.cache_enabled:
        embedder = build_embedder(settings)
        vector_store = build_vector_store(settings, store)
        cache = SemanticCache(
            embedder, vector_store, store,
            similarity_floor=settings.cache_similarity_floor,
            ttl_s=settings.cache_ttl_s,
            enabled=True,
        )
    tracer = build_tracer(settings)
    meter_sink = build_meter_sink(settings)
    stream_buffer = None
    if settings.resume_enabled:
        stream_buffer = StreamBuffer(
            max_streams=settings.resume_max_streams,
            max_frames=settings.resume_max_frames,
            ttl_s=settings.resume_ttl_s,
        )

    app.state.settings = settings
    app.state.store = store
    app.state.limiter = limiter
    app.state.prices = prices
    app.state.routes = routes
    app.state.breaker = breaker
    app.state.cache = cache
    app.state.hooks = hooks
    app.state.tracer = tracer
    app.state.meter_sink = meter_sink
    app.state.stream_buffer = stream_buffer
    app.state.gateway = Gateway(
        settings=settings, store=store, limiter=limiter, registry=registry,
        prices=prices, routes=routes, breaker=breaker, cache=cache, hooks=hooks,
        tracer=tracer, stream_buffer=stream_buffer,
    )

    if routes.names():
        log.info("routes loaded: %s", routes.names())

    if cache is not None:
        log.info(
            "semantic cache ENABLED: %s backend, %s embedder, floor %.2f, ttl %.0fs",
            settings.cache_backend, cache._embedder.name,
            settings.cache_similarity_floor, settings.cache_ttl_s,
        )
    if hooks.active:
        log.info("guardrail hooks active: %s", settings.guardrail_hooks)

    if settings.otel_enabled:
        log.info("tracing ENABLED: exporting spans to %s%s",
                 settings.otel_exporter_endpoint or "the console",
                 " (with prompt/completion content)" if settings.otel_include_content else "")
    if stream_buffer is not None:
        log.info("stream resume ENABLED: buffering up to %d streams for %.0fs",
                 settings.resume_max_streams, settings.resume_ttl_s)
    if meter_sink is not None:
        log.info("metering sink configured: %s", meter_sink.name)

    if settings.chaos_enabled:
        log.warning(
            "chaos injection is ENABLED. The %s header can make this gateway fail on purpose.",
            CHAOS_HEADER,
        )

    unpriced = prices.unpriced([m["id"] for m in registry.catalogue()])
    if unpriced:
        log.warning(
            "no verified rate for %s. Requests to these are recorded at $0.00 and flagged; "
            "set STORMDOOR_PRICING_FILE to fix that.",
            unpriced,
        )

    # ── error handling ───────────────────────────────────────────────────

    @app.exception_handler(StormdoorError)
    async def _stormdoor_error(_request: Request, exc: StormdoorError) -> JSONResponse:
        headers = {}
        retry_after = getattr(exc, "retry_after_s", None)
        if retry_after is not None:
            headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
        return JSONResponse(exc.envelope(), status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        param = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        err = BadRequest(first.get("msg", "invalid request body"), param=param or None)
        return JSONResponse(err.envelope(), status_code=err.status_code)

    # ── health and discovery ─────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "version": app.version,
            "providers": registry.names(),
            "limiter": settings.limiter_backend,
            "chaos_enabled": settings.chaos_enabled,
            "failover_enabled": settings.failover_enabled,
            "routes": routes.names(),
        }

    @app.get("/v1/models")
    async def list_models(_key: VirtualKey = Depends(_require_key)) -> dict:
        return {"object": "list", "data": registry.catalogue()}

    # ── the request path ─────────────────────────────────────────────────

    @app.post("/v1/chat/completions")
    async def chat_completions(
        body: ChatCompletionRequest,
        request: Request,
        key: VirtualKey = Depends(_require_key),
        gateway: Gateway = Depends(_gateway),
    ):
        ctx = RequestContext(requested_model=body.model)
        chaos = ChaosGate(
            parse_spec(request.headers.get(CHAOS_HEADER) or settings.chaos_default),
            enabled=settings.chaos_enabled,
        )

        try:
            admission = await gateway.admit(
                key, body, tier_hint=request.headers.get(TIER_HEADER)
            )
            # Cache first, so a hit costs nothing: it is checked before the
            # budget is reserved. Streams are never cached, so they fall straight
            # through to the reservation. A hit returns a finished body here.
            if not body.stream:
                cached = await gateway.cache_lookup(key, admission, ctx)
                if cached is not None:
                    return JSONResponse(
                        cached, headers={"X-Stormdoor-Request-Id": ctx.request_id}
                    )
            # A miss (or a stream) reserves the budget now, just before the call.
            # A BudgetExceeded here is recorded as a refusal like any other.
            await gateway.reserve_budget(key, admission)
        except StormdoorError as err:
            ctx.chaos_fault = chaos.label
            await gateway.record_refusal(key, ctx, body, err)
            raise

        if body.stream:
            return StreamingResponse(
                gateway.stream(key, admission.request, admission, chaos, ctx),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    # Stops nginx buffering the stream into one lump, which
                    # would defeat the entire point of streaming.
                    "X-Accel-Buffering": "no",
                    "X-Stormdoor-Request-Id": ctx.request_id,
                },
            )

        payload = await gateway.complete(key, admission.request, admission, chaos, ctx)
        return JSONResponse(payload, headers={"X-Stormdoor-Request-Id": ctx.request_id})

    @app.get("/v1/stream/{request_id}")
    async def resume_stream(
        request_id: str,
        request: Request,
        last_event_id: int | None = None,
        key: VirtualKey = Depends(_require_key),
    ):
        """Resume a dropped stream from the last event id the client saw.

        The request id comes back in the `X-Stormdoor-Request-Id` header of the
        original stream. Reconnect here with a `Last-Event-ID` header (or a
        `?last_event_id=` query) and get the frames after it. A stream this key
        did not create, or one that has expired, is a 404, and the two are
        indistinguishable on purpose so one key cannot probe another's ids.
        """
        buffer = request.app.state.stream_buffer
        if buffer is None:
            raise BadRequest("stream resume is not enabled on this gateway",
                             code="resume_disabled")
        header_id = request.headers.get("Last-Event-ID")
        after = last_event_id if last_event_id is not None else (
            int(header_id) if header_id and header_id.lstrip("-").isdigit() else -1
        )
        replay = buffer.replay(request_id, after_id=after, key_id=key.id)
        if replay is None:
            raise UnknownModel(  # reuse the 404 envelope
                f"no resumable stream {request_id!r} for this key", param="request_id")
        if replay.too_far_behind:
            raise BadRequest(
                "the buffer no longer holds frames from that point; restart the request",
                code="resume_too_far_behind")

        async def replayed():
            for frame in replay.frames:
                yield frame
            if not replay.frames and not replay.done:
                # Nothing new yet and the stream is still live: send a comment so
                # the connection is not mistaken for a dead one, then close. The
                # client reconnects for more.
                yield ": no new frames yet\n\n"

        return StreamingResponse(
            replayed(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Stormdoor-Request-Id": request_id},
        )

    # ── admin plane ──────────────────────────────────────────────────────

    @app.post("/admin/keys", dependencies=[Depends(_require_admin)], status_code=201)
    async def create_key(body: CreateKeyBody) -> dict:
        key, secret = await store.create_key(
            name=body.name,
            budget_usd=body.budget_usd,
            rpm=body.rpm,
            tpm=body.tpm,
            allowed_models=body.allowed_models,
            expires_at=body.expires_at,
            tenant=body.tenant,
        )
        # The only time the plaintext exists outside the caller's hands.
        return {"key": key.public(), "secret": secret}

    @app.get("/admin/keys", dependencies=[Depends(_require_admin)])
    async def list_keys() -> dict:
        return {"data": [k.public() for k in await store.list_keys()]}

    @app.get("/admin/keys/{key_id}", dependencies=[Depends(_require_admin)])
    async def get_key(key_id: str) -> dict:
        key = await store.key_by_id(key_id)
        if key is None:
            raise BadRequest(f"no key with id {key_id!r}", param="key_id")
        return key.public()

    @app.post("/admin/keys/{key_id}/disable", dependencies=[Depends(_require_admin)])
    async def disable_key(key_id: str) -> dict:
        if not await store.set_enabled(key_id, False):
            raise BadRequest(f"no key with id {key_id!r}", param="key_id")
        return {"id": key_id, "enabled": False}

    @app.post("/admin/keys/{key_id}/enable", dependencies=[Depends(_require_admin)])
    async def enable_key(key_id: str) -> dict:
        if not await store.set_enabled(key_id, True):
            raise BadRequest(f"no key with id {key_id!r}", param="key_id")
        return {"id": key_id, "enabled": True}

    @app.get("/admin/keys/{key_id}/usage", dependencies=[Depends(_require_admin)])
    async def key_usage(key_id: str, limit: int = 25) -> dict:
        key = await store.key_by_id(key_id)
        if key is None:
            raise BadRequest(f"no key with id {key_id!r}", param="key_id")
        summary = await store.usage_summary(key_id, limit=min(max(limit, 1), 500))
        return {"key": key.public(), **summary}

    @app.get("/admin/ledger", dependencies=[Depends(_require_admin)])
    async def ledger(limit: int = 50, day: str | None = None) -> dict:
        if day is not None and not _DAY.fullmatch(day):
            raise BadRequest(f"day must look like YYYY-MM-DD, got {day!r}", param="day")
        return {"data": await store.recent_ledger(min(max(limit, 1), 500), day=day)}

    @app.get("/admin/spend", dependencies=[Depends(_require_admin)])
    async def spend(days: int = 14, day: str | None = None) -> dict:
        """Daily spend, and optionally the per-key split for one day.

        This is the view that answers "which day cost the most, and who spent it",
        which the running totals cannot.
        """
        if day is not None and not _DAY.fullmatch(day):
            raise BadRequest(f"day must look like YYYY-MM-DD, got {day!r}", param="day")
        series = await store.spend_by_day(min(max(days, 1), 365))
        payload: dict = {"days": series}
        if series:
            peak = max(series, key=lambda d: d["cost_usd"])
            payload["peak"] = peak if peak["cost_usd"] > 0 else None
        if day is not None:
            payload["by_key"] = await store.spend_for_day(day)
        return payload

    @app.get("/admin/stats", dependencies=[Depends(_require_admin)])
    async def stats() -> dict:
        return {
            "totals": await store.totals(),
            "providers": registry.names(),
            "models": registry.catalogue(),
            "limiter": settings.limiter_backend,
            "chaos_enabled": settings.chaos_enabled,
            "cache": cache.stats() if cache is not None else {"enabled": False},
            "guardrails": {
                "hooks": [h.name for h in hooks.pre] + [h.name for h in hooks.post],
            },
        }

    @app.get("/admin/cache", dependencies=[Depends(_require_admin)])
    async def cache_stats() -> dict:
        """Cache hit ratio and configuration. Enabled is false when the cache
        is off, which is the default, so this never 404s."""
        if cache is None:
            return {"enabled": False}
        return cache.stats()

    @app.delete("/admin/cache", dependencies=[Depends(_require_admin)])
    async def cache_invalidate() -> dict:
        """Drop every cached answer. The usual reason is a changed backing
        document: the cached answer is now wrong and no similarity floor catches
        that, because the prompt did not change, the world did."""
        if cache is None:
            return {"enabled": False, "invalidated": 0}
        n = await asyncio.to_thread(cache.invalidate)
        return {"invalidated": n}

    @app.get("/admin/usage/export", dependencies=[Depends(_require_admin)])
    async def usage_export(since: str | None = None, until: str | None = None,
                           group_by: str = "key") -> dict:
        """Usage rolled up for billing, over a window, grouped by key or tenant.

        Needs no external service: this is the ledger aggregated, which is what a
        billing job wants to read. `since` and `until` are ISO timestamps compared
        against the ledger's own; omit them for all time.
        """
        if group_by not in ("key", "tenant"):
            raise BadRequest("group_by must be 'key' or 'tenant'", param="group_by")
        for label, value in (("since", since), ("until", until)):
            if value is not None and not _TS.match(value):
                raise BadRequest(f"{label} must be an ISO timestamp", param=label)
        rows = await store.usage_export(since=since, until=until, group_by=group_by)
        return {"group_by": group_by, "since": since, "until": until, "rows": rows}

    @app.post("/admin/usage/push", dependencies=[Depends(_require_admin)])
    async def usage_push(since: str | None = None, until: str | None = None) -> dict:
        """Push the window's usage to the configured meter, exactly once.

        Idempotent: a window already pushed is refused rather than billed twice.
        Returns 400 if no meter is configured, because a push with nowhere to go
        is a mistake worth surfacing, not a silent no-op.
        """
        if meter_sink is None:
            raise BadRequest(
                "no metering sink configured; set STORMDOOR_STRIPE_API_KEY to push, "
                "or use GET /admin/usage/export to read usage without a sink",
                code="no_meter_sink")
        for label, value in (("since", since), ("until", until)):
            if value is not None and not _TS.match(value):
                raise BadRequest(f"{label} must be an ISO timestamp", param=label)
        return await push_usage(store, meter_sink, since=since, until=until)

    @app.get("/admin/health", dependencies=[Depends(_require_admin)])
    async def routing_health() -> dict:
        """Circuit state per target, and the routes behind it.

        Health comes from real traffic, so a target with no rows here has simply
        not been used. Absence is not a problem, it is silence.
        """
        return {
            "failover_enabled": settings.failover_enabled,
            "max_retries": settings.max_retries,
            "breaker": {
                "failure_threshold": settings.breaker_failure_threshold,
                "cooldown_s": settings.breaker_cooldown_s,
            },
            "targets": breaker.snapshot(),
            "routes": routes.describe(),
        }

    @app.post("/admin/breaker/reset", dependencies=[Depends(_require_admin)])
    async def reset_breaker(target: str | None = None) -> dict:
        """Force a target back to closed, for when you know it recovered."""
        breaker.reset(target)
        return {"reset": target or "all targets"}

    @app.post("/admin/drill", dependencies=[Depends(_require_admin)])
    async def drill(body: DrillBody, gateway: Gateway = Depends(_gateway)) -> dict:
        """Fire one request through the real path and report what happened."""
        key = await store.key_by_id(body.key_id)
        if key is None:
            raise BadRequest(f"no key with id {body.key_id!r}", param="key_id")

        req = ChatCompletionRequest(
            model=body.model,
            messages=[{"role": "user", "content": body.prompt}],
            stream=body.stream,
            max_tokens=body.max_tokens,
        )
        ctx = RequestContext(requested_model=body.model)
        chaos = ChaosGate(parse_spec(body.chaos), enabled=settings.chaos_enabled)

        cached = None
        try:
            admission = await gateway.admit(key, req)
            if not body.stream:
                cached = await gateway.cache_lookup(key, admission, ctx)
            if cached is None:
                await gateway.reserve_budget(key, admission)
        except UnknownModel:
            # Not a drill outcome. A model this gateway cannot route is a
            # mistake in the request, so it gets a real error status rather
            # than a 200 describing a refusal that never happened.
            raise
        except StormdoorError as err:
            # A budget, rate or permission refusal IS the result being
            # demonstrated, so it comes back as a 200 the dashboard can render.
            ctx.chaos_fault = chaos.label
            await gateway.record_refusal(key, ctx, req, err)
            return {
                "outcome": "refused at the door",
                "http_status": err.status_code,
                "cost_usd": 0.0,
                "detail": err.envelope()["error"],
                "request_id": ctx.request_id,
            }

        if cached is not None:
            return {
                "outcome": "answered from cache",
                "http_status": 200,
                "cost_usd": 0.0,
                "latency_ms": cached["stormdoor"]["latency_ms"],
                "content": cached["choices"][0]["message"]["content"],
                "usage": cached["usage"],
                "cache": cached["stormdoor"].get("cache"),
                "request_id": ctx.request_id,
            }

        if body.stream:
            # Drain the same generator the public endpoint streams, then
            # summarise it, so the dashboard can show that partial output
            # reached the caller before the failure did.
            chunks, error, done = 0, None, False
            event: str | None = None
            async for frame in gateway.stream(key, req, admission, chaos, ctx):
                for line in frame.splitlines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            done = True
                        elif event == "error":
                            error = json.loads(payload)["error"]
                        else:
                            parsed = json.loads(payload)
                            choices = parsed.get("choices") or []
                            if choices and choices[0]["delta"].get("content"):
                                chunks += 1
                        event = None
            return {
                "outcome": "stream died part way" if error else "streamed to the end",
                "http_status": 200,
                "content_chunks_delivered": chunks,
                "stream_closed_cleanly": done,
                "detail": error,
                "request_id": ctx.request_id,
                "ttft_ms": ctx.ttft_ms,
                "latency_ms": ctx.latency_ms,
            }

        try:
            payload = await gateway.complete(key, req, admission, chaos, ctx)
        except StormdoorError as err:
            return {
                "outcome": "the provider failed",
                "http_status": err.status_code,
                "cost_usd": 0.0,
                "detail": err.envelope()["error"],
                "request_id": ctx.request_id,
            }
        return {
            "outcome": "answered",
            "http_status": 200,
            "cost_usd": payload["stormdoor"]["cost_usd"],
            "latency_ms": payload["stormdoor"]["latency_ms"],
            "content": payload["choices"][0]["message"]["content"],
            "usage": payload["usage"],
            "request_id": ctx.request_id,
        }

    # ── dashboard ────────────────────────────────────────────────────────

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    return app


app = create_app  # uvicorn factory entry point: `uvicorn stormdoor.app:app --factory`
