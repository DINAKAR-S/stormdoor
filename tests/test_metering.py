"""Metering: usage aggregated for billing, and pushed to a meter exactly once.

The export path is tested end to end against the real ledger. The push path is
tested against a fake sink that records what it was handed, so the idempotency
and the tenant handling are exercised without a Stripe account.
"""

from __future__ import annotations

import json

import pytest_asyncio
from conftest import chat_body
from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings
from stormdoor.metering import MeterEvent, build_events, push_usage

PRICING = {"echo-small": {"input_per_mtok": 1000.0, "output_per_mtok": 1000.0,
                          "source": "fictional", "checked_on": "2026-09-01"}}


class FakeSink:
    name = "fake"

    def __init__(self):
        self.pushed: list[MeterEvent] = []
        self.calls = 0

    def push(self, events):
        self.calls += 1
        self.pushed.extend(events)
        return len(events)


def build(tmp_path, **over):
    pricing = tmp_path / "p.json"
    pricing.write_text(json.dumps(PRICING), encoding="utf-8")
    return create_app(Settings(
        db_path=tmp_path / f"{over.pop('db', 'm')}.db", admin_token="admin",
        pricing_file=pricing, _env_file=None, **over,
    ))


@pytest_asyncio.fixture
async def app_c(tmp_path):
    app = build(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        yield app, c
    app.state.store.close()


async def _spend(c, secret, n=1):
    for _ in range(n):
        await c.post("/v1/chat/completions", json=chat_body(max_tokens=32),
                     headers={"Authorization": f"Bearer {secret}"})


# ── export ───────────────────────────────────────────────────────────────────


async def test_export_rolls_up_per_key(app_c):
    app, c = app_c
    ka, sa = await app.state.store.create_key(name="a")
    kb, sb = await app.state.store.create_key(name="b")
    await _spend(c, sa, 3)
    await _spend(c, sb, 1)
    rows = await app.state.store.usage_export(group_by="key")
    by_key = {r["key_id"]: r for r in rows}
    assert by_key[ka.id]["requests"] == 3
    assert by_key[kb.id]["requests"] == 1
    assert by_key[ka.id]["cost_usd"] > by_key[kb.id]["cost_usd"]


async def test_export_rolls_up_per_tenant(app_c):
    app, c = app_c
    _ka, sa = await app.state.store.create_key(name="a", tenant="acme")
    _kb, sb = await app.state.store.create_key(name="b", tenant="acme")
    _kc, sc = await app.state.store.create_key(name="c", tenant="globex")
    await _spend(c, sa, 2)
    await _spend(c, sb, 2)
    await _spend(c, sc, 1)
    rows = await app.state.store.usage_export(group_by="tenant")
    by_tenant = {r["tenant"]: r for r in rows}
    assert by_tenant["acme"]["requests"] == 4
    assert by_tenant["globex"]["requests"] == 1


async def test_export_excludes_refusals(app_c):
    app, c = app_c
    key, s = await app.state.store.create_key(name="a", allowed_models=["echo-large"])
    # echo-small is denied for this key, so every call is a refusal with no tokens.
    await c.post("/v1/chat/completions", json=chat_body(model="echo-small"),
                 headers={"Authorization": f"Bearer {s}"})
    rows = await app.state.store.usage_export(group_by="key")
    assert rows == [], "a refusal is not usage and must not appear in a bill"


async def test_export_endpoint_validates_group_by(app_c):
    _app, c = app_c
    r = await c.get("/admin/usage/export?group_by=nonsense",
                    headers={"X-Stormdoor-Admin": "admin"})
    assert r.status_code == 400


# ── push ─────────────────────────────────────────────────────────────────────


def test_untenanted_usage_is_not_pushed():
    rows = [{"tenant": None, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.1},
            {"tenant": "acme", "input_tokens": 20, "output_tokens": 8, "cost_usd": 0.2}]
    events = build_events(rows, period_key="k", metrics=("cost_usd",))
    assert {e.tenant for e in events} == {"acme"}, "untenanted usage cannot be billed to anyone"


async def test_a_window_is_pushed_once_then_refused(app_c):
    app, c = app_c
    _k, s = await app.state.store.create_key(name="a", tenant="acme")
    await _spend(c, s, 2)
    sink = FakeSink()

    first = await push_usage(app.state.store, sink, since=None, until=None)
    assert first["pushed"] is True
    assert first["events"] >= 1
    assert sink.calls == 1

    # Same window again: refused, and the sink is not called a second time.
    second = await push_usage(app.state.store, sink, since=None, until=None)
    assert second["pushed"] is False
    assert "already pushed" in second["reason"]
    assert sink.calls == 1, "a re-push must not reach the sink and double-bill"


async def test_push_endpoint_needs_a_sink(app_c):
    _app, c = app_c
    r = await c.post("/admin/usage/push", headers={"X-Stormdoor-Admin": "admin"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "no_meter_sink"
