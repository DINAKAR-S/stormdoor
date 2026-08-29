"""The circuit breaker state machine, with the clock passed in.

Time is an argument here rather than something read from the environment, so
every transition is asserted exactly instead of being slept through.
"""

from __future__ import annotations

from stormdoor.breaker import BreakerConfig, CircuitBreaker

TARGET = "openai/gpt-4o-mini"


def breaker(**over) -> CircuitBreaker:
    return CircuitBreaker(BreakerConfig(**{"failure_threshold": 3, "cooldown_s": 30.0, **over}))


def test_a_new_target_is_allowed():
    assert breaker().allow(TARGET, now=0.0) is True


def test_it_takes_the_threshold_to_open():
    b = breaker(failure_threshold=3)
    for i in range(2):
        b.record_failure(TARGET, retryable=True, now=float(i))
        assert b.allow(TARGET, now=float(i)) is True, "two failures is not an outage yet"

    b.record_failure(TARGET, retryable=True, now=2.0)
    assert b.allow(TARGET, now=2.0) is False
    assert b.health(TARGET).state == "open"


def test_a_success_resets_the_count():
    """Failures have to be consecutive. Occasional failures are not an outage."""
    b = breaker(failure_threshold=3)
    b.record_failure(TARGET, retryable=True, now=0.0)
    b.record_failure(TARGET, retryable=True, now=1.0)
    b.record_success(TARGET, now=2.0)
    b.record_failure(TARGET, retryable=True, now=3.0)
    b.record_failure(TARGET, retryable=True, now=4.0)

    assert b.allow(TARGET, now=4.0) is True
    assert b.health(TARGET).consecutive_failures == 2


def test_a_non_retryable_failure_never_opens_the_circuit():
    """The most important rule in the file.

    A 400 is one caller sending a malformed request. If it counted, a single bad
    prompt repeated a few times would take a working model away from everybody
    else on the gateway.
    """
    b = breaker(failure_threshold=3)
    for i in range(20):
        b.record_failure(TARGET, retryable=False, error="invalid_request", now=float(i))

    assert b.allow(TARGET, now=20.0) is True
    assert b.health(TARGET).state == "closed"
    assert b.health(TARGET).ignored_failures == 20
    assert b.health(TARGET).consecutive_failures == 0


def test_it_stays_open_for_the_cooldown_then_probes_once():
    b = breaker(failure_threshold=1, cooldown_s=30.0)
    b.record_failure(TARGET, retryable=True, now=0.0)

    assert b.allow(TARGET, now=29.9) is False, "still cooling down"
    assert b.allow(TARGET, now=30.0) is True, "the probe is allowed through"
    assert b.health(TARGET).state == "half_open"

    # Exactly one probe. A burst arriving the moment the cooldown expires must
    # not all be sent at a provider that may still be down.
    assert b.allow(TARGET, now=30.1) is False
    assert b.allow(TARGET, now=99.0) is False


def test_a_successful_probe_closes_the_circuit():
    b = breaker(failure_threshold=1, cooldown_s=10.0)
    b.record_failure(TARGET, retryable=True, now=0.0)
    assert b.allow(TARGET, now=10.0) is True

    b.record_success(TARGET, now=10.5)
    assert b.health(TARGET).state == "closed"
    assert b.allow(TARGET, now=10.6) is True


def test_a_failed_probe_reopens_immediately_and_restarts_the_cooldown():
    """One failure is enough when half-open, whatever the threshold.

    Waiting for the threshold again would send a burst at a provider that has
    just said it is still broken.
    """
    b = breaker(failure_threshold=5, cooldown_s=10.0)
    for i in range(5):
        b.record_failure(TARGET, retryable=True, now=float(i))
    # The fifth failure is what opened it, at t=4, so the cooldown ends at 14.
    assert b.allow(TARGET, now=13.9) is False
    assert b.allow(TARGET, now=14.0) is True, "half open"

    b.record_failure(TARGET, retryable=True, now=14.5)
    assert b.health(TARGET).state == "open"
    assert b.allow(TARGET, now=20.0) is False, "the cooldown restarted from the failed probe"
    assert b.allow(TARGET, now=24.5) is True


def test_targets_are_independent():
    """One model being overloaded must not take the rest of the account down."""
    b = breaker(failure_threshold=1)
    b.record_failure("openai/gpt-4o", retryable=True, now=0.0)

    assert b.allow("openai/gpt-4o", now=0.0) is False
    assert b.allow("openai/gpt-4o-mini", now=0.0) is True
    assert b.allow("anthropic/claude-opus-5", now=0.0) is True


def test_the_snapshot_explains_why_a_circuit_did_not_open():
    b = breaker(failure_threshold=3)
    b.record_failure(TARGET, retryable=False, error="invalid_request", now=0.0)
    b.record_failure(TARGET, retryable=True, error="overloaded", now=1.0)
    b.record_success(TARGET, now=2.0)

    row = next(r for r in b.snapshot() if r["target"] == TARGET)
    assert row["state"] == "closed"
    assert row["failures"] == 2
    assert row["ignored_failures"] == 1, "the caller's own 400 is counted separately"
    assert row["successes"] == 1
    assert row["last_error"] == "overloaded"


def test_reset_clears_one_target_or_all_of_them():
    b = breaker(failure_threshold=1)
    b.record_failure("a/one", retryable=True, now=0.0)
    b.record_failure("b/two", retryable=True, now=0.0)

    b.reset("a/one")
    assert b.allow("a/one", now=0.0) is True
    assert b.allow("b/two", now=0.0) is False

    b.reset()
    assert b.snapshot() == []
    # Asking about a target creates its row again, so the emptiness is checked
    # before the question, not after it.
    assert b.allow("b/two", now=0.0) is True


def test_opened_count_survives_a_close():
    """How often a target has flapped is worth keeping after it recovers."""
    b = breaker(failure_threshold=1, cooldown_s=1.0)
    for cycle in range(3):
        base = cycle * 10.0
        b.record_failure(TARGET, retryable=True, now=base)
        b.allow(TARGET, now=base + 1.0)
        b.record_success(TARGET, now=base + 1.1)

    assert b.health(TARGET).opened_count == 3
    assert b.health(TARGET).state == "closed"
