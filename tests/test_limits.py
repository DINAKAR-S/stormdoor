"""The token-bucket arithmetic, tested without a clock or a store.

`take_from_bucket` is pure, so these assert exact numbers rather than ranges.
Both limiter backends call it, which is what keeps the in-memory and the Redis
implementations from drifting apart in behaviour.
"""

from __future__ import annotations

import pytest

from stormdoor.limits import BucketState, MemoryLimiter, take_from_bucket


def test_first_call_starts_full():
    decision, state = take_from_bucket(
        None, cost=1, capacity=60, refill_per_s=1.0, now=100.0
    )
    assert decision.allowed
    assert state.tokens == 59


def test_burst_up_to_capacity_then_refuses():
    state = BucketState(tokens=60, updated_at=0.0)
    for _ in range(60):
        decision, state = take_from_bucket(
            state, cost=1, capacity=60, refill_per_s=1.0, now=0.0
        )
        assert decision.allowed

    decision, state = take_from_bucket(state, cost=1, capacity=60, refill_per_s=1.0, now=0.0)
    assert not decision.allowed
    assert decision.retry_after_s == pytest.approx(1.0)


def test_refills_over_time_and_never_past_capacity():
    state = BucketState(tokens=0.0, updated_at=0.0)
    decision, state = take_from_bucket(state, cost=5, capacity=60, refill_per_s=1.0, now=10.0)
    assert decision.allowed
    assert state.tokens == pytest.approx(5.0)

    # Idle for an hour: the bucket caps at capacity, it does not accumulate.
    decision, state = take_from_bucket(state, cost=1, capacity=60, refill_per_s=1.0, now=3610.0)
    assert state.tokens == pytest.approx(59.0)


def test_cost_larger_than_capacity_is_refused_not_queued():
    """A request that can never fit should fail now, not wait forever."""
    decision, _ = take_from_bucket(None, cost=500, capacity=60, refill_per_s=1.0, now=0.0)
    assert not decision.allowed
    assert decision.retry_after_s == pytest.approx(60.0)


def test_retry_after_covers_the_actual_deficit():
    state = BucketState(tokens=2.0, updated_at=0.0)
    decision, _ = take_from_bucket(state, cost=10, capacity=60, refill_per_s=1.0, now=0.0)
    assert not decision.allowed
    assert decision.retry_after_s == pytest.approx(8.0)


async def test_memory_limiter_isolates_buckets():
    limiter = MemoryLimiter()
    for _ in range(2):
        assert (await limiter.take("key-a:rpm", 1, per_minute=2)).allowed
    assert not (await limiter.take("key-a:rpm", 1, per_minute=2)).allowed

    # A different key is untouched by the first key's spending.
    assert (await limiter.take("key-b:rpm", 1, per_minute=2)).allowed
