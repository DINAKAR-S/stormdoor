"""Model rate card, and the one rule this file exists to enforce.

**A rate that is not sourced is not shipped.** Every entry below carries the
source it came from and the date it was checked. A model with no verified rate
is recorded as ``pricing_known = false`` in the usage ledger and costs 0.00,
and the gateway logs a warning at startup naming it. It is not silently
guessed, because a guessed rate produces an invoice that is wrong in a way
nobody notices until the customer notices.

Provider prices change. Re-check them, then update ``checked_on``. Point
``STORMDOOR_PRICING_FILE`` at a JSON file to override the whole table without
touching the package:

    {
      "gpt-4o-mini": {
        "input_per_mtok": 0.15,
        "output_per_mtok": 0.60,
        "source": "https://openai.com/api/pricing/",
        "checked_on": "2026-08-29"
      }
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("stormdoor.pricing")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    source: str
    checked_on: str
    cached_input_per_mtok: float | None = None


# ── Anthropic ────────────────────────────────────────────────────────────────
# First-party Claude API rates. Bedrock and Vertex are partner-operated and
# priced separately, so do not reuse these for those platforms.
_ANTHROPIC_SOURCE = "Anthropic first-party API rate card"
_ANTHROPIC_CHECKED = "2026-06-24"

_BUILTIN: dict[str, ModelPrice] = {
    "claude-fable-5": ModelPrice(10.00, 50.00, _ANTHROPIC_SOURCE, _ANTHROPIC_CHECKED),
    "claude-opus-5": ModelPrice(5.00, 25.00, _ANTHROPIC_SOURCE, _ANTHROPIC_CHECKED),
    "claude-opus-4-8": ModelPrice(5.00, 25.00, _ANTHROPIC_SOURCE, _ANTHROPIC_CHECKED),
    "claude-opus-4-7": ModelPrice(5.00, 25.00, _ANTHROPIC_SOURCE, _ANTHROPIC_CHECKED),
    "claude-opus-4-6": ModelPrice(5.00, 25.00, _ANTHROPIC_SOURCE, _ANTHROPIC_CHECKED),
    "claude-sonnet-5": ModelPrice(2.00, 10.00, _ANTHROPIC_SOURCE, _ANTHROPIC_CHECKED),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00, _ANTHROPIC_SOURCE, _ANTHROPIC_CHECKED),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00, _ANTHROPIC_SOURCE, _ANTHROPIC_CHECKED),
    # ── stormdoor's own local provider ──────────────────────────────────────
    # Free by construction: it never leaves the process.
    "echo-small": ModelPrice(0.0, 0.0, "local provider, no upstream call", "2026-08-29"),
    "echo-large": ModelPrice(0.0, 0.0, "local provider, no upstream call", "2026-08-29"),
    # ── OpenAI ──────────────────────────────────────────────────────────────
    # Deliberately absent. Add them yourself from the live pricing page and set
    # checked_on to the day you looked. See STORMDOOR_PRICING_FILE above.
}


class PriceBook:
    def __init__(self, prices: dict[str, ModelPrice]):
        self._prices = dict(prices)

    @classmethod
    def load(cls, override_file: Path | None = None) -> PriceBook:
        prices = dict(_BUILTIN)
        if override_file is not None:
            raw = json.loads(Path(override_file).read_text(encoding="utf-8"))
            for model, entry in raw.items():
                prices[model] = ModelPrice(
                    input_per_mtok=float(entry["input_per_mtok"]),
                    output_per_mtok=float(entry["output_per_mtok"]),
                    source=str(entry.get("source", str(override_file))),
                    checked_on=str(entry.get("checked_on", "unknown")),
                    cached_input_per_mtok=(
                        float(entry["cached_input_per_mtok"])
                        if entry.get("cached_input_per_mtok") is not None
                        else None
                    ),
                )
        return cls(prices)

    def get(self, model: str) -> ModelPrice | None:
        return self._prices.get(model)

    def known(self, model: str) -> bool:
        return model in self._prices

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int,
                 cached_input_tokens: int = 0) -> tuple[float, bool]:
        """Return ``(cost, pricing_known)``.

        An unpriced model costs 0.00 and is flagged, never estimated.
        """
        price = self._prices.get(model)
        if price is None:
            return 0.0, False

        billable_input = max(0, input_tokens - cached_input_tokens)
        cost = (billable_input / 1_000_000) * price.input_per_mtok
        cost += (output_tokens / 1_000_000) * price.output_per_mtok
        if cached_input_tokens:
            cached_rate = (
                price.cached_input_per_mtok
                if price.cached_input_per_mtok is not None
                else price.input_per_mtok
            )
            cost += (cached_input_tokens / 1_000_000) * cached_rate
        return cost, True

    def max_cost_usd(self, model: str, prompt_tokens: int, max_output_tokens: int) -> float | None:
        """Worst-case cost of a request that has not run yet.

        Returns ``None`` when the model has no verified rate, which the
        admission gate reads as "cannot judge this, let it through and flag the
        ledger row" rather than as "free".
        """
        price = self._prices.get(model)
        if price is None:
            return None
        return (
            (prompt_tokens / 1_000_000) * price.input_per_mtok
            + (max_output_tokens / 1_000_000) * price.output_per_mtok
        )

    def unpriced(self, models: list[str]) -> list[str]:
        return [m for m in models if m not in self._prices]
