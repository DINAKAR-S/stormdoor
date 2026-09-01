"""SSE resume: a dropped stream picked up from its last event id.

Unit tests pin the buffer's bounds and ownership rules, then end-to-end tests
run a real stream through the gateway and resume it through the HTTP endpoint.
"""

from __future__ import annotations

import pytest_asyncio
from conftest import chat_body
from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings
from stormdoor.resume import StreamBuffer

# ── unit: the buffer ─────────────────────────────────────────────────────────


def test_replay_returns_frames_after_the_last_seen_id():
    b = StreamBuffer()
    b.open("r1", "key1")
    for i in range(5):
        b.append("r1", f"id: {i}\ndata: {i}\n\n")
    b.mark_done("r1")
    replay = b.replay("r1", after_id=1, key_id="key1")
    assert [f.split("\n")[0] for f in replay.frames] == ["id: 2", "id: 3", "id: 4"]
    assert replay.done is True


def test_a_stream_is_only_replayable_by_the_key_that_made_it():
    b = StreamBuffer()
    b.open("r1", "key1")
    b.append("r1", "id: 0\ndata: x\n\n")
    assert b.replay("r1", after_id=-1, key_id="key2") is None, "cross-key resume leaked"
    assert b.replay("r1", after_id=-1, key_id="key1") is not None


def test_an_unknown_stream_is_none():
    b = StreamBuffer()
    assert b.replay("nope", after_id=-1, key_id="key1") is None


def test_the_frame_cap_forgets_oldest_and_reports_too_far_behind():
    b = StreamBuffer(max_frames=3)
    b.open("r1", "key1")
    for i in range(6):  # 0..5; only the last 3 survive
        b.append("r1", f"id: {i}\ndata: {i}\n\n")
    # Asking from before the forgotten line cannot be answered without a gap.
    behind = b.replay("r1", after_id=0, key_id="key1")
    assert behind.too_far_behind is True
    # Asking from inside what remains works.
    ok = b.replay("r1", after_id=3, key_id="key1")
    assert [f.split("\n")[0] for f in ok.frames] == ["id: 4", "id: 5"]
    assert ok.too_far_behind is False


def test_the_stream_cap_evicts_the_oldest_whole_stream():
    b = StreamBuffer(max_streams=2)
    b.open("r1", "k")
    b.open("r2", "k")
    b.open("r3", "k")  # evicts r1
    assert b.replay("r1", after_id=-1, key_id="k") is None
    assert b.replay("r3", after_id=-1, key_id="k") is not None
    assert b.size() == 2


# ── end to end ───────────────────────────────────────────────────────────────


def build(tmp_path, **over):
    return create_app(Settings(
        db_path=tmp_path / "resume.db", admin_token="admin",
        resume_enabled=over.pop("resume_enabled", True),
        chaos_enabled=True, _env_file=None, **over,
    ))


@pytest_asyncio.fixture
async def app_c(tmp_path):
    app = build(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        yield app, c
    app.state.store.close()


async def _drain(resp):
    frames = []
    async for line in resp.aiter_lines():
        frames.append(line)
    return "\n".join(frames)


async def test_a_completed_stream_can_be_resumed_from_the_middle(app_c):
    app, c = app_c
    _k, s = await app.state.store.create_key(name="k")
    auth = {"Authorization": f"Bearer {s}"}

    async with c.stream("POST", "/v1/chat/completions",
                        json=chat_body(stream=True, max_tokens=64), headers=auth) as r:
        request_id = r.headers["X-Stormdoor-Request-Id"]
        full = await _drain(r)
    # Count the id-carrying frames the client saw.
    ids = [ln for ln in full.splitlines() if ln.startswith("id: ")]
    assert len(ids) >= 3

    # Resume as if the client only saw up to id 1: it must get the rest.
    resumed = await c.get(f"/v1/stream/{request_id}",
                          headers={**auth, "Last-Event-ID": "1"})
    assert resumed.status_code == 200
    body = await _drain(resumed)
    resumed_ids = [ln for ln in body.splitlines() if ln.startswith("id: ")]
    assert "id: 2" in resumed_ids
    # Line-exact, not substring: "id: 1" must not match "id: 10".
    assert "id: 0" not in resumed_ids and "id: 1" not in resumed_ids, \
        "resume replayed frames the client already had"
    assert "[DONE]" in body


async def test_resuming_someone_elses_stream_is_a_404(app_c):
    app, c = app_c
    _k1, s1 = await app.state.store.create_key(name="one")
    _k2, s2 = await app.state.store.create_key(name="two")
    h1 = {"Authorization": f"Bearer {s1}"}
    async with c.stream("POST", "/v1/chat/completions",
                        json=chat_body(stream=True), headers=h1) as r:
        request_id = r.headers["X-Stormdoor-Request-Id"]
        await _drain(r)
    poached = await c.get(f"/v1/stream/{request_id}",
                          headers={"Authorization": f"Bearer {s2}", "Last-Event-ID": "0"})
    assert poached.status_code == 404


async def test_resume_is_a_clean_error_when_disabled(tmp_path):
    app = build(tmp_path, resume_enabled=False)
    _k, s = await app.state.store.create_key(name="k")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        r = await c.get("/v1/stream/whatever",
                        headers={"Authorization": f"Bearer {s}", "Last-Event-ID": "0"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "resume_disabled"
    app.state.store.close()


async def test_an_unknown_request_id_is_a_404(app_c):
    _app, c = app_c
    _k, s = await _app.state.store.create_key(name="k")
    r = await c.get("/v1/stream/req_does_not_exist",
                    headers={"Authorization": f"Bearer {s}", "Last-Event-ID": "0"})
    assert r.status_code == 404
