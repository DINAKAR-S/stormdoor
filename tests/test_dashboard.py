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


# ── edge cases the dashboard can actually hit ────────────────────────────────


async def test_a_wrong_admin_token_is_rejected_not_just_a_missing_one(client):
    bad = {"X-Stormdoor-Admin": "not-the-token"}
    for path in ("/admin/stats", "/admin/ledger", "/admin/keys"):
        r = await client.get(path, headers=bad)
        assert r.status_code == 401, path


async def test_stats_on_a_fresh_gateway_are_zeros_not_nulls(client):
    """The tiles render these directly, so a null would print as 'null'."""
    totals = (await client.get("/admin/stats", headers=ADMIN)).json()["totals"]
    for field in ("requests", "ok", "errors", "aborted", "refused", "drills",
                  "unpriced_requests", "input_tokens", "output_tokens", "keys",
                  "keys_enabled"):
        assert totals[field] == 0, f"{field} should be 0 on a fresh gateway"
    assert totals["cost_usd"] == 0.0


async def test_ledger_on_a_fresh_gateway_is_an_empty_list(client):
    assert (await client.get("/admin/ledger", headers=ADMIN)).json()["data"] == []


async def test_ledger_limit_is_clamped_not_trusted(client):
    """The limit reaches SQL, so an absurd value must not become an absurd query."""
    for limit in (0, -5, 100000):
        r = await client.get(f"/admin/ledger?limit={limit}", headers=ADMIN)
        assert r.status_code == 200


async def test_creating_a_key_with_a_blank_name_is_refused(client):
    """A key whose name renders as an empty cell cannot be identified later."""
    for blank in ("", " ", "     "):
        r = await client.post("/admin/keys", json={"name": blank}, headers=ADMIN)
        assert r.status_code == 400, f"{blank!r} should not be accepted as a name"
        assert r.json()["error"]["type"] == "invalid_request_error"


async def test_a_key_name_is_stored_trimmed(client):
    r = await client.post("/admin/keys", json={"name": "  padded  "}, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["key"]["name"] == "padded"


async def test_creating_a_key_with_a_negative_budget_is_refused(client):
    r = await client.post("/admin/keys", json={"name": "bad", "budget_usd": -1}, headers=ADMIN)
    assert r.status_code == 400


async def test_disable_then_enable_round_trips(client, store):
    key, _secret = await store.create_key(name="toggle")

    off = await client.post(f"/admin/keys/{key.id}/disable", headers=ADMIN)
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert (await store.key_by_id(key.id)).enabled is False

    on = await client.post(f"/admin/keys/{key.id}/enable", headers=ADMIN)
    assert on.status_code == 200 and on.json()["enabled"] is True
    assert (await store.key_by_id(key.id)).enabled is True


async def test_toggling_a_key_that_does_not_exist_is_a_400(client):
    r = await client.post("/admin/keys/key_missing/disable", headers=ADMIN)
    assert r.status_code == 400


async def test_a_malformed_chaos_spec_in_a_drill_is_refused(client, key):
    """The dashboard builds the spec, but the endpoint must not trust it."""
    virtual_key, _secret = key
    r = await client.post(
        "/admin/drill",
        json={"key_id": virtual_key.id, "chaos": "fault=nonsense"},
        headers=ADMIN,
    )
    assert r.status_code == 400
    assert "nonsense" in r.json()["error"]["message"]


async def test_a_drill_against_an_unroutable_model_is_a_404(client, key):
    virtual_key, _secret = key
    r = await client.post(
        "/admin/drill", json={"key_id": virtual_key.id, "model": "gpt-nope"}, headers=ADMIN
    )
    assert r.status_code == 404


async def test_key_rows_never_leak_the_secret_or_its_hash(client, store):
    await store.create_key(name="private")
    body = (await client.get("/admin/keys", headers=ADMIN)).text
    assert "key_hash" not in body
    assert "secret" not in body


async def test_ledger_rows_carry_every_field_the_dashboard_renders(client, auth):
    await client.post("/v1/chat/completions", json=chat_body(), headers=auth)
    row = (await client.get("/admin/ledger", headers=ADMIN)).json()["data"][0]
    for field in ("ts", "key_name", "key_id", "model", "provider", "status", "error_code",
                  "input_tokens", "output_tokens", "cost_usd", "pricing_known",
                  "latency_ms", "ttft_ms", "streamed", "chaos_fault"):
        assert field in row, f"the dashboard reads {field} and it is missing"


async def test_the_dashboard_can_say_the_gateway_is_unreachable(client):
    """A monitoring view that looks fine while the thing it monitors is down is worse
    than no view at all. The page must carry an offline banner and a handler that
    survives a failed poll, not just a 401 path."""
    html = (await client.get("/dashboard")).text

    assert 'id="conn"' in html, "no offline banner element"
    assert "Cannot reach the gateway" in html, "no offline message"
    assert "no longer live" in html, "the banner must say the numbers are stale"
    # The poll loop has to distinguish "your token is wrong" from "it is down".
    assert "err.status === 401" in html
