"""Failover, retries and circuit breaking, end to end through the gateway.

Every test here injects the outage rather than waiting for one. The fault is
aimed at a single target, because an outage that takes down every provider at
once is the one case where failover has nothing to do.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from conftest import ADMIN_TOKEN, chat_body
from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings

ADMIN = {"X-Stormdoor-Admin": ADMIN_TOKEN}

FIRST = "echo/echo-small"
SECOND = "echo/echo-large"

ROUTES = {
    "resilient": {"targets": ["echo-small", "echo-large"]},
    "solo": {"targets": ["echo-small"]},
    "smart": {
        "strategy": "complexity",
        "targets": [
            {"model": "echo-small", "tier": "cheap"},
            {"model": "echo-large", "tier": "deep"},
        ],
    },
}


def build(tmp_path, **over):
    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps(ROUTES), encoding="utf-8")
    return create_app(Settings(
        db_path=tmp_path / f"{over.pop('db', 'fo')}.db",
        admin_token=ADMIN_TOKEN,
        chaos_enabled=True,
        routes_file=routes,
        # Retries are a separate concern with their own test. Off by default
        # here so a failover test measures failover and not sleeping.
        max_retries=over.pop("max_retries", 0),
        _env_file=None,
        **over,
    ))


@pytest.fixture
def fo_app(tmp_path):
    return build(tmp_path)


@pytest_asyncio.fixture
async def fo(fo_app):
    async with AsyncClient(
        transport=ASGITransport(app=fo_app), base_url="http://stormdoor.test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def fo_auth(fo_app):
    _key, secret = await fo_app.state.store.create_key(name="failover")
    return {"Authorization": f"Bearer {secret}"}


def outage(target: str, status: int = 503) -> dict:
    return {"X-Stormdoor-Chaos": f"fault=error;status={status};target={target}"}


# ── the headline ─────────────────────────────────────────────────────────────


async def test_an_outage_on_the_first_target_is_invisible_to_the_caller(fo, fo_auth):
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="resilient"),
        headers={**fo_auth, **outage(FIRST)},
    )
    assert r.status_code == 200, "the second target should have answered"

    trail = r.json()["stormdoor"]
    assert trail["served_by"] == SECOND
    assert trail["failed_over_from"] == FIRST
    assert trail["tried"] == 2


async def test_without_a_route_there_is_nowhere_to_fall_back_to(fo, fo_auth):
    """The honest control. Failover is a property of the route, not magic."""
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="solo"),
        headers={**fo_auth, **outage(FIRST)},
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "chaos_injected"


async def test_failover_can_be_switched_off(tmp_path):
    """So the bench can measure what failover is actually worth."""
    app = build(tmp_path, failover_enabled=False, db="nofo")
    _key, secret = await app.state.store.create_key(name="k")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        r = await c.post(
            "/v1/chat/completions", json=chat_body(model="resilient"),
            headers={"Authorization": f"Bearer {secret}", **outage(FIRST)},
        )
    assert r.status_code == 503, "with failover off the outage reaches the caller"
    app.state.store.close()


async def test_a_total_outage_still_fails(fo, fo_auth):
    """Every target down is the one case failover cannot help with."""
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="resilient"),
        headers={**fo_auth, "X-Stormdoor-Chaos": "fault=error;status=503"},
    )
    assert r.status_code == 503


# ── what must not fail over ──────────────────────────────────────────────────


async def test_a_bad_request_is_not_retried_anywhere(fo, fo_auth):
    """A 400 will be a 400 at every provider.

    Trying the rest of the chain turns one bad request into several, more
    slowly, and bills the caller for the privilege.
    """
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="resilient"),
        headers={**fo_auth, **outage(FIRST, status=400)},
    )
    assert r.status_code == 400
    assert r.json()["error"]["retryable"] is False


async def test_a_key_restricted_to_one_model_is_never_failed_over(fo_app, fo):
    """An allow-list is a boundary, not a preference.

    Falling back onto a model the key was explicitly denied would be a quiet
    privilege escalation.
    """
    _key, secret = await fo_app.state.store.create_key(
        name="narrow", allowed_models=["echo-small"]
    )
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="resilient"),
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert r.status_code == 403


# ── the circuit ──────────────────────────────────────────────────────────────


async def test_repeated_failures_open_the_circuit_and_then_it_is_skipped(fo_app, fo, fo_auth):
    for _ in range(fo_app.state.settings.breaker_failure_threshold):
        await fo.post(
            "/v1/chat/completions", json=chat_body(model="resilient"),
            headers={**fo_auth, **outage(FIRST)},
        )

    health = {t["target"]: t for t in fo_app.state.breaker.snapshot()}
    assert health[FIRST]["state"] == "open"

    # With the circuit open the first target is not even tried, so the request
    # goes straight to the second one and costs no timeout on the way.
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="resilient"), headers=fo_auth
    )
    assert r.status_code == 200
    trail = r.json()["stormdoor"]
    assert trail["served_by"] == SECOND
    assert trail["tried"] == 1, "a skip is not an attempt"
    assert trail["attempts"][0]["outcome"] == "skipped"


async def test_a_callers_bad_request_never_opens_a_circuit(fo_app, fo, fo_auth):
    """One malformed prompt must not take a working model away from everyone."""
    for _ in range(10):
        await fo.post(
            "/v1/chat/completions", json=chat_body(model="solo"),
            headers={**fo_auth, **outage(FIRST, status=400)},
        )

    health = {t["target"]: t for t in fo_app.state.breaker.snapshot()}
    assert health[FIRST]["state"] == "closed"
    assert health[FIRST]["ignored_failures"] == 10

    assert (await fo.post(
        "/v1/chat/completions", json=chat_body(model="solo"), headers=fo_auth
    )).status_code == 200


async def test_a_recovered_target_is_used_again(fo_app, fo, fo_auth):
    fo_app.state.settings.breaker_cooldown_s = 0.0
    fo_app.state.breaker.config = type(fo_app.state.breaker.config)(
        failure_threshold=1, cooldown_s=0.0
    )

    await fo.post(
        "/v1/chat/completions", json=chat_body(model="resilient"),
        headers={**fo_auth, **outage(FIRST)},
    )
    assert fo_app.state.breaker.health(FIRST).state == "open"

    # Cooldown of zero, so the next request is the probe, and it succeeds.
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="resilient"), headers=fo_auth
    )
    assert r.status_code == 200
    assert r.json()["stormdoor"]["served_by"] == FIRST
    assert fo_app.state.breaker.health(FIRST).state == "closed"


# ── retries ──────────────────────────────────────────────────────────────────


async def test_a_target_is_retried_before_the_chain_moves_on(tmp_path):
    """A single 503 is more often a blip than an outage."""
    app = build(tmp_path, max_retries=2, retry_base_delay_s=0.001, db="retry")
    _key, secret = await app.state.store.create_key(name="k")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        r = await c.post(
            "/v1/chat/completions", json=chat_body(model="resilient"),
            headers={"Authorization": f"Bearer {secret}", **outage(FIRST)},
        )

    trail = r.json()["stormdoor"]
    failed = [a for a in trail["attempts"] if a["outcome"] == "failed"]
    assert len(failed) == 3, "one try plus two retries against the first target"
    assert all(a["target"] == FIRST for a in failed)
    assert trail["served_by"] == SECOND
    app.state.store.close()


# ── streaming ────────────────────────────────────────────────────────────────


async def _collect(client, body, headers):
    content, error, event = 0, None, None
    async with client.stream("POST", "/v1/chat/completions", json=body, headers=headers) as r:
        status = r.status_code
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and line != "data: [DONE]":
                payload = json.loads(line[6:])
                if event == "error":
                    error = payload
                elif payload.get("choices") and payload["choices"][0]["delta"].get("content"):
                    content += 1
                event = None
    return status, content, error


async def test_a_stream_fails_over_before_the_first_token(fo, fo_auth, fo_app):
    """Nothing has reached the caller yet, so switching provider is honest."""
    status, content, error = await _collect(
        fo, chat_body(model="resilient", stream=True), {**fo_auth, **outage(FIRST)}
    )
    assert status == 200
    assert error is None, "the caller should never learn the first target failed"
    assert content > 0

    row = (await fo.get("/admin/ledger", headers=ADMIN)).json()["data"][0]
    assert row["failed_over_from"] == FIRST
    assert row["status"] == "ok"


async def test_a_stream_cannot_fail_over_once_it_has_started(fo, fo_auth):
    """The honest limit.

    Switching provider mid-sentence would stitch two models' words into one
    answer and bill it as a single response. So the stream ends, says why, and
    is recorded as aborted rather than as a success.
    """
    status, content, error = await _collect(
        fo, chat_body(model="resilient", stream=True),
        {**fo_auth, "X-Stormdoor-Chaos": "fault=mid_stream_abort;after_chunks=4"},
    )
    assert status == 200, "the headers had already gone out"
    assert content == 3
    assert error is not None and error["error"]["code"] == "chaos_injected"


async def test_an_aborted_stream_is_not_recorded_as_a_failover(fo, fo_auth):
    await _collect(
        fo, chat_body(model="resilient", stream=True),
        {**fo_auth, "X-Stormdoor-Chaos": "fault=mid_stream_abort;after_chunks=4"},
    )
    row = (await fo.get("/admin/ledger", headers=ADMIN)).json()["data"][0]
    assert row["status"] == "aborted"
    assert row["failed_over_from"] is None


# ── complexity routing ───────────────────────────────────────────────────────


async def test_a_short_request_starts_at_the_cheap_target(fo, fo_auth):
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="smart"), headers=fo_auth
    )
    assert r.json()["stormdoor"]["served_by"] == FIRST


async def test_a_request_with_code_starts_at_the_deep_target(fo, fo_auth):
    r = await fo.post(
        "/v1/chat/completions",
        json=chat_body(model="smart", messages=[
            {"role": "user", "content": "fix this\n```python\nx=1\n```"}
        ]),
        headers=fo_auth,
    )
    assert r.json()["stormdoor"]["served_by"] == SECOND


async def test_the_caller_can_ask_for_a_tier(fo, fo_auth):
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="smart"),
        headers={**fo_auth, "X-Stormdoor-Tier": "deep"},
    )
    assert r.json()["stormdoor"]["served_by"] == SECOND


async def test_an_unknown_tier_is_refused(fo, fo_auth):
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="smart"),
        headers={**fo_auth, "X-Stormdoor-Tier": "enormous"},
    )
    assert r.status_code == 400


async def test_a_cheap_request_still_escalates_when_the_cheap_target_is_down(fo, fo_auth):
    """A tier decides where to start, never what is available."""
    r = await fo.post(
        "/v1/chat/completions", json=chat_body(model="smart"),
        headers={**fo_auth, **outage(FIRST)},
    )
    assert r.status_code == 200
    assert r.json()["stormdoor"]["served_by"] == SECOND


# ── what the operator sees ───────────────────────────────────────────────────


async def test_admin_health_reports_circuits_and_routes(fo, fo_auth):
    await fo.post(
        "/v1/chat/completions", json=chat_body(model="resilient"),
        headers={**fo_auth, **outage(FIRST)},
    )
    body = (await fo.get("/admin/health", headers=ADMIN)).json()

    assert body["failover_enabled"] is True
    targets = {t["target"]: t for t in body["targets"]}
    assert targets[FIRST]["failures"] == 1
    assert targets[SECOND]["successes"] == 1
    assert {r["name"] for r in body["routes"]} == set(ROUTES)


async def test_admin_health_needs_the_token(fo):
    assert (await fo.get("/admin/health")).status_code == 401


async def test_a_circuit_can_be_reset_by_hand(fo_app, fo, fo_auth):
    for _ in range(fo_app.state.settings.breaker_failure_threshold):
        await fo.post(
            "/v1/chat/completions", json=chat_body(model="solo"),
            headers={**fo_auth, **outage(FIRST)},
        )
    assert fo_app.state.breaker.health(FIRST).state == "open"

    r = await fo.post("/admin/breaker/reset", headers=ADMIN)
    assert r.status_code == 200
    assert fo_app.state.breaker.health(FIRST).state == "closed"


async def test_the_failover_counter_moves(fo, fo_auth):
    await fo.post(
        "/v1/chat/completions", json=chat_body(model="resilient"),
        headers={**fo_auth, **outage(FIRST)},
    )
    totals = (await fo.get("/admin/stats", headers=ADMIN)).json()["totals"]
    assert totals["failed_over"] == 1
