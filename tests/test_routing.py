"""Routes and complexity scoring, without a gateway in the way."""

from __future__ import annotations

import json
import random

import pytest

from stormdoor.attempts import AttemptLog, backoff_delay
from stormdoor.errors import BadRequest
from stormdoor.routing import Complexity, RouteTable, Target, score
from stormdoor.types import ChatCompletionRequest


def req(content: str = "hello", **over) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=over.pop("model", "auto"),
        messages=over.pop("messages", [{"role": "user", "content": content}]),
        **over,
    )


def write_routes(tmp_path, data) -> object:
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── route loading ────────────────────────────────────────────────────────────


def test_no_route_file_means_a_model_is_itself():
    table = RouteTable.load(None)
    assert table.candidates("gpt-4o-mini") == [Target(model="gpt-4o-mini")]
    assert table.names() == []


def test_an_unrouted_model_is_still_itself(tmp_path):
    table = RouteTable.load(write_routes(tmp_path, {"auto": {"targets": ["echo-small"]}}))
    assert table.candidates("gpt-4o-mini") == [Target(model="gpt-4o-mini")]


def test_a_bare_string_target_is_allowed(tmp_path):
    """Most routes are a list of model names. Requiring objects for that is ceremony."""
    table = RouteTable.load(
        write_routes(tmp_path, {"auto": {"targets": ["echo-small", "echo-large"]}})
    )
    assert table.candidates("auto") == [Target("echo-small"), Target("echo-large")]


def test_targets_keep_the_order_they_were_written(tmp_path):
    table = RouteTable.load(
        write_routes(tmp_path, {"auto": {"targets": ["a", "b", "c"]}})
    )
    assert [t.model for t in table.candidates("auto")] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "bad, expected",
    [
        ({"auto": {"targets": []}}, "lists no targets"),
        ({"auto": {"strategy": "vibes", "targets": ["a"]}}, "unknown strategy"),
        ({"auto": {"targets": [{"tier": "cheap"}]}}, "no model"),
        ({"auto": {"targets": [{"model": "a", "tier": "enormous"}]}}, "unknown tier"),
        ({"auto": ["a", "b"]}, "must be an object"),
    ],
)
def test_a_broken_route_file_fails_loudly_at_load(tmp_path, bad, expected):
    """A typo in a route must surface at startup, not during an outage six weeks later."""
    with pytest.raises(ValueError, match=expected):
        RouteTable.load(write_routes(tmp_path, bad))


# ── complexity ───────────────────────────────────────────────────────────────


def test_a_short_question_is_cheap():
    assert score(req("what time do you close")).tier == "cheap"


def test_code_in_the_prompt_is_deep():
    result = score(req("fix this:\n```python\nprint(1)\n```"))
    assert result.tier == "deep"
    assert "code" in result.reason


def test_a_long_prompt_is_deep():
    result = score(req("word " * 3000))
    assert result.tier == "deep"
    assert "long prompt" in result.reason


def test_a_deep_conversation_is_deep():
    messages = [{"role": "user", "content": "hi"} for _ in range(9)]
    result = score(req(messages=messages))
    assert result.tier == "deep"
    assert "9 messages deep" in result.reason


def test_asking_for_a_lot_of_output_is_deep():
    assert score(req("summarise", max_tokens=4000)).tier == "deep"


def test_the_caller_can_override_the_score():
    result = score(req("hi"), hint="deep")
    assert result.tier == "deep"
    assert result.reason == "asked for by the caller"


def test_an_unknown_tier_hint_is_refused():
    with pytest.raises(BadRequest):
        score(req("hi"), hint="enormous")


def test_every_scoring_decision_explains_itself():
    """A routing choice nobody can explain is a routing choice nobody trusts."""
    for request in (req("hi"), req("word " * 3000), req("```code```")):
        assert score(request).reason


# ── complexity plus routes ───────────────────────────────────────────────────


def test_the_preferred_tier_goes_first(tmp_path):
    table = RouteTable.load(write_routes(tmp_path, {
        "auto": {"strategy": "complexity", "targets": [
            {"model": "big", "tier": "deep"},
            {"model": "small", "tier": "cheap"},
        ]},
    }))
    order = [t.model for t in table.candidates("auto", Complexity("cheap", "short"))]
    assert order[0] == "small"


def test_nothing_is_dropped_by_a_tier_preference(tmp_path):
    """A tier decides where to start, never what is available.

    A cheap request must still be able to escalate to a deep target when the
    cheap ones are down, or the fallback chain would be shorter exactly when it
    is needed most.
    """
    table = RouteTable.load(write_routes(tmp_path, {
        "auto": {"strategy": "complexity", "targets": [
            {"model": "big", "tier": "deep"},
            {"model": "small", "tier": "cheap"},
        ]},
    }))
    order = [t.model for t in table.candidates("auto", Complexity("cheap", "short"))]
    assert sorted(order) == ["big", "small"]


def test_a_chain_route_ignores_complexity(tmp_path):
    table = RouteTable.load(write_routes(tmp_path, {
        "auto": {"targets": [
            {"model": "big", "tier": "deep"},
            {"model": "small", "tier": "cheap"},
        ]},
    }))
    order = [t.model for t in table.candidates("auto", Complexity("cheap", "short"))]
    assert order == ["big", "small"], "a plain chain is tried in the order written"


def test_a_target_with_no_tier_suits_anything():
    assert Target("m").suits("cheap") and Target("m").suits("deep")


# ── backoff ──────────────────────────────────────────────────────────────────


def test_backoff_grows_but_is_capped():
    rng = random.Random(1)
    ceilings = []
    for attempt in range(8):
        # With a fixed rng the draw is uniform below the ceiling, so sampling
        # the maximum over many draws approximates the ceiling itself.
        ceilings.append(max(
            backoff_delay(attempt, base=0.2, cap=5.0, rng=rng) for _ in range(400)
        ))
    assert ceilings[0] < ceilings[2] < ceilings[4], "it should grow"
    assert all(c <= 5.0 for c in ceilings), "and never pass the cap"


def test_backoff_is_jittered_not_fixed():
    """Exponential backoff alone synchronises clients.

    Everyone who failed at the same moment would retry at the same moment, and a
    provider that was merely struggling gets a second identical spike.
    """
    rng = random.Random(7)
    draws = {round(backoff_delay(3, base=0.2, cap=5.0, rng=rng), 6) for _ in range(50)}
    assert len(draws) > 40, "the delay must vary between callers"


def test_backoff_rejects_a_negative_attempt():
    with pytest.raises(ValueError):
        backoff_delay(-1, base=0.2, cap=5.0)


# ── the attempt trail ────────────────────────────────────────────────────────


def test_a_skip_is_not_an_attempt():
    """Skipping an open circuit costs nothing, so it must not look like a try."""
    log = AttemptLog()
    log.skipped("a/one", "circuit open")
    log.succeeded("b/two")
    assert log.tried == 1
    assert log.served_by == "b/two"


def test_failed_over_from_is_only_set_when_something_else_answered():
    lost = AttemptLog()
    lost.failed("a/one", "overloaded")
    assert lost.failed_over_from is None, "nothing to fall back from if nothing answered"

    saved = AttemptLog()
    saved.failed("a/one", "overloaded")
    saved.succeeded("b/two")
    assert saved.failed_over_from == "a/one"
    assert saved.tried == 2
