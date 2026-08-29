"""Fault injection: the thing this gateway exists to make testable.

Each test here is a failure drill that a normal gateway can only rehearse by
waiting for a real outage.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from conftest import chat_body
from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.chaos import ChaosGate, parse_spec
from stormdoor.config import Settings
from stormdoor.errors import BadRequest


def chaos(spec: str) -> dict[str, str]:
    return {"X-Stormdoor-Chaos": spec}


# ── spec parsing ─────────────────────────────────────────────────────────────


def test_empty_spec_is_no_fault():
    assert parse_spec(None).active is False
    assert parse_spec("").active is False


def test_a_full_spec_parses():
    spec = parse_spec("fault=mid_stream_abort;after_chunks=5;p=0.5;seed=7")
    assert spec.fault == "mid_stream_abort"
    assert spec.after_chunks == 5
    assert spec.probability == 0.5
    assert spec.seed == 7


@pytest.mark.parametrize(
    "bad",
    [
        "fault=explode",          # not a fault we have
        "fault=error;p=7",        # probability out of range
        "fault=error;status=abc",  # not a number
        "fault=error;whoops=1",   # unknown field
        "fault",                  # no value
    ],
)
def test_a_malformed_spec_is_rejected_loudly(bad):
    """A silently ignored typo would make a drill appear to pass while doing nothing."""
    with pytest.raises(BadRequest):
        parse_spec(bad)


def test_a_seeded_probability_is_reproducible():
    spec = parse_spec("fault=error;p=0.5;seed=42")
    rolls = {ChaosGate(spec, enabled=True).armed for _ in range(20)}
    assert len(rolls) == 1, "the same seed must always produce the same decision"


# ── injected faults over HTTP ────────────────────────────────────────────────


async def test_error_fault_surfaces_as_the_chosen_status(client, auth):
    r = await client.post(
        "/v1/chat/completions", json=chat_body(),
        headers={**auth, **chaos("fault=error;status=503")},
    )
    assert r.status_code == 503
    error = r.json()["error"]
    assert error["code"] == "chaos_injected"
    assert error["retryable"] is True, "a 503 is worth retrying"


async def test_a_429_fault_is_retryable_but_a_400_is_not(client, auth):
    """The retryable flag is what the fallback engine reads, so it has to be honest."""
    too_many = await client.post(
        "/v1/chat/completions", json=chat_body(),
        headers={**auth, **chaos("fault=error;status=429")},
    )
    assert too_many.json()["error"]["retryable"] is True

    bad = await client.post(
        "/v1/chat/completions", json=chat_body(),
        headers={**auth, **chaos("fault=error;status=400")},
    )
    assert bad.json()["error"]["retryable"] is False


async def test_a_failed_request_is_recorded_as_an_error_with_its_fault(client, auth, key, store):
    virtual_key, _secret = key
    await client.post(
        "/v1/chat/completions", json=chat_body(),
        headers={**auth, **chaos("fault=error;status=503")},
    )

    summary = await store.usage_summary(virtual_key.id)
    assert summary["totals"]["errors"] == 1
    row = summary["recent"][0]
    assert row["error_code"] == "chaos_injected"
    assert "fault=error" in row["chaos_fault"], "a drill must be distinguishable from an outage"


async def test_mid_stream_abort_delivers_partial_output_then_an_error_event(client, auth):
    """The hard case: the status line already said 200, so failure must ride the stream."""
    content_frames = 0
    error_payload = None
    event = None

    async with client.stream(
        "POST", "/v1/chat/completions", json=chat_body(stream=True),
        headers={**auth, **chaos("fault=mid_stream_abort;after_chunks=4")},
    ) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and line != "data: [DONE]":
                payload = json.loads(line[6:])
                if event == "error":
                    error_payload = payload
                elif payload.get("choices") and payload["choices"][0]["delta"].get("content"):
                    content_frames += 1
                event = None

    assert content_frames == 3, "chunks before the abort should have reached the caller"
    assert error_payload is not None, "the stream must say why it stopped"
    assert error_payload["error"]["code"] == "chaos_injected"


async def test_an_aborted_stream_is_recorded_as_aborted_not_as_success(client, auth, key, store):
    virtual_key, _secret = key
    async with client.stream(
        "POST", "/v1/chat/completions", json=chat_body(stream=True),
        headers={**auth, **chaos("fault=mid_stream_abort;after_chunks=4")},
    ) as r:
        async for _line in r.aiter_lines():
            pass

    summary = await store.usage_summary(virtual_key.id)
    assert summary["totals"]["aborted"] == 1
    assert summary["totals"]["ok"] == 0


async def test_a_slow_fault_delays_without_failing(client, auth):
    fast = await client.post("/v1/chat/completions", json=chat_body(), headers=auth)
    slow = await client.post(
        "/v1/chat/completions", json=chat_body(),
        headers={**auth, **chaos("fault=slow;delay_ms=120")},
    )
    assert slow.status_code == 200

    # Compared against an unfaulted request rather than against the wall clock.
    # Windows timer granularity is about 15ms and asyncio.sleep can return a few
    # milliseconds early, so asserting "at least the requested delay" made this
    # test fail intermittently at 46ms for a 50ms sleep. The claim that actually
    # matters is that the fault made the request meaningfully slower.
    delta = slow.json()["stormdoor"]["latency_ms"] - fast.json()["stormdoor"]["latency_ms"]
    assert delta >= 80, f"slow fault only added {delta}ms"


# ── the safety catch ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def unarmed_client(tmp_path):
    app = create_app(
        Settings(db_path=tmp_path / "unarmed.db", admin_token="admin",
                 chaos_enabled=False, _env_file=None)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://stormdoor.test"
    ) as c:
        yield app, c


async def test_the_header_does_nothing_unless_chaos_is_armed(unarmed_client):
    """A caller must not be able to break a production deployment from outside."""
    app, client = unarmed_client
    _key, secret = await app.state.store.create_key(name="prod")

    r = await client.post(
        "/v1/chat/completions", json=chat_body(),
        headers={"Authorization": f"Bearer {secret}", **chaos("fault=error;status=503")},
    )
    assert r.status_code == 200
    assert r.json()["stormdoor"]["chaos_fault"] is None
