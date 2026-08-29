"""Fault injection, as a feature rather than a test script.

This is the part of stormdoor that the other gateways do not have. A gateway's
whole value is what it does when the provider misbehaves, and that behaviour is
almost never exercised, because provoking a real 503 or a real mid-stream
disconnect is inconvenient. So the failures are simulated here, at the provider
boundary, against real traffic, on demand.

Faults
------
``error``            fail before the upstream call, with a chosen status code
``timeout``          hang past the request timeout
``slow``             delay, then proceed normally (useful for time-to-first-token work)
``mid_stream_abort`` stream ``after_chunks`` chunks, then die mid-response

Safety
------
Off unless ``STORMDOOR_CHAOS_ENABLED=true``. While off, the request header is
not even parsed, so a caller cannot induce failures in a production deployment.
Every injected fault is tagged in the usage ledger, so a drill is never
mistaken for a real outage when you read the history back.

Spec syntax, as a header or as ``STORMDOOR_CHAOS_DEFAULT``::

    X-Stormdoor-Chaos: fault=error;status=503;p=1.0
    X-Stormdoor-Chaos: fault=mid_stream_abort;after_chunks=5
    X-Stormdoor-Chaos: fault=slow;delay_ms=800;p=0.25;seed=7

``seed`` makes a probabilistic fault reproducible, which is what turns a demo
into a regression test.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Literal

from .errors import BadRequest, ChaosInjected

Fault = Literal["none", "error", "timeout", "slow", "mid_stream_abort"]

_VALID_FAULTS = {"none", "error", "timeout", "slow", "mid_stream_abort"}

HEADER = "x-stormdoor-chaos"


@dataclass(frozen=True, slots=True)
class ChaosSpec:
    fault: Fault = "none"
    probability: float = 1.0
    status_code: int = 503
    after_chunks: int = 3
    delay_ms: int = 1000
    seed: int | None = None

    @property
    def active(self) -> bool:
        return self.fault != "none"

    def describe(self) -> str:
        if not self.active:
            return "none"
        bits = [f"fault={self.fault}"]
        if self.fault == "error":
            bits.append(f"status={self.status_code}")
        if self.fault == "mid_stream_abort":
            bits.append(f"after_chunks={self.after_chunks}")
        if self.fault in ("slow", "timeout"):
            bits.append(f"delay_ms={self.delay_ms}")
        if self.probability != 1.0:
            bits.append(f"p={self.probability}")
        return ";".join(bits)


NO_CHAOS = ChaosSpec()


def parse_spec(raw: str | None) -> ChaosSpec:
    """Parse a spec string. Raises BadRequest on anything malformed.

    A silently ignored typo in a fault spec is worse than an error: the drill
    appears to pass while injecting nothing at all.
    """
    if not raw or not raw.strip():
        return NO_CHAOS

    fields: dict[str, str] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise BadRequest(f"malformed chaos spec segment: {chunk!r}", param="chaos")
        key, _, value = chunk.partition("=")
        fields[key.strip().lower()] = value.strip()

    unknown = set(fields) - {"fault", "p", "probability", "status", "after_chunks",
                             "delay_ms", "seed"}
    if unknown:
        raise BadRequest(f"unknown chaos fields: {sorted(unknown)}", param="chaos")

    fault = fields.get("fault", "none").lower()
    if fault not in _VALID_FAULTS:
        raise BadRequest(
            f"unknown chaos fault {fault!r}, expected one of {sorted(_VALID_FAULTS)}",
            param="chaos",
        )

    def _num(key: str, default, cast):
        if key not in fields:
            return default
        try:
            return cast(fields[key])
        except ValueError as exc:
            raise BadRequest(f"chaos field {key!r} is not a number: {fields[key]!r}",
                             param="chaos") from exc

    probability = _num("p", _num("probability", 1.0, float), float)
    if not 0.0 <= probability <= 1.0:
        raise BadRequest(f"chaos probability must be between 0 and 1, got {probability}",
                         param="chaos")

    return ChaosSpec(
        fault=fault,  # type: ignore[arg-type]
        probability=probability,
        status_code=_num("status", 503, int),
        after_chunks=_num("after_chunks", 3, int),
        delay_ms=_num("delay_ms", 1000, int),
        seed=_num("seed", None, int),
    )


class ChaosGate:
    """Decides whether a given request gets a fault, and applies it.

    One instance per request. Holding the roll on the instance means a
    probabilistic fault is decided once and then applies consistently to both
    the pre-call hook and the mid-stream hook, instead of being re-rolled.
    """

    def __init__(self, spec: ChaosSpec, *, enabled: bool):
        self.spec = spec if enabled else NO_CHAOS
        self._fired = self._roll()

    def _roll(self) -> bool:
        if not self.spec.active:
            return False
        if self.spec.probability >= 1.0:
            return True
        rng = random.Random(self.spec.seed) if self.spec.seed is not None else random
        return rng.random() < self.spec.probability

    @property
    def armed(self) -> bool:
        """True when this request has been selected to fail."""
        return self._fired

    @property
    def label(self) -> str | None:
        return self.spec.describe() if self._fired else None

    async def before_call(self) -> None:
        """Run before the upstream request. May raise, hang, or delay."""
        if not self._fired:
            return
        if self.spec.fault == "error":
            raise ChaosInjected(
                f"injected fault: upstream returned {self.spec.status_code}",
                fault=self.spec.describe(),
                status_code=self.spec.status_code,
            )
        if self.spec.fault == "timeout":
            # Sleep far past any sane request timeout. The caller's timeout is
            # what should fire, which is exactly the path being tested.
            await asyncio.sleep(3600)
        if self.spec.fault == "slow":
            await asyncio.sleep(self.spec.delay_ms / 1000)

    def should_abort_stream(self, chunks_sent: int) -> bool:
        """True when the stream should die right now."""
        return (
            self._fired
            and self.spec.fault == "mid_stream_abort"
            and chunks_sent >= self.spec.after_chunks
        )

    def abort_error(self) -> ChaosInjected:
        return ChaosInjected(
            f"injected fault: upstream died after {self.spec.after_chunks} chunks",
            fault=self.spec.describe(),
            status_code=self.spec.status_code,
        )
