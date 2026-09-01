"""One OpenTelemetry span per request, so a gateway call joins up with the rest
of your system's traces instead of being a black box in the middle.

Off by default, and off means nothing: no dependency imported, no span created,
no overhead. Turn it on and every request emits a span carrying what the ledger
already knows plus the trace context, exported wherever your collector lives.

**Prompt and completion text are not in the span.** This is deliberate and it is
the whole reason the default is metadata only. Traces routinely leave your
process for a third-party vendor, and a gateway that ships PII redaction on the
request path must not turn around and post the same text into a trace. The span
carries model, provider, token counts, cost, latency, cache and failover
outcome. If you knowingly want the content for debugging, `otel_include_content`
adds a truncated preview, and the README says plainly what that means.

The attribute names follow the OpenTelemetry GenAI semantic conventions where
they exist (`gen_ai.*`), and use a `stormdoor.*` namespace for the things those
conventions do not cover, like the cost and the failover trail.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("stormdoor.tracing")

_PREVIEW_CHARS = 200


class Tracer(Protocol):
    def record_request(self, *, name: str, start_unix_ns: int, end_unix_ns: int,
                       attributes: dict, error: str | None = None) -> None:
        ...


class NoopTracer:
    """The default. Every method is a no-op, so tracing off costs nothing."""

    def record_request(self, *, name, start_unix_ns, end_unix_ns, attributes,
                        error=None) -> None:
        return None


class OtelTracer:  # pragma: no cover - needs the otel SDK and a collector
    """Emits a completed span per request against a configured exporter.

    The SDK is imported lazily, exactly like the provider and vector-store
    adapters, so importing this module never pulls OpenTelemetry into a
    deployment that has tracing off. Spans are created with explicit start and
    end timestamps taken from the request context, because the gateway records
    them once at the end of the request rather than wrapping the whole path in a
    live span, which keeps instrumentation to one choke point.
    """

    def __init__(self, *, service_name: str = "stormdoor", endpoint: str | None = None,
                 console: bool = False):
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import (
                BatchSpanProcessor,
                ConsoleSpanExporter,
            )
        except ImportError as exc:
            raise RuntimeError(
                "tracing needs the extra: pip install 'stormdoor[otel]'"
            ) from exc

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        if console or not endpoint:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        self._tracer = trace.get_tracer("stormdoor")
        self._trace = trace

    def record_request(self, *, name, start_unix_ns, end_unix_ns, attributes,
                        error=None) -> None:
        span = self._tracer.start_span(name, start_time=start_unix_ns)
        for k, v in attributes.items():
            if v is not None:
                span.set_attribute(k, v)
        if error:
            from opentelemetry.trace import Status, StatusCode
            span.set_status(Status(StatusCode.ERROR, error))
        span.end(end_time=end_unix_ns)


def request_attributes(
    *, requested_model: str, served_model: str, provider: str, input_tokens: int,
    output_tokens: int, cost_usd: float, status: str, cache_hit: bool | None,
    attempts: int, failed_over_from: str | None, chaos_fault: str | None,
    prompt_preview: str | None = None, completion_preview: str | None = None,
) -> dict:
    """The attribute set for one request span. Content is included only when a
    preview is passed, which the gateway does only if otel_include_content is on."""
    attrs: dict = {
        "gen_ai.system": "stormdoor",
        "gen_ai.request.model": requested_model,
        "gen_ai.response.model": served_model,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "stormdoor.provider": provider,
        "stormdoor.cost_usd": round(cost_usd, 8),
        "stormdoor.status": status,
        "stormdoor.attempts": attempts,
    }
    if cache_hit is not None:
        attrs["stormdoor.cache_hit"] = cache_hit
    if failed_over_from:
        attrs["stormdoor.failed_over_from"] = failed_over_from
    if chaos_fault:
        attrs["stormdoor.chaos_fault"] = chaos_fault
    if prompt_preview is not None:
        attrs["gen_ai.prompt"] = prompt_preview[:_PREVIEW_CHARS]
    if completion_preview is not None:
        attrs["gen_ai.completion"] = completion_preview[:_PREVIEW_CHARS]
    return attrs


def build_tracer(settings) -> Tracer:
    """Pick the tracer named in settings. Noop unless tracing is enabled."""
    if not getattr(settings, "otel_enabled", False):
        return NoopTracer()
    return OtelTracer(
        service_name=getattr(settings, "otel_service_name", "stormdoor"),
        endpoint=getattr(settings, "otel_exporter_endpoint", None),
        console=getattr(settings, "otel_console", False),
    )
