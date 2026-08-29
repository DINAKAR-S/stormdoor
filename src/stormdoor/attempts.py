"""Backoff, and the record of what was tried.

Two small things that both need to be honest.

**Backoff uses full jitter.** Exponential backoff alone synchronises clients:
everyone who failed at the same moment retries at the same moment, and the
provider that was merely struggling gets a second identical spike. Full jitter,
a uniform pick between zero and the exponential ceiling, spreads them out. It is
one line and it is the difference between a retry policy that helps and one that
finishes the job the outage started.

**Every attempt is recorded.** A request that succeeded on the third target is
not the same event as one that succeeded immediately, and the ledger should not
pretend otherwise.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


def backoff_delay(
    attempt: int, *, base: float, cap: float, rng: random.Random | None = None
) -> float:
    """Full jitter: uniform between 0 and min(cap, base * 2**attempt).

    ``attempt`` is zero for the first retry. Pass ``rng`` to make it
    deterministic in a test.
    """
    if attempt < 0:
        raise ValueError("attempt must not be negative")
    ceiling = min(cap, base * (2**attempt))
    picker = rng or random
    return picker.uniform(0.0, ceiling)


@dataclass(frozen=True, slots=True)
class Attempt:
    target: str
    outcome: str  # "ok" | "failed" | "skipped"
    detail: str | None = None

    def public(self) -> dict:
        out = {"target": self.target, "outcome": self.outcome}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(slots=True)
class AttemptLog:
    """What the gateway tried, in order, for one request."""

    attempts: list[Attempt] = field(default_factory=list)

    def skipped(self, target: str, reason: str) -> None:
        self.attempts.append(Attempt(target=target, outcome="skipped", detail=reason))

    def failed(self, target: str, detail: str) -> None:
        self.attempts.append(Attempt(target=target, outcome="failed", detail=detail))

    def succeeded(self, target: str) -> None:
        self.attempts.append(Attempt(target=target, outcome="ok"))

    @property
    def tried(self) -> int:
        """Attempts that actually reached a provider. A skip is not an attempt."""
        return sum(1 for a in self.attempts if a.outcome != "skipped")

    @property
    def served_by(self) -> str | None:
        for a in self.attempts:
            if a.outcome == "ok":
                return a.target
        return None

    @property
    def failed_over_from(self) -> str | None:
        """The first target that was tried and did not answer.

        Only set when something else eventually did, because "what did we fall
        back from" is only a question worth answering when there was a fallback.
        """
        if self.served_by is None:
            return None
        for a in self.attempts:
            if a.outcome == "failed":
                return a.target
        return None

    def public(self) -> dict:
        return {
            "attempts": [a.public() for a in self.attempts],
            "tried": self.tried,
            "served_by": self.served_by,
            "failed_over_from": self.failed_over_from,
        }
