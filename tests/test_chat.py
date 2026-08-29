"""The happy path, and what it leaves behind in the ledger."""

from __future__ import annotations

import json

from conftest import ADMIN_TOKEN, chat_body


async def test_rejects_a_request_with_no_key(client):
    r = await client.post("/v1/chat/completions", json=chat_body())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


async def test_rejects_an_unknown_key(client):
    r = await client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"Authorization": "Bearer sd-not-a-real-key"},
    )
    assert r.status_code == 401


async def test_completion_returns_the_openai_shape(client, auth):
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth)
    assert r.status_code == 200
    body = r.json()

    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"].startswith("[echo:")
    assert body["usage"]["total_tokens"] > 0
    assert r.headers["x-stormdoor-request-id"].startswith("req_")


async def test_echo_is_deterministic(client, auth):
    first = await client.post("/v1/chat/completions", json=chat_body(), headers=auth)
    second = await client.post("/v1/chat/completions", json=chat_body(), headers=auth)
    assert (
        first.json()["choices"][0]["message"]["content"]
        == second.json()["choices"][0]["message"]["content"]
    )


async def test_completion_writes_one_ledger_row(client, auth, key, store):
    virtual_key, _secret = key
    await client.post("/v1/chat/completions", json=chat_body(), headers=auth)

    summary = await store.usage_summary(virtual_key.id)
    assert summary["totals"]["requests"] == 1
    assert summary["totals"]["ok"] == 1
    assert summary["totals"]["output_tokens"] > 0
    assert summary["recent"][0]["status"] == "ok"
    assert summary["recent"][0]["latency_ms"] is not None


async def test_unknown_model_is_a_404_not_a_500(client, auth):
    r = await client.post("/v1/chat/completions", json=chat_body(model="not-a-model"), headers=auth)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


async def test_model_allow_list_is_enforced(client, store):
    _key, secret = await store.create_key(name="narrow", allowed_models=["echo-large"])
    headers = {"Authorization": f"Bearer {secret}"}

    assert (await client.post("/v1/chat/completions", json=chat_body(model="echo-large"),
                              headers=headers)).status_code == 200
    denied = await client.post("/v1/chat/completions", json=chat_body(model="echo-small"),
                               headers=headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "model_not_allowed"


async def test_disabled_key_stops_working(client, store):
    virtual_key, secret = await store.create_key(name="doomed")
    headers = {"Authorization": f"Bearer {secret}"}
    assert (await client.post("/v1/chat/completions", json=chat_body(),
                              headers=headers)).status_code == 200

    await store.set_enabled(virtual_key.id, False)
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "key_disabled"


# ── streaming ────────────────────────────────────────────────────────────────


async def _collect_sse(client, body, headers):
    frames: list[tuple[str | None, str]] = []
    event = None
    async with client.stream("POST", "/v1/chat/completions", json=body, headers=headers) as r:
        status = r.status_code
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                frames.append((event, line[6:]))
                event = None
    return status, frames


async def test_stream_emits_deltas_then_usage_then_done(client, auth):
    status, frames = await _collect_sse(client, chat_body(stream=True), auth)
    assert status == 200

    assert frames[-1][1] == "[DONE]"
    payloads = [json.loads(data) for _event, data in frames[:-1]]

    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    text = "".join(
        p["choices"][0]["delta"].get("content", "")
        for p in payloads
        if p.get("choices")
    )
    assert text.startswith("[echo:")

    usage_frame = payloads[-1]
    assert usage_frame["usage"]["completion_tokens"] > 0


async def test_every_stream_frame_carries_an_event_id(client, auth):
    """The anchor Last-Event-ID resume will need in week 4."""
    ids: list[int] = []
    async with client.stream(
        "POST", "/v1/chat/completions", json=chat_body(stream=True), headers=auth
    ) as r:
        async for line in r.aiter_lines():
            if line.startswith("id: "):
                ids.append(int(line[4:]))

    assert ids, "no event ids were emitted"
    assert ids == list(range(len(ids))), "event ids must be gapless and monotonic"


async def test_stream_records_usage_in_the_ledger(client, auth, key, store):
    virtual_key, _secret = key
    await _collect_sse(client, chat_body(stream=True), auth)

    summary = await store.usage_summary(virtual_key.id)
    assert summary["totals"]["requests"] == 1
    assert summary["recent"][0]["streamed"] == 1
    assert summary["recent"][0]["ttft_ms"] is not None


# ── admin plane ──────────────────────────────────────────────────────────────


async def test_admin_needs_the_token(client):
    assert (await client.get("/admin/keys")).status_code == 401
    r = await client.get("/admin/keys", headers={"X-Stormdoor-Admin": ADMIN_TOKEN})
    assert r.status_code == 200


async def test_created_key_returns_its_secret_once_and_never_again(client):
    admin = {"X-Stormdoor-Admin": ADMIN_TOKEN}
    created = await client.post(
        "/admin/keys", json={"name": "issued", "budget_usd": 1.0}, headers=admin
    )
    assert created.status_code == 201
    secret = created.json()["secret"]
    key_id = created.json()["key"]["id"]

    fetched = await client.get(f"/admin/keys/{key_id}", headers=admin)
    assert "secret" not in fetched.json()
    assert fetched.json()["key_prefix"] == secret[:11]


async def test_healthz_reports_what_is_armed(client):
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok"
    assert "echo" in body["providers"]
    assert body["chaos_enabled"] is True
