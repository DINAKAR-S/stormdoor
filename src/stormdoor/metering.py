"""Turning the usage ledger into a bill.

The ledger already records, per request, who spent what. Metering rolls that up
per key or per tenant over a window, which is the shape a billing system wants,
and optionally pushes it to one.

Two layers, and the split is on purpose:

**Export is local and needs nothing.** ``GET /admin/usage/export`` aggregates the
ledger and returns rows a human or a billing job can read: requests, tokens, cost,
grouped by key or tenant, over any window. No external service, no key, testable
on a laptop. This is the honest default, because "send my usage somewhere" and
"let me see my usage" are different asks and only the second should require an
account.

**Pushing to a meter is opt-in and idempotent.** A ``MeterSink`` sends the same
rolled-up usage to an external billing meter. The one that ships is Stripe, behind
the ``stripe`` extra. The hard part of any usage push is not sending it, it is
sending it exactly once: a retry after a timeout must not bill the customer twice.
So every push is keyed by a deterministic period key, recorded in the same
transaction that marks it done, and refused if that period was already pushed. The
sink also stamps each event with a stable identifier so the meter itself dedupes,
which is belt and braces on top of the local record.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("stormdoor.metering")


@dataclass(frozen=True, slots=True)
class MeterEvent:
    """One usage figure for one tenant, ready to send to a meter."""

    tenant: str
    metric: str          # "requests" | "input_tokens" | "output_tokens" | "cost_usd"
    value: float
    identifier: str      # stable per (sink, period, tenant, metric): the dedup key


class MeterSink(Protocol):
    name: str

    def push(self, events: list[MeterEvent]) -> int:
        """Send the events to the meter. Returns how many were accepted."""
        ...


class StripeMeterSink:  # pragma: no cover - needs a live Stripe account
    """Pushes usage to Stripe billing meters, one meter event per figure.

    ``customer_map`` maps a tenant to a Stripe customer id, because Stripe bills a
    customer, not a free-form label. ``meter_map`` maps a metric name to the
    Stripe meter's ``event_name``. A tenant with no mapped customer is skipped and
    logged rather than guessed, because inventing a customer id is how you bill the
    wrong account.
    """

    name = "stripe"

    def __init__(self, *, api_key: str, customer_map: dict[str, str],
                 meter_map: dict[str, str]):
        try:
            import stripe
        except ImportError as exc:
            raise RuntimeError(
                "the stripe meter needs the extra: pip install 'stormdoor[stripe]'"
            ) from exc
        self._stripe = stripe
        self._stripe.api_key = api_key
        self._customers = customer_map
        self._meters = meter_map

    def push(self, events: list[MeterEvent]) -> int:
        sent = 0
        for e in events:
            customer = self._customers.get(e.tenant)
            event_name = self._meters.get(e.metric)
            if customer is None or event_name is None:
                log.warning("skipping meter event: no mapping for tenant=%r metric=%r",
                            e.tenant, e.metric)
                continue
            self._stripe.billing.MeterEvent.create(
                event_name=event_name,
                payload={"stripe_customer_id": customer, "value": str(e.value)},
                identifier=e.identifier,  # Stripe dedupes on this within its window
            )
            sent += 1
        return sent


def _period_key(sink: str, since: str | None, until: str | None) -> str:
    return f"{sink}:{since or 'start'}:{until or 'now'}"


def build_events(rows: list[dict], *, period_key: str,
                 metrics: tuple[str, ...]) -> list[MeterEvent]:
    """Turn aggregated usage rows into meter events, skipping the untenanted.

    Usage that belongs to a key with no tenant cannot be billed to anyone, so it
    is left out of a push rather than attributed to a placeholder. It is still in
    the local export, where it is visible and can be chased up.
    """
    events: list[MeterEvent] = []
    for row in rows:
        tenant = row.get("tenant")
        if not tenant:
            continue
        for metric in metrics:
            value = row.get(metric, 0)
            if not value:
                continue
            events.append(MeterEvent(
                tenant=tenant, metric=metric, value=float(value),
                identifier=f"{period_key}:{tenant}:{metric}",
            ))
    return events


async def push_usage(
    store, sink: MeterSink, *, since: str | None, until: str | None,
    metrics: tuple[str, ...] = ("input_tokens", "output_tokens", "cost_usd"),
) -> dict:
    """Aggregate the window, push it once, and record that it was pushed.

    Idempotent by construction: the period is recorded in the store before the
    result is returned, and a second call for the same window is refused there, so
    a retry after a network wobble cannot double-bill.
    """
    period_key = _period_key(sink.name, since, until)

    # Claim the period before pushing. The claim is atomic, so two concurrent
    # pushes for the same window cannot both reach the sink: the loser is told it
    # was already pushed and never calls the meter. This is what stops a
    # double-bill, rather than relying only on the meter to dedupe after the fact.
    won = await store.metering_reserve(period_key=period_key, sink=sink.name)
    if not won:
        return {"pushed": False, "reason": "already pushed", "period_key": period_key}

    try:
        rows = await store.usage_export(since=since, until=until, group_by="tenant")
        events = build_events(rows, period_key=period_key, metrics=metrics)
        sent = sink.push(events)
    except Exception:
        # The push did not complete. Release the claim so the window can be
        # retried rather than being stuck as pushed-but-empty forever.
        await store.metering_release(period_key)
        raise

    await store.metering_finalize(period_key=period_key, events=sent)
    return {"pushed": True, "events": sent, "tenants": len({e.tenant for e in events}),
            "period_key": period_key}


def build_meter_sink(settings) -> MeterSink | None:
    """The Stripe sink if it is configured, otherwise None (export-only)."""
    if not getattr(settings, "stripe_api_key", None):
        return None
    import json
    customer_map = json.loads(getattr(settings, "stripe_customer_map", None) or "{}")
    meter_map = json.loads(getattr(settings, "stripe_meter_map", None) or "{}")
    return StripeMeterSink(  # pragma: no cover - needs a live Stripe account
        api_key=settings.stripe_api_key, customer_map=customer_map, meter_map=meter_map,
    )
