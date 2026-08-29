"""Circuit breakers, one per target.

A target is a provider and a model together, because "OpenAI is down" is rarely
what happens. One model being overloaded while the rest of the account is fine is
much more common, and a breaker that trips the whole provider would take healthy
models down with it.

Three states, and the middle one is the point:

``closed``     traffic flows, failures are counted
``open``       traffic is refused without an upstream call, until a cooldown passes
``half_open``  exactly one request is let through to find out if it recovered

The value is in ``open``. Retrying a provider that is down costs a timeout per
request and adds load to something already struggling. Skipping it costs nothing
and lets the next target answer immediately.

**Only retryable failures count.** A 400 is one caller sending a malformed
request. Counting it toward a breaker would let one bad prompt take a working
model away from everybody else. This is the single most important line in the
file, and it is the kind of thing that looks fine in review and is discovered in
production.

Health is tracked from real traffic rather than from a background pinger. An idle
provider is not a sick one, and a pinger is one more thing that can lie.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

State = Literal["closed", "open", "half_open"]


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    # Consecutive retryable failures before the circuit opens. Three is enough
    # to distinguish a real outage from one unlucky request without spending
    # long on it.
    failure_threshold: int = 3
    # How long to stay open before letting a single probe through.
    cooldown_s: float = 30.0


@dataclass(slots=True)
class TargetHealth:
    """What is known about one target, from real traffic only."""

    target: str
    state: State = "closed"
    consecutive_failures: int = 0
    opened_at: float | None = None
    last_error: str | None = None
    last_success_at: float | None = None
    successes: int = 0
    failures: int = 0
    # Failures that were the caller's fault and deliberately not counted toward
    # the breaker. Surfaced so "why is this not opening" has an answer.
    ignored_failures: int = 0
    opened_count: int = 0

    def public(self) -> dict:
        return {
            "target": self.target,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "successes": self.successes,
            "failures": self.failures,
            "ignored_failures": self.ignored_failures,
            "times_opened": self.opened_count,
            "last_error": self.last_error,
        }


@dataclass
class CircuitBreaker:
    """In-process breakers, keyed by target.

    Deliberately not shared across replicas. A breaker is an optimisation, and a
    wrong shared one is worse than a right local one: each replica learns from
    its own traffic within a few requests, and a network round trip on the hot
    path to ask another process what it thinks would cost more than the timeouts
    it saves.
    """

    config: BreakerConfig = field(default_factory=BreakerConfig)
    _health: dict[str, TargetHealth] = field(default_factory=dict)

    def health(self, target: str) -> TargetHealth:
        if target not in self._health:
            self._health[target] = TargetHealth(target=target)
        return self._health[target]

    def allow(self, target: str, *, now: float | None = None) -> bool:
        """Should this target be tried right now?

        Also performs the open to half-open transition, because the cooldown
        expiring is only observable when somebody asks.
        """
        now = time.monotonic() if now is None else now
        health = self.health(target)

        if health.state == "closed":
            return True

        if health.state == "half_open":
            # A probe is already out. Everything else keeps being refused, so a
            # burst arriving the moment the cooldown expires does not all get
            # sent at a provider that may still be down.
            return False

        # `or now` would be wrong here: 0.0 is a legitimate timestamp and it is
        # falsy, so a circuit opened at zero would reset its own cooldown on
        # every check and never probe. time.monotonic() never returns 0 in a
        # real process, which is exactly why this kind of bug survives to
        # production and only shows up when a test passes the clock in.
        opened_at = health.opened_at if health.opened_at is not None else now
        if now - opened_at >= self.config.cooldown_s:
            health.state = "half_open"
            return True
        return False

    def record_success(self, target: str, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        health = self.health(target)
        health.state = "closed"
        health.consecutive_failures = 0
        health.opened_at = None
        health.last_success_at = now
        health.successes += 1

    def record_failure(
        self, target: str, *, retryable: bool, error: str | None = None,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        health = self.health(target)
        health.failures += 1
        health.last_error = error

        if not retryable:
            # The caller's problem, not the provider's. Counting it would let a
            # single malformed request close a working model to everyone.
            health.ignored_failures += 1
            return

        health.consecutive_failures += 1

        # A failed probe sends it straight back to open, and restarts the
        # cooldown. Waiting for the threshold again would hammer a provider that
        # has just told us it is still broken.
        tripped = health.consecutive_failures >= self.config.failure_threshold
        if health.state == "half_open" or tripped:
            if health.state != "open":
                health.opened_count += 1
            health.state = "open"
            health.opened_at = now

    def snapshot(self) -> list[dict]:
        return [h.public() for h in sorted(self._health.values(), key=lambda h: h.target)]

    def reset(self, target: str | None = None) -> None:
        """Forget everything, or one target. For operators and for tests."""
        if target is None:
            self._health.clear()
        else:
            self._health.pop(target, None)
