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

import json
import logging
import re
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .chaos import HEADER as CHAOS_HEADER
from .chaos import ChaosGate, parse_spec
from .config import Settings, get_settings
from .errors import AuthError, BadRequest, StormdoorError, UnknownModel
from .gateway import Gateway
from .limits import build_limiter
from .pricing import PriceBook
from .providers import build_registry
from .store import Store, VirtualKey
from .types import ChatCompletionRequest, RequestContext

log = logging.getLogger("stormdoor")

# One file, no build step, no CDN. It ships inside the wheel.
DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"

# A day filter reaches a SQL comparison, so it is validated in shape before it
# gets there rather than trusted because the dashboard happened to send it.
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


# ── admin request bodies ─────────────────────────────────────────────────────


class CreateKeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    budget_usd: float | None = Field(default=None, ge=0)
    rpm: int | None = Field(default=None, ge=1)
    tpm: int | None = Field(default=None, ge=1)
    allowed_models: list[str] = Field(default_factory=list)
    expires_at: str | None = None

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

    app.state.settings = settings
    app.state.store = store
    app.state.limiter = limiter
    app.state.prices = prices
    app.state.gateway = Gateway(
        settings=settings, store=store, limiter=limiter, registry=registry, prices=prices
    )

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
        ctx = RequestContext()
        chaos = ChaosGate(
            parse_spec(request.headers.get(CHAOS_HEADER) or settings.chaos_default),
            enabled=settings.chaos_enabled,
        )

        try:
            admission = await gateway.admit(key, body)
        except StormdoorError as err:
            ctx.chaos_fault = chaos.label
            await gateway.record_refusal(key, ctx, body, err)
            raise

        if body.stream:
            return StreamingResponse(
                gateway.stream(key, body, admission, chaos, ctx),
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

        payload = await gateway.complete(key, body, admission, chaos, ctx)
        return JSONResponse(payload, headers={"X-Stormdoor-Request-Id": ctx.request_id})

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
        }

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
        ctx = RequestContext()
        chaos = ChaosGate(parse_spec(body.chaos), enabled=settings.chaos_enabled)

        try:
            admission = await gateway.admit(key, req)
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
