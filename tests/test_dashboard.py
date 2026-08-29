"""The dashboard and the endpoints behind it."""

from __future__ import annotations

import json
import re

import pytest
import pytest_asyncio
from conftest import ADMIN_TOKEN, chat_body
from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings

ADMIN = {"X-Stormdoor-Admin": ADMIN_TOKEN}


@pytest.fixture
def priced_dashboard_app(tmp_path):
    """A gateway where the echo model actually costs money, so spend is visible."""
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({
        "echo-small": {"input_per_mtok": 1000.0, "output_per_mtok": 1000.0,
                       "source": "fictional, for tests", "checked_on": "2026-08-29"},
    }), encoding="utf-8")
    return create_app(Settings(db_path=tmp_path / "spend.db", admin_token=ADMIN_TOKEN,
                               chaos_enabled=True, pricing_file=pricing, _env_file=None))


@pytest_asyncio.fixture
async def priced_dash_client(priced_dashboard_app):
    async with AsyncClient(transport=ASGITransport(app=priced_dashboard_app),
                           base_url="http://stormdoor.test") as c:
        yield c



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


# ── spend by day, and the day filter ─────────────────────────────────────────


async def test_spend_by_day_reports_a_series_and_a_peak(client, auth):
    for _ in range(3):
        await client.post("/v1/chat/completions", json=chat_body(), headers=auth)

    body = (await client.get("/admin/spend?days=14", headers=ADMIN)).json()
    assert len(body["days"]) == 1, "everything happened today"
    today = body["days"][0]
    assert today["requests"] == 3
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", today["day"])
    # The echo model is priced at zero, so there is no peak to point at.
    assert body["peak"] is None


async def test_spend_by_day_names_the_most_expensive_day(priced_dashboard_app, priced_dash_client):
    _key, secret = await priced_dashboard_app.state.store.create_key(name="spender")
    for _ in range(4):
        await priced_dash_client.post(
            "/v1/chat/completions", json=chat_body(max_tokens=16),
            headers={"Authorization": f"Bearer {secret}"},
        )

    body = (await priced_dash_client.get("/admin/spend", headers=ADMIN)).json()
    assert body["peak"] is not None
    assert body["peak"]["cost_usd"] > 0
    assert body["peak"]["day"] == body["days"][-1]["day"]


async def test_asking_for_one_day_returns_who_spent_it(priced_dashboard_app, priced_dash_client):
    _key, secret = await priced_dashboard_app.state.store.create_key(name="culprit")
    await priced_dash_client.post(
        "/v1/chat/completions", json=chat_body(max_tokens=16),
        headers={"Authorization": f"Bearer {secret}"},
    )
    day = (await priced_dash_client.get("/admin/spend", headers=ADMIN)).json()["days"][-1]["day"]

    body = (await priced_dash_client.get(f"/admin/spend?day={day}", headers=ADMIN)).json()
    assert body["by_key"][0]["key_name"] == "culprit"
    assert body["by_key"][0]["cost_usd"] > 0


async def test_the_ledger_can_be_filtered_to_one_day(client, auth):
    await client.post("/v1/chat/completions", json=chat_body(), headers=auth)
    day = (await client.get("/admin/spend", headers=ADMIN)).json()["days"][-1]["day"]

    today = (await client.get(f"/admin/ledger?day={day}", headers=ADMIN)).json()["data"]
    assert len(today) == 1

    other = (await client.get("/admin/ledger?day=2001-01-01", headers=ADMIN)).json()["data"]
    assert other == [], "a day with nothing in it is empty, not everything"


async def test_a_malformed_day_is_refused_before_it_reaches_sql(client):
    """The filter reaches a query, so its shape is checked rather than trusted."""
    for bad in ("not-a-day", "2026-13-99x", "'; DROP TABLE usage_records; --", "2026-8-1"):
        r = await client.get("/admin/ledger", params={"day": bad}, headers=ADMIN)
        assert r.status_code == 400, f"{bad!r} should be refused"
        r2 = await client.get("/admin/spend", params={"day": bad}, headers=ADMIN)
        assert r2.status_code == 400, f"{bad!r} should be refused"

    # and the table is still there
    assert (await client.get("/admin/ledger", headers=ADMIN)).status_code == 200


async def test_the_days_range_is_clamped(client):
    for days in (0, -3, 99999):
        assert (await client.get(f"/admin/spend?days={days}", headers=ADMIN)).status_code == 200


async def test_the_dashboard_ships_the_day_filter_ui(client):
    html = (await client.get("/dashboard")).text
    assert 'id="spend-days"' in html
    assert "renderSpendDays" in html
    assert 'id="clear-day"' in html


# ── finding the admin token ──────────────────────────────────────────────────


def test_a_generated_admin_token_survives_a_restart(tmp_path):
    """Regenerating per process meant one missed log line locked you out."""
    from stormdoor.store import Store

    store = Store(tmp_path / "tok.db")
    first, created = store.ensure_admin_token()
    assert created is True
    assert len(first) == 32
    store.close()

    reopened = Store(tmp_path / "tok.db")
    second, created_again = reopened.ensure_admin_token()
    reopened.close()

    assert second == first, "the token must be the same after a restart"
    assert created_again is False


def test_an_app_with_no_configured_token_uses_the_stored_one(tmp_path):
    from stormdoor.config import Settings as S

    def make():
        return create_app(S(db_path=tmp_path / "app.db", admin_token=None, _env_file=None))

    first = make().state.settings.admin_token
    second = make().state.settings.admin_token
    assert first and first == second, "restarting must not invalidate the token"


def test_an_explicit_token_wins_over_the_stored_one(tmp_path):
    from stormdoor.config import Settings as S
    from stormdoor.store import Store

    store = Store(tmp_path / "wins.db")
    stored, _ = store.ensure_admin_token()
    store.close()

    app = create_app(S(db_path=tmp_path / "wins.db", admin_token="chosen-by-me",
                       _env_file=None))
    assert app.state.settings.admin_token == "chosen-by-me"
    assert app.state.settings.admin_token != stored


async def test_the_sign_in_screen_says_where_to_find_the_token(client):
    html = (await client.get("/dashboard")).text
    assert "stormdoor admin-token" in html, "the gate must name the command that prints it"


# ── the provider health panel ────────────────────────────────────────────────


async def test_the_dashboard_ships_the_provider_health_panel(client):
    html = (await client.get("/dashboard")).text
    assert 'id="targets"' in html
    assert "renderTargets" in html
    assert 'id="reset-breakers"' in html
    # A state name is jargon. The page has to say what it means for traffic.
    assert "skipped, circuit open" in html


async def test_health_is_empty_before_anything_is_called(client):
    body = (await client.get("/admin/health", headers=ADMIN)).json()
    assert body["targets"] == [], "silence is not a problem, it is silence"


async def test_health_fills_in_from_real_traffic(client, auth):
    await client.post("/v1/chat/completions", json=chat_body(), headers=auth)
    body = (await client.get("/admin/health", headers=ADMIN)).json()

    target = next(t for t in body["targets"] if t["target"] == "echo/echo-small")
    assert target["state"] == "closed"
    assert target["successes"] == 1
