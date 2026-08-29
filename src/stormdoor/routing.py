"""Routes: what a model name means, and what to try when it fails.

A route turns one model name into an ordered list of targets. Ask for
``support-chat`` and the gateway tries a cheap model, then a stronger one, then
somebody else's cheap model, in that order, skipping anything whose circuit is
open.

Two things a route can do:

**Fall back.** The obvious one. If the first target fails in a way worth
retrying, try the next.

**Start at the right size.** A request like "summarise this in one line" does not
need the largest model. Complexity scoring picks the cheapest tier that suits the
request and escalates only when the request earns it, which is where most of the
money is.

Routes are optional. With no route file, a model name means itself and there is
nothing to fall back to, which is exactly how the gateway behaved before routes
existed.

Route file format::

    {
      "support-chat": {
        "strategy": "complexity",
        "targets": [
          {"model": "claude-haiku-4-5", "tier": "cheap"},
          {"model": "claude-sonnet-5",  "tier": "deep"},
          {"model": "openai/gpt-4o-mini", "tier": "cheap"}
        ]
      },
      "gpt-4o-mini": {
        "targets": [{"model": "gpt-4o-mini"}, {"model": "claude-haiku-4-5"}]
      }
    }

The second form is the common one: keep calling the model you already call, and
give it somewhere to go when it breaks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import BadRequest
from .tokens import estimate_prompt_tokens
from .types import ChatCompletionRequest

Tier = Literal["cheap", "deep", "any"]
Strategy = Literal["chain", "complexity"]

TIERS: tuple[Tier, ...] = ("cheap", "deep", "any")
STRATEGIES: tuple[Strategy, ...] = ("chain", "complexity")

# Header a caller can use to override the score. Deliberately advisory: it can
# only be honoured if the route actually has a target in that tier.
TIER_HEADER = "x-stormdoor-tier"

# Signals that a request is doing real work rather than a one-liner. Each is
# cheap to compute and none of them needs the model's opinion, because asking a
# model which model to use costs a model call.
_CODE_FENCE = re.compile(r"```")
_LONG_PROMPT_TOKENS = 600
_MANY_TURNS = 8


@dataclass(frozen=True, slots=True)
class Target:
    model: str
    tier: Tier = "any"

    def suits(self, wanted: Tier) -> bool:
        return self.tier == "any" or wanted == "any" or self.tier == wanted


@dataclass(frozen=True, slots=True)
class Route:
    name: str
    targets: tuple[Target, ...]
    strategy: Strategy = "chain"


@dataclass(frozen=True, slots=True)
class Complexity:
    """Why a tier was chosen. Carried into the ledger so the choice is auditable."""

    tier: Tier
    reason: str

    def public(self) -> dict:
        return {"tier": self.tier, "reason": self.reason}


def score(req: ChatCompletionRequest, *, hint: str | None = None) -> Complexity:
    """Pick a tier from the request itself.

    No model call, no heuristic anyone has to trust blindly: every branch here is
    a fact about the request that a reader can check against their own traffic.
    """
    if hint:
        wanted = hint.strip().lower()
        if wanted not in TIERS:
            raise BadRequest(
                f"unknown tier {hint!r}, expected one of {list(TIERS)}", param="tier"
            )
        return Complexity(tier=wanted, reason="asked for by the caller")  # type: ignore[arg-type]

    prompt = req.prompt_text()

    if _CODE_FENCE.search(prompt):
        return Complexity(tier="deep", reason="the prompt contains code")

    tokens = estimate_prompt_tokens([m.text() for m in req.messages])
    if tokens >= _LONG_PROMPT_TOKENS:
        return Complexity(tier="deep", reason=f"long prompt, about {tokens} tokens")

    if len(req.messages) >= _MANY_TURNS:
        return Complexity(
            tier="deep", reason=f"a conversation {len(req.messages)} messages deep"
        )

    if req.max_tokens is not None and req.max_tokens >= 2000:
        return Complexity(tier="deep", reason=f"asked for up to {req.max_tokens} tokens back")

    return Complexity(tier="cheap", reason="short single-shot request")


class RouteTable:
    def __init__(self, routes: dict[str, Route]):
        self._routes = routes

    @classmethod
    def load(cls, path: Path | None) -> RouteTable:
        if path is None:
            return cls({})

        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        routes: dict[str, Route] = {}

        for name, spec in raw.items():
            if not isinstance(spec, dict):
                raise ValueError(f"route {name!r} must be an object")

            strategy = spec.get("strategy", "chain")
            if strategy not in STRATEGIES:
                raise ValueError(
                    f"route {name!r} has unknown strategy {strategy!r}, "
                    f"expected one of {list(STRATEGIES)}"
                )

            entries = spec.get("targets") or []
            if not entries:
                raise ValueError(f"route {name!r} lists no targets")

            targets = []
            for entry in entries:
                # A bare string is allowed, because most routes are just a list
                # of model names and making people write objects for that is
                # ceremony.
                if isinstance(entry, str):
                    targets.append(Target(model=entry))
                    continue
                model = entry.get("model")
                if not model:
                    raise ValueError(f"route {name!r} has a target with no model")
                tier = entry.get("tier", "any")
                if tier not in TIERS:
                    raise ValueError(
                        f"route {name!r} target {model!r} has unknown tier {tier!r}"
                    )
                targets.append(Target(model=model, tier=tier))

            routes[name] = Route(name=name, targets=tuple(targets), strategy=strategy)

        return cls(routes)

    def has(self, name: str) -> bool:
        return name in self._routes

    def get(self, name: str) -> Route | None:
        return self._routes.get(name)

    def names(self) -> list[str]:
        return sorted(self._routes)

    def candidates(self, model: str, complexity: Complexity | None = None) -> list[Target]:
        """The ordered list of targets to try for this request.

        A model with no route resolves to itself, with nothing to fall back to.
        That is the default and it is the previous behaviour exactly.
        """
        route = self._routes.get(model)
        if route is None:
            return [Target(model=model)]

        if route.strategy != "complexity" or complexity is None:
            return list(route.targets)

        # Preferred tier first, everything else after it in the order written.
        # Nothing is dropped: a tier preference decides where to start, never
        # what is available, so a cheap-tier request can still escalate to a
        # deep target when the cheap ones are down.
        preferred = [t for t in route.targets if t.suits(complexity.tier)]
        rest = [t for t in route.targets if not t.suits(complexity.tier)]
        return preferred + rest

    def describe(self) -> list[dict]:
        return [
            {
                "name": route.name,
                "strategy": route.strategy,
                "targets": [{"model": t.model, "tier": t.tier} for t in route.targets],
            }
            for route in sorted(self._routes.values(), key=lambda r: r.name)
        ]
