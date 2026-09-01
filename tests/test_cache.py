"""The semantic cache, end to end through the gateway.

Every test drives the real HTTP path against the local echo provider, so a hit
is a hit through admission, the cache and the ledger, not a unit calling a
method. The echo output is a deterministic function of the prompt, so an
identical prompt is genuinely the same question and a changed one genuinely is
not.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from conftest import chat_body
from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings


def build(tmp_path, **over):
    pricing = None
    if over.pop("priced", False):
        pricing = tmp_path / "pricing.json"
        pricing.write_text(json.dumps({"echo-small": {
            "input_per_mtok": 1000.0, "output_per_mtok": 1000.0,
            "source": "fictional, for tests", "checked_on": "2026-09-01",
        }}), encoding="utf-8")
    return create_app(Settings(
        db_path=tmp_path / f"{over.pop('db', 'cache')}.db",
        admin_token="admin",
        cache_enabled=over.pop("cache_enabled", True),
        cache_similarity_floor=over.pop("floor", 0.95),
        cache_ttl_s=over.pop("ttl", 3600.0),
        pricing_file=pricing,
        chaos_enabled=True,
        _env_file=None,
        **over,
    ))


@pytest.fixture
def cache_app(tmp_path):
    return build(tmp_path)


@pytest_asyncio.fixture
async def c(cache_app):
    async with AsyncClient(
        transport=ASGITransport(app=cache_app), base_url="http://x"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def auth(cache_app):
    _key, secret = await cache_app.state.store.create_key(name="cache")
    return {"Authorization": f"Bearer {secret}"}


def um(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


async def post(c, auth, **over):
    return await c.post("/v1/chat/completions", json=chat_body(**over), headers=auth)


async def ask(c, auth, text: str, **over):
    return await post(c, auth, messages=um(text), **over)


# ── the headline ──────────────────────────────────────────────────────────────


async def test_an_identical_repeat_is_served_from_cache(c, auth):
    first = await post(c, auth, messages=um("what is a gateway"))
    assert first.json()["stormdoor"]["cache"] == {"hit": False}

    second = await post(c, auth, messages=um("what is a gateway"))
    body = second.json()
    assert body["stormdoor"]["cache"]["hit"] is True
    assert body["stormdoor"]["cache"]["similarity"] >= 0.95
    assert body["stormdoor"]["cost_usd"] == 0.0
    # Same answer both times: a cache that returned something different would be
    # worse than no cache.
    answer = body["choices"][0]["message"]["content"]
    assert answer == first.json()["choices"][0]["message"]["content"]


async def test_a_near_identical_prompt_still_hits(c, auth):
    await post(c, auth, messages=um("what is the capital of France"))
    r = await post(c, auth, messages=um("what is the capital of France?"))
    # One trailing '?' shares every word; the lexical embedder puts them well
    # above the floor.
    assert r.json()["stormdoor"]["cache"]["hit"] is True


async def test_a_different_question_is_a_miss(c, auth):
    await post(c, auth, messages=um("what is a gateway"))
    r = await post(c, auth, messages=um("explain circuit breakers in depth"))
    assert r.json()["stormdoor"]["cache"]["hit"] is False


async def test_streaming_is_never_cached(c, auth):
    body = chat_body(messages=[{"role": "user", "content": "stream me"}], stream=True)
    r1 = await c.post("/v1/chat/completions", json=body, headers=auth)
    assert r1.status_code == 200
    # A non-streaming repeat of the same prompt must still miss, because the
    # stream never populated the cache.
    r2 = await post(c, auth, messages=[{"role": "user", "content": "stream me"}])
    assert r2.json()["stormdoor"]["cache"]["hit"] is False


async def test_a_sampled_request_is_never_cached(c, auth):
    await post(c, auth, messages=[{"role": "user", "content": "be creative"}], temperature=0.9)
    r = await post(c, auth, messages=[{"role": "user", "content": "be creative"}], temperature=0.9)
    # temperature > 0 asked for variety; the cache must not freeze one sample.
    assert "cache" not in r.json()["stormdoor"]


async def test_one_tenant_is_never_served_anothers_cached_answer(cache_app, c):
    _a, sa = await cache_app.state.store.create_key(name="a")
    _b, sb = await cache_app.state.store.create_key(name="b")
    prompt = [{"role": "user", "content": "shared question"}]
    await c.post("/v1/chat/completions", json=chat_body(messages=prompt),
                 headers={"Authorization": f"Bearer {sa}"})
    r = await c.post("/v1/chat/completions", json=chat_body(messages=prompt),
                     headers={"Authorization": f"Bearer {sb}"})
    assert r.json()["stormdoor"]["cache"]["hit"] is False, "cache leaked across keys"


async def test_an_expired_entry_is_a_miss(tmp_path):
    app = build(tmp_path, ttl=0.0, db="ttl")  # every entry is already expired
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        _k, s = await app.state.store.create_key(name="k")
        auth = {"Authorization": f"Bearer {s}"}
        await c.post("/v1/chat/completions",
                     json=chat_body(messages=um("soon gone")), headers=auth)
        r = await c.post("/v1/chat/completions",
                         json=chat_body(messages=um("soon gone")), headers=auth)
        assert r.json()["stormdoor"]["cache"]["hit"] is False
    app.state.store.close()


async def test_below_the_floor_is_a_miss(tmp_path):
    app = build(tmp_path, floor=0.999, db="floor")  # almost nothing clears this
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        _k, s = await app.state.store.create_key(name="k")
        auth = {"Authorization": f"Bearer {s}"}
        await c.post("/v1/chat/completions",
                     json=chat_body(messages=um("alpha beta gamma delta")), headers=auth)
        r = await c.post("/v1/chat/completions",
                         json=chat_body(messages=um("alpha beta gamma epsilon zeta")), headers=auth)
        assert r.json()["stormdoor"]["cache"]["hit"] is False
    app.state.store.close()


async def test_the_hit_ratio_is_reported(cache_app, c, auth):
    await post(c, auth, messages=[{"role": "user", "content": "q one"}])   # miss
    await post(c, auth, messages=[{"role": "user", "content": "q one"}])   # hit
    await post(c, auth, messages=[{"role": "user", "content": "q two"}])   # miss
    stats = cache_app.state.cache.stats()
    assert stats["lookups"] == 3
    assert stats["hits"] == 1
    assert stats["hit_ratio"] == pytest.approx(1 / 3, abs=1e-3)


async def test_invalidation_drops_everything(cache_app, c, auth):
    await post(c, auth, messages=[{"role": "user", "content": "cache me"}])
    r = await c.delete("/admin/cache", headers={"X-Stormdoor-Admin": "admin"})
    assert r.json()["invalidated"] == 1
    miss = await post(c, auth, messages=[{"role": "user", "content": "cache me"}])
    assert miss.json()["stormdoor"]["cache"]["hit"] is False


async def test_a_cache_hit_costs_no_budget(tmp_path):
    """The whole economic point: a hit must not spend against the key's budget."""
    app = build(tmp_path, priced=True, db="budget")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        key, s = await app.state.store.create_key(name="k", budget_usd=1.0)
        auth = {"Authorization": f"Bearer {s}"}
        # A small max_tokens keeps the worst-case estimate well under the budget;
        # both requests share it so they land in the same cache scope.
        prompt = [{"role": "user", "content": "bill me once"}]
        body = chat_body(messages=prompt, max_tokens=50)
        await c.post("/v1/chat/completions", json=body, headers=auth)
        after_first = await app.state.store.key_by_id(key.id)
        spent_once = after_first.spent_usd
        assert spent_once > 0.0

        hit = await c.post("/v1/chat/completions", json=body, headers=auth)
        assert hit.json()["stormdoor"]["cache"]["hit"] is True
        after_hit = await app.state.store.key_by_id(key.id)
        assert after_hit.spent_usd == pytest.approx(spent_once, abs=1e-12), "a hit spent budget"
        assert after_hit.reserved_usd == pytest.approx(0.0, abs=1e-9)
    app.state.store.close()


async def test_disabled_by_default(tmp_path):
    app = build(tmp_path, cache_enabled=False, db="off")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        _k, s = await app.state.store.create_key(name="k")
        auth = {"Authorization": f"Bearer {s}"}
        r = await c.post("/v1/chat/completions",
                         json=chat_body(messages=um("no cache here")), headers=auth)
        assert "cache" not in r.json()["stormdoor"]
    app.state.store.close()
