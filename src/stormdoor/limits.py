"""Token-bucket rate limiting, per key, for requests and for tokens.

A bucket holds ``capacity`` units and refills at ``capacity / 60`` per second,
so ``rpm=60`` means sixty requests a minute with a burst of sixty rather than
one request per second exactly. Bursts are the point: real traffic arrives in
clumps, and a limiter that rejects a clump it could have absorbed is just an
outage you built yourself.

Two backends behind one interface:

``MemoryLimiter``  in-process, no dependencies, correct for a single replica.
                   The default, because it makes the gateway runnable with
                   nothing installed.
``RedisLimiter``   shared across replicas. The refill and the take happen inside
                   one Lua script, so two replicas cannot both spend the last
                   token in the bucket.

The refill arithmetic itself lives in ``take_from_bucket`` and is used by both,
so the two backends cannot drift apart in behaviour.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BucketState:
    tokens: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    retry_after_s: float = 0.0
    remaining: float = 0.0


def take_from_bucket(
    state: BucketState | None,
    *,
    cost: float,
    capacity: float,
    refill_per_s: float,
    now: float,
) -> tuple[Decision, BucketState]:
    """Pure token-bucket step. No clock, no storage, no I/O, so it is testable.

    A cost larger than the whole bucket can never succeed, so it is rejected
    immediately with the time it would take to refill from empty rather than
    looping forever.
    """
    if state is None:
        state = BucketState(tokens=capacity, updated_at=now)

    elapsed = max(0.0, now - state.updated_at)
    tokens = min(capacity, state.tokens + elapsed * refill_per_s)

    if cost > capacity:
        return (
            Decision(allowed=False, retry_after_s=capacity / refill_per_s, remaining=tokens),
            BucketState(tokens=tokens, updated_at=now),
        )

    if tokens >= cost:
        remaining = tokens - cost
        return (
            Decision(allowed=True, remaining=remaining),
            BucketState(tokens=remaining, updated_at=now),
        )

    deficit = cost - tokens
    return (
        Decision(allowed=False, retry_after_s=deficit / refill_per_s, remaining=tokens),
        BucketState(tokens=tokens, updated_at=now),
    )


class Limiter(Protocol):
    async def take(self, bucket: str, cost: float, *, per_minute: int) -> Decision: ...
    async def close(self) -> None: ...


class MemoryLimiter:
    """Single-process limiter. One lock, held only across the arithmetic."""

    def __init__(self) -> None:
        self._buckets: dict[str, BucketState] = {}
        self._lock = asyncio.Lock()

    async def take(self, bucket: str, cost: float, *, per_minute: int) -> Decision:
        capacity = float(per_minute)
        refill = capacity / 60.0
        now = time.monotonic()
        async with self._lock:
            decision, state = take_from_bucket(
                self._buckets.get(bucket),
                cost=cost,
                capacity=capacity,
                refill_per_s=refill,
                now=now,
            )
            self._buckets[bucket] = state
        return decision

    async def close(self) -> None:
        self._buckets.clear()


# Refill and take in one round trip, so concurrent replicas cannot double-spend.
# KEYS[1] bucket   ARGV: cost, capacity, refill_per_s, now
_LUA = """
local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated_at')
local cost, capacity = tonumber(ARGV[1]), tonumber(ARGV[2])
local refill, now = tonumber(ARGV[3]), tonumber(ARGV[4])

local tokens = tonumber(state[1])
local updated = tonumber(state[2])
if tokens == nil then tokens = capacity; updated = now end

local elapsed = now - updated
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)

if cost > capacity then
  redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated_at', now)
  redis.call('EXPIRE', KEYS[1], 3600)
  return {0, tostring(capacity / refill), tostring(tokens)}
end

if tokens >= cost then
  tokens = tokens - cost
  redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated_at', now)
  redis.call('EXPIRE', KEYS[1], 3600)
  return {1, '0', tostring(tokens)}
end

redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated_at', now)
redis.call('EXPIRE', KEYS[1], 3600)
return {0, tostring((cost - tokens) / refill), tostring(tokens)}
"""


class RedisLimiter:
    """Shared limiter. Needs the ``redis`` extra."""

    def __init__(self, url: str):
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "limiter_backend='redis' needs the redis extra: pip install 'stormdoor[redis]'"
            ) from exc
        self._redis = Redis.from_url(url, decode_responses=True)
        self._script = self._redis.register_script(_LUA)

    async def take(self, bucket: str, cost: float, *, per_minute: int) -> Decision:
        capacity = float(per_minute)
        refill = capacity / 60.0
        now = time.time()
        allowed, retry_after, remaining = await self._script(
            keys=[f"stormdoor:bucket:{bucket}"],
            args=[cost, capacity, refill, now],
        )
        return Decision(
            allowed=bool(int(allowed)),
            retry_after_s=float(retry_after),
            remaining=float(remaining),
        )

    async def close(self) -> None:  # pragma: no cover - needs a live redis
        await self._redis.aclose()


def build_limiter(backend: str, redis_url: str | None) -> Limiter:
    if backend == "redis":
        if not redis_url:
            raise RuntimeError("limiter_backend='redis' requires STORMDOOR_REDIS_URL")
        return RedisLimiter(redis_url)
    return MemoryLimiter()
