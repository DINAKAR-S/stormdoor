"""The dashboard and the endpoints behind it."""

from __future__ import annotations

import re

from conftest import ADMIN_TOKEN, chat_body

ADMIN = {"X-Stormdoor-Admin": ADMIN_TOKEN}


async def test_dashboard_is_served_and_self_contained(client):
    r = await client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]

    html = r.text
    assert "<title>stormdoor</title>" in html

    # Everything the page needs is in the page. No build step, no external
    # script, style, font or image, because a dashboard that phones out to
    # somebody else's CDN breaks on exactly the air-gapped deployments this
    # gateway is otherwise perfectly happy in.
    external = re.findall(r'(?:src|href)\s*=\s*["\'](?!/|\#)([^"\']+)', html)
    assert external == [], f"the dashboard loads something external: {external}"


async def test_root_redirects_to_the_dashboard(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/dashboard"


async def test_dashboard_endpoints_need_the_admin_token(client):
    for path in ("/admin/stats", "/admin/ledger"):
        assert (await client.get(path)).status_code == 401


async def test_stats_reports_the_gateway_shape(client, auth):
    await client.post("/v1/chat/completions", json=chat_body(), headers=auth)

    body = (await client.get("/admin/stats", headers=ADMIN)).json()
    assert body["totals"]["requests"] == 1
    assert body["totals"]["ok"] == 1
    assert body["totals"]["keys"] == 1
    assert "echo" in body["providers"]
    assert body["chaos_enabled"] is True
    assert any(m["id"] == "echo-small" for m in body["models"])


async def test_ledger_lists_requests_across_keys_newest_first(client, store):
    for name in ("first", "second"):
        _key, secret = await store.create_key(name=name)
        await client.post(
            "/v1/chat/completions", json=chat_body(),
            headers={"Authorization": f"Bearer {secret}"},
        )

    rows = (await client.get("/admin/ledger?limit=10", headers=ADMIN)).json()["data"]
    assert [r["key_name"] for r in rows] == ["second", "first"]
    assert rows[0]["status"] == "ok"


# ── the drill button ─────────────────────────────────────────────────────────


async def test_drill_answers_when_no_fault_is_asked_for(client, key):
    virtual_key, _secret = key
    res = (await client.post(
        "/admin/drill", json={"key_id": virtual_key.id, "model": "echo-small"}, headers=ADMIN
    )).json()

    assert res["outcome"] == "answered"
    assert res["http_status"] == 200
    assert res["content"].startswith("[echo:")


async def test_drill_reports_an_injected_provider_failure(client, key):
    virtual_key, _secret = key
    res = (await client.post(
        "/admin/drill",
        json={"key_id": virtual_key.id, "chaos": "fault=error;status=503"},
        headers=ADMIN,
    )).json()

    assert res["outcome"] == "the provider failed"
    assert res["http_status"] == 503
    assert res["detail"]["code"] == "chaos_injected"
    assert res["cost_usd"] == 0.0


async def test_drill_shows_partial_output_before_a_mid_stream_death(client, key):
    """The point of the panel: you can see that the caller got some of an answer."""
    virtual_key, _secret = key
    res = (await client.post(
        "/admin/drill",
        json={
            "key_id": virtual_key.id,
            "stream": True,
            "chaos": "fault=mid_stream_abort;after_chunks=4",
        },
        headers=ADMIN,
    )).json()

    assert res["outcome"] == "stream died part way"
    assert res["http_status"] == 200, "the headers had already gone out"
    assert res["content_chunks_delivered"] == 3
    assert res["detail"]["code"] == "chaos_injected"


async def test_drill_reports_a_refusal_without_calling_the_provider(client, store):
    key, _secret = await store.create_key(name="throttled", rpm=1)
    payload = {"key_id": key.id, "model": "echo-small"}

    assert (await client.post("/admin/drill", json=payload, headers=ADMIN)).json()["outcome"] == (
        "answered"
    )
    refused = (await client.post("/admin/drill", json=payload, headers=ADMIN)).json()
    assert refused["outcome"] == "refused at the door"
    assert refused["http_status"] == 429
    assert refused["cost_usd"] == 0.0


async def test_drill_rejects_an_unknown_key(client):
    r = await client.post("/admin/drill", json={"key_id": "key_nope"}, headers=ADMIN)
    assert r.status_code == 400


async def test_a_drill_lands_in_the_ledger_tagged_as_a_drill(client, key):
    virtual_key, _secret = key
    await client.post(
        "/admin/drill",
        json={"key_id": virtual_key.id, "chaos": "fault=error;status=503"},
        headers=ADMIN,
    )

    row = (await client.get("/admin/ledger", headers=ADMIN)).json()["data"][0]
    assert row["chaos_fault"] is not None
    assert (await client.get("/admin/stats", headers=ADMIN)).json()["totals"]["drills"] == 1
