"""Rate limits over the wire: the status code, the headers, and the ledger row."""

from __future__ import annotations

import pytest
from conftest import chat_body


async def test_request_rate_limit_returns_429_with_retry_after(client, store):
    _key, secret = await store.create_key(name="throttled", rpm=3)
    headers = {"Authorization": f"Bearer {secret}"}

    statuses = [
        (await client.post("/v1/chat/completions", json=chat_body(), headers=headers)).status_code
        for _ in range(5)
    ]
    assert statuses[:3] == [200, 200, 200], "the burst should be allowed"
    assert statuses[3:] == [429, 429]

    refused = await client.post("/v1/chat/completions", json=chat_body(), headers=headers)
    error = refused.json()["error"]
    assert error["limit"] == "rpm"
    # At 3 requests a minute the bucket refills one token every 20 seconds, so
    # that is honestly how long the caller has to wait. Retry-After rounds up.
    assert error["retry_after_s"] == pytest.approx(20.0, abs=0.5)
    assert refused.headers["retry-after"] == "20"


async def test_token_rate_limit_refuses_a_request_it_cannot_ever_fit(client, store):
    """A request larger than the whole bucket fails immediately rather than waiting."""
    _key, secret = await store.create_key(name="tiny-tpm", tpm=100)
    headers = {"Authorization": f"Bearer {secret}"}

    r = await client.post(
        "/v1/chat/completions", json=chat_body(max_tokens=5000), headers=headers
    )
    assert r.status_code == 429
    assert r.json()["error"]["limit"] == "tpm"


async def test_a_throttled_request_is_recorded_as_refused(client, store):
    key, secret = await store.create_key(name="throttled", rpm=1)
    headers = {"Authorization": f"Bearer {secret}"}

    await client.post("/v1/chat/completions", json=chat_body(), headers=headers)
    await client.post("/v1/chat/completions", json=chat_body(), headers=headers)

    summary = await store.usage_summary(key.id)
    assert summary["totals"]["ok"] == 1
    assert summary["totals"]["refused"] == 1
    assert summary["recent"][0]["error_code"] == "rate_limit_exceeded"


async def test_limits_are_per_key_not_global(client, store):
    _a, secret_a = await store.create_key(name="a", rpm=1)
    _b, secret_b = await store.create_key(name="b", rpm=1)

    for secret in (secret_a, secret_b):
        r = await client.post(
            "/v1/chat/completions", json=chat_body(),
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert r.status_code == 200, "one key's spending must not throttle another"


async def test_a_key_with_no_limits_is_not_throttled(client, auth):
    statuses = [
        (await client.post("/v1/chat/completions", json=chat_body(), headers=auth)).status_code
        for _ in range(20)
    ]
    assert set(statuses) == {200}
