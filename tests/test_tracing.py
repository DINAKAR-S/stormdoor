"""Tracing: one span per request, metadata only unless content is opted in.

The OTel SDK is not a test dependency, so these tests use a recording tracer that
implements the same tiny interface, plus a unit test of the attribute builder.
They assert what goes into a span, which is the part that can leak, rather than
that OTel itself works.
"""

from __future__ import annotations

from conftest import chat_body
from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings
from stormdoor.tracing import NoopTracer, request_attributes


class RecordingTracer:
    def __init__(self):
        self.spans = []

    def record_request(self, *, name, start_unix_ns, end_unix_ns, attributes, error=None):
        self.spans.append({"name": name, "attrs": attributes, "error": error,
                           "duration_ns": end_unix_ns - start_unix_ns})


def build(tmp_path, tracer, **over):
    app = create_app(Settings(
        db_path=tmp_path / "trace.db", admin_token="admin", chaos_enabled=True,
        _env_file=None, **over,
    ))
    app.state.gateway.tracer = tracer
    return app


# ── unit: the attribute set ─────────────────────────────────────────────────


def test_content_is_absent_unless_a_preview_is_passed():
    attrs = request_attributes(
        requested_model="echo-small", served_model="echo-small", provider="echo",
        input_tokens=3, output_tokens=5, cost_usd=0.001, status="ok", cache_hit=False,
        attempts=1, failed_over_from=None, chaos_fault=None,
    )
    assert "gen_ai.prompt" not in attrs
    assert "gen_ai.completion" not in attrs
    assert attrs["gen_ai.usage.input_tokens"] == 3
    assert attrs["stormdoor.cost_usd"] == 0.001


def test_content_is_truncated_when_included():
    attrs = request_attributes(
        requested_model="m", served_model="m", provider="p", input_tokens=0,
        output_tokens=0, cost_usd=0.0, status="ok", cache_hit=None, attempts=1,
        failed_over_from=None, chaos_fault=None,
        prompt_preview="x" * 5000, completion_preview="y" * 5000,
    )
    assert len(attrs["gen_ai.prompt"]) <= 200
    assert len(attrs["gen_ai.completion"]) <= 200


# ── end to end ───────────────────────────────────────────────────────────────


async def test_a_request_emits_exactly_one_span(tmp_path):
    tracer = RecordingTracer()
    app = build(tmp_path, tracer)
    _k, s = await app.state.store.create_key(name="k")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        await c.post("/v1/chat/completions", json=chat_body(),
                     headers={"Authorization": f"Bearer {s}"})
    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span["attrs"]["gen_ai.request.model"] == "echo-small"
    assert span["attrs"]["stormdoor.status"] == "ok"
    assert "gen_ai.prompt" not in span["attrs"], "content leaked into a span by default"
    app.state.store.close()


async def test_a_refusal_emits_a_span_marked_error(tmp_path):
    tracer = RecordingTracer()
    app = build(tmp_path, tracer)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        r = await c.post("/v1/chat/completions", json=chat_body(model="nope"),
                         headers={"Authorization": "Bearer sd-wrong"})
    # A bad key never reaches admission, so there is no span; a routable refusal
    # does. Use an unknown model on a valid key to get a recorded refusal.
    assert r.status_code == 401
    tracer2 = RecordingTracer()
    app2 = build(tmp_path, tracer2, db="t2")
    _k, s = await app2.state.store.create_key(name="k", budget_usd=0.0,
                                              allowed_models=["echo-large"])
    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://x") as c:
        await c.post("/v1/chat/completions", json=chat_body(model="echo-small"),
                     headers={"Authorization": f"Bearer {s}"})
    assert len(tracer2.spans) == 1
    assert tracer2.spans[0]["attrs"]["stormdoor.status"] == "refused"
    assert tracer2.spans[0]["error"] is not None
    app.state.store.close()
    app2.state.store.close()


async def test_content_appears_only_when_opted_in(tmp_path):
    tracer = RecordingTracer()
    app = build(tmp_path, tracer, otel_include_content=True)
    _k, s = await app.state.store.create_key(name="k")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        await c.post("/v1/chat/completions", json=chat_body(
            messages=[{"role": "user", "content": "trace this prompt"}]),
            headers={"Authorization": f"Bearer {s}"})
    assert "trace this prompt" in tracer.spans[0]["attrs"]["gen_ai.prompt"]
    app.state.store.close()


def test_the_default_tracer_is_a_noop(tmp_path):
    app = create_app(Settings(db_path=tmp_path / "d.db", admin_token="a", _env_file=None))
    assert isinstance(app.state.gateway.tracer, NoopTracer)
    app.state.store.close()
