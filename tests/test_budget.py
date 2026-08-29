"""Budgets are enforced before the upstream call, not reconciled after it.

These tests price the local echo model through a pricing override file, which
also exercises the STORMDOOR_PRICING_FILE path. The rates are deliberately
round and fictional: $1000 per million tokens each way, so one token costs
exactly $0.001 and the arithmetic in the assertions is readable.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio
from conftest import chat_body
from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings
from stormdoor.pricing import PriceBook

RATE_PER_TOKEN = 0.001


@pytest.fixture
def priced_app(tmp_path):
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        json.dumps(
            {
                "echo-small": {
                    "input_per_mtok": 1000.0,
                    "output_per_mtok": 1000.0,
                    "source": "fictional, for tests",
                    "checked_on": "2026-08-29",
                }
            }
        ),
        encoding="utf-8",
    )
    return create_app(
        Settings(
            db_path=tmp_path / "budget.db",
            admin_token="admin",
            pricing_file=pricing,
            default_max_tokens=4096,
            # Needed by the reservation tests, which use injected failures to
            # prove a failed request gives its claim back.
            chaos_enabled=True,
            _env_file=None,
        )
    )


@pytest_asyncio.fixture
async def priced_client(priced_app):
    async with AsyncClient(
        transport=ASGITransport(app=priced_app), base_url="http://stormdoor.test"
    ) as c:
        yield c


async def test_refuses_when_the_worst_case_would_break_the_budget(priced_app, priced_client):
    """A request whose ceiling exceeds the budget never reaches the provider."""
    _key, secret = await priced_app.state.store.create_key(name="tiny", budget_usd=1.0)

    # No max_tokens, so admission prices the 4096-token default: about $4.10.
    r = await priced_client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"Authorization": f"Bearer {secret}"},
    )

    assert r.status_code == 402
    error = r.json()["error"]
    assert error["code"] == "budget_exceeded"
    assert error["budget"]["budget_usd"] == 1.0
    assert error["budget"]["spent_usd"] == 0.0
    assert error["budget"]["estimated_cost_usd"] > 1.0


async def test_a_capped_request_fits_the_same_budget(priced_app, priced_client):
    """Same key, same budget: asking for less output is admitted."""
    _key, secret = await priced_app.state.store.create_key(name="tiny", budget_usd=1.0)

    r = await priced_client.post(
        "/v1/chat/completions",
        json=chat_body(max_tokens=10),
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert r.status_code == 200
    assert r.json()["stormdoor"]["cost_usd"] > 0


async def test_a_refusal_is_still_recorded(priced_app, priced_client):
    """A request turned away at the door belongs in the history too."""
    key, secret = await priced_app.state.store.create_key(name="tiny", budget_usd=1.0)
    await priced_client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"Authorization": f"Bearer {secret}"},
    )

    summary = await priced_app.state.store.usage_summary(key.id)
    assert summary["totals"]["refused"] == 1
    assert summary["totals"]["cost_usd"] == 0.0
    assert summary["recent"][0]["error_code"] == "budget_exceeded"


async def test_spend_accumulates_and_eventually_closes_the_door(priced_app, priced_client):
    """Repeated small calls walk the key up to its ceiling and then stop."""
    key, secret = await priced_app.state.store.create_key(name="walker", budget_usd=0.05)
    headers = {"Authorization": f"Bearer {secret}"}

    statuses = []
    for _ in range(12):
        r = await priced_client.post(
            "/v1/chat/completions", json=chat_body(max_tokens=10), headers=headers
        )
        statuses.append(r.status_code)

    assert statuses[0] == 200, "the first call should have been admitted"
    assert 402 in statuses, "the key should eventually run out of budget"
    assert all(s == 402 for s in statuses[statuses.index(402):]), (
        "once the door closes it stays closed"
    )

    refreshed = await priced_app.state.store.key_by_id(key.id)
    # Sequential calls against the echo provider, where the local estimate is
    # exact by construction, so the worst-case ceiling genuinely holds. Against
    # a real tokenizer the estimate can undershoot; see the overshoot note in
    # the README for the honest bound.
    assert refreshed.spent_usd <= refreshed.budget_usd, "spend must never pass the ceiling"


# ── the pricing rules themselves ─────────────────────────────────────────────


def test_an_unpriced_model_costs_zero_and_says_so():
    book = PriceBook.load()
    cost, known = book.cost_usd("some-model-nobody-priced", 1000, 1000)
    assert cost == 0.0
    assert known is False


def test_an_unpriced_model_cannot_be_admitted_on_price():
    """None means "cannot judge this", which is not the same as "free"."""
    book = PriceBook.load()
    assert book.max_cost_usd("some-model-nobody-priced", 100, 100) is None


def test_every_shipped_rate_carries_a_source_and_a_date():
    book = PriceBook.load()
    for model, price in book._prices.items():  # noqa: SLF001
        assert price.source, f"{model} has no source"
        assert price.checked_on, f"{model} has no checked_on date"


def test_cached_input_is_not_billed_twice():
    book = PriceBook.load()
    cost, _ = book.cost_usd("claude-opus-5", input_tokens=1000, output_tokens=0,
                            cached_input_tokens=1000)
    plain, _ = book.cost_usd("claude-opus-5", input_tokens=1000, output_tokens=0)
    assert cost == pytest.approx(plain)


# ── reservations: the concurrency fix ────────────────────────────────────────


async def test_a_budget_holds_when_the_whole_burst_arrives_at_once(priced_app, priced_client):
    """The bug this exists to prevent.

    Admission used to read the spend, then decide. Sixty requests arriving
    together each read the same spend, each concluded there was room, and a
    $0.20 key spent $1.50. The stress harness measured a 650% overshoot.
    Reserving atomically before the call is what closes it.
    """
    ceiling = 0.20
    key, secret = await priced_app.state.store.create_key(name="racer", budget_usd=ceiling)
    headers = {"Authorization": f"Bearer {secret}"}

    results = await asyncio.gather(*(
        priced_client.post("/v1/chat/completions", json=chat_body(max_tokens=16), headers=headers)
        for _ in range(40)
    ))
    codes = [r.status_code for r in results]

    refreshed = await priced_app.state.store.key_by_id(key.id)
    assert refreshed.spent_usd <= ceiling, (
        f"spent ${refreshed.spent_usd:.4f} against a ${ceiling:.2f} ceiling"
    )
    assert 200 in codes and 402 in codes, "some should be admitted and some refused"


async def test_a_reservation_is_released_however_the_request_ends(priced_app, priced_client):
    key, secret = await priced_app.state.store.create_key(name="settler", budget_usd=5.0)
    headers = {"Authorization": f"Bearer {secret}"}

    # answered, failed at the provider, and cut off mid-stream
    await priced_client.post("/v1/chat/completions", json=chat_body(max_tokens=8), headers=headers)
    await priced_client.post(
        "/v1/chat/completions", json=chat_body(max_tokens=8),
        headers={**headers, "X-Stormdoor-Chaos": "fault=error;status=503"},
    )
    async with priced_client.stream(
        "POST", "/v1/chat/completions", json=chat_body(stream=True, max_tokens=32),
        headers={**headers, "X-Stormdoor-Chaos": "fault=mid_stream_abort;after_chunks=3"},
    ) as r:
        async for _line in r.aiter_lines():
            pass

    refreshed = await priced_app.state.store.key_by_id(key.id)
    assert abs(refreshed.reserved_usd) < 1e-9, (
        f"${refreshed.reserved_usd} left reserved after three requests finished"
    )


async def test_a_failed_request_gives_its_reservation_back(priced_app, priced_client):
    """A provider outage must not eat the budget it briefly claimed."""
    key, secret = await priced_app.state.store.create_key(name="unlucky", budget_usd=0.10)
    headers = {"Authorization": f"Bearer {secret}",
               "X-Stormdoor-Chaos": "fault=error;status=503"}

    for _ in range(20):
        r = await priced_client.post(
            "/v1/chat/completions", json=chat_body(max_tokens=16), headers=headers
        )
        assert r.status_code == 503

    refreshed = await priced_app.state.store.key_by_id(key.id)
    assert refreshed.spent_usd == 0.0
    assert abs(refreshed.reserved_usd) < 1e-9

    # Twenty consecutive failures opened the target's circuit, which is the
    # right response to an outage. Cleared here because what this test is about
    # is the budget, not the breaker.
    priced_app.state.breaker.reset()

    # And the key is still usable afterwards, not silently exhausted.
    ok = await priced_client.post(
        "/v1/chat/completions", json=chat_body(max_tokens=8),
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert ok.status_code == 200


async def test_reservations_are_cleared_when_the_gateway_restarts(tmp_path):
    """A process that dies mid-request must not shrink the budget forever."""
    from stormdoor.store import Store

    store = Store(tmp_path / "restart.db")
    key, _secret = await store.create_key(name="crashed", budget_usd=1.0)
    granted, _spent, _committed = await store.reserve(key.id, 0.40)
    assert granted
    assert (await store.key_by_id(key.id)).reserved_usd == 0.40
    store.close()

    # A fresh process against the same file: nothing can be in flight.
    reopened = Store(tmp_path / "restart.db")
    assert (await reopened.key_by_id(key.id)).reserved_usd == 0.0
    reopened.close()
