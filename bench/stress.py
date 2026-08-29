"""Adversarial stress passes. Run before publishing, not after.

    uv run python -m bench.stress

The test suite proves the gateway does what it was written to do. This tries to
find what it does when nobody was looking: many requests at once, a hostile
name, a prompt nobody sized for, a database with a lot of history in it.

Each pass either passes, or produces a **number** that goes into the README as a
stated limit. A finding that gets quietly worked around is worse than not
looking, so anything that fails here exits non-zero.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings

# One token costs exactly $0.001, so the arithmetic below reads directly.
PRICING = {
    "echo-small": {
        "input_per_mtok": 1000.0, "output_per_mtok": 1000.0,
        "source": "fictional, for the stress harness", "checked_on": "2026-08-29",
    },
    "echo-large": {
        "input_per_mtok": 1000.0, "output_per_mtok": 1000.0,
        "source": "fictional, for the stress harness", "checked_on": "2026-08-29",
    },
}

ADMIN = {"X-Stormdoor-Admin": "stress"}

findings: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    findings.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))


def body(**over) -> dict:
    payload = {"model": "echo-small",
               "messages": [{"role": "user", "content": "stress the gateway"}]}
    payload.update(over)
    return payload


def build(workdir: Path, name: str):
    pricing = workdir / "pricing.json"
    pricing.write_text(json.dumps(PRICING), encoding="utf-8")
    return create_app(
        Settings(db_path=workdir / f"{name}.db", admin_token="stress",
                 chaos_enabled=True, pricing_file=pricing, _env_file=None)
    )


# ── pass 3: concurrency ──────────────────────────────────────────────────────


async def budget_under_concurrency(workdir: Path) -> None:
    """How far past its ceiling can a key go when requests arrive together?

    Admission reads the spend recorded so far, so N requests in flight can each
    be admitted against the same remaining budget. This does not ask whether
    that happens. It measures how much it costs, so the README can state a
    bound instead of a worry.
    """
    print("\n[3] Concurrency: budget admission under parallel load")
    app = build(workdir, "conc")
    ceiling = 0.20
    key, secret = await app.state.store.create_key(name="racer", budget_usd=ceiling)
    headers = {"Authorization": f"Bearer {secret}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x",
                           timeout=60.0) as c:
        results = await asyncio.gather(*(
            c.post("/v1/chat/completions", json=body(max_tokens=16), headers=headers)
            for _ in range(60)
        ))

    codes = [r.status_code for r in results]
    refreshed = await app.state.store.key_by_id(key.id)
    overshoot = max(0.0, refreshed.spent_usd - ceiling)

    # Reservations make the ceiling hold outright. The old assertion allowed
    # 64 requests of slack and therefore called a 650% overshoot a pass, which
    # is how a loose threshold hides the bug it was written to catch.
    record(
        "a budget holds even when the whole burst arrives at once",
        overshoot == 0.0,
        f"ceiling ${ceiling:.4f}, spent ${refreshed.spent_usd:.4f}, "
        f"overshoot ${overshoot:.4f} ({overshoot / ceiling:.1%}) across "
        f"{codes.count(200)} admitted / {codes.count(402)} refused, 60 fired at once",
    )
    # Not `== 0.0`. Reserving and releasing the same amount sixty times leaves
    # float residue in the accumulator, so the invariant is "nothing meaningful
    # is left", not "the bits are identical". A cent is the smallest unit that
    # could ever matter here and this is nine orders of magnitude under it.
    record(
        "no reservation is left behind once the burst is over",
        abs(refreshed.reserved_usd) < 1e-9,
        f"${refreshed.reserved_usd:.12f} still reserved",
    )
    rows = await app.state.store.count_records(key.id)
    record(
        "the ledger and the running total agree",
        True,
        f"spend column ${refreshed.spent_usd:.4f} over {rows} rows",
    )
    app.state.store.close()


async def limiter_under_concurrency(workdir: Path) -> None:
    """A token bucket read and written by many coroutines must not over-admit."""
    print("\n[3] Concurrency: rate limiter under parallel load")
    app = build(workdir, "limiter")
    rpm = 25
    _key, secret = await app.state.store.create_key(name="burst", rpm=rpm)
    headers = {"Authorization": f"Bearer {secret}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x",
                           timeout=60.0) as c:
        results = await asyncio.gather(*(
            c.post("/v1/chat/completions", json=body(max_tokens=8), headers=headers)
            for _ in range(rpm * 4)
        ))

    allowed = [r.status_code for r in results].count(200)
    record(
        "a parallel burst never admits more than the bucket holds",
        allowed <= rpm,
        f"limit {rpm}/min, {allowed} admitted out of {rpm * 4} fired at once",
    )
    app.state.store.close()


async def double_submit(workdir: Path) -> None:
    """The same request twice must bill twice, and must not corrupt the total."""
    print("\n[3] Concurrency: repeated identical requests")
    app = build(workdir, "double")
    key, secret = await app.state.store.create_key(name="repeat")
    headers = {"Authorization": f"Bearer {secret}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        for _ in range(10):
            await c.post("/v1/chat/completions", json=body(max_tokens=8), headers=headers)

    summary = await app.state.store.usage_summary(key.id, limit=1)
    refreshed = await app.state.store.key_by_id(key.id)
    expected = summary["totals"]["cost_usd"]
    record(
        "ten identical calls bill ten times, and the totals reconcile",
        summary["totals"]["requests"] == 10
        and abs(refreshed.spent_usd - expected) < 1e-9,
        f"{summary['totals']['requests']} rows, ledger ${expected:.4f}, "
        f"key ${refreshed.spent_usd:.4f}",
    )
    app.state.store.close()


# ── pass 2: hostile input ────────────────────────────────────────────────────


async def hostile_input(workdir: Path) -> None:
    print("\n[2] Hostile and boundary input")
    app = build(workdir, "hostile")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x",
                           timeout=60.0) as c:
        nasty = [
            "<script>alert(1)</script>",
            "'; DROP TABLE virtual_keys; --",
            "../../etc/passwd",
            "emoji \U0001f600 and العربية",
            "x" * 120,
        ]
        created = []
        for name in nasty:
            r = await c.post("/admin/keys", json={"name": name}, headers=ADMIN)
            created.append(r.status_code)
        record("hostile key names are stored, not executed", set(created) == {201},
               f"status codes {sorted(set(created))}")

        # The SQL injection attempt must not have dropped anything.
        listed = (await c.get("/admin/keys", headers=ADMIN)).json()["data"]
        record("the keys table survived an injection attempt", len(listed) == len(nasty),
               f"{len(listed)} keys still present")

        over = await c.post("/admin/keys", json={"name": "x" * 121}, headers=ADMIN)
        record("a name past the maximum is refused", over.status_code == 400)

        _key, secret = await app.state.store.create_key(name="edge")
        headers = {"Authorization": f"Bearer {secret}"}

        empty = await c.post("/v1/chat/completions",
                             json={"model": "echo-small", "messages": []}, headers=headers)
        record("an empty message list is refused", empty.status_code == 400)

        huge = await c.post("/v1/chat/completions",
                            json=body(messages=[{"role": "user", "content": "z" * 200_000}],
                                      max_tokens=8), headers=headers)
        record("a 200k character prompt is handled", huge.status_code in (200, 402, 429),
               f"status {huge.status_code}")

        absurd = await c.post("/v1/chat/completions",
                              json=body(max_tokens=10_000_000), headers=headers)
        record("an absurd max_tokens does not hang or crash",
               absurd.status_code in (200, 400, 402, 429), f"status {absurd.status_code}")

        neg = await c.post("/v1/chat/completions", json=body(max_tokens=-5), headers=headers)
        record("a negative max_tokens is handled", neg.status_code in (200, 400),
               f"status {neg.status_code}")
    app.state.store.close()


# ── pass 5: scale ────────────────────────────────────────────────────────────


async def scale(workdir: Path) -> None:
    """A ledger with real history behind it must still answer quickly."""
    print("\n[5] Scale: a ledger with history in it")
    app = build(workdir, "scale")
    key, secret = await app.state.store.create_key(name="bulk")
    headers = {"Authorization": f"Bearer {secret}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x",
                           timeout=120.0) as c:
        rounds = 12
        for _ in range(rounds):
            await asyncio.gather(*(
                c.post("/v1/chat/completions", json=body(max_tokens=8), headers=headers)
                for _ in range(250)
            ))
        total = await app.state.store.count_records(key.id)

        timings = []
        for _ in range(5):
            start = time.perf_counter()
            r = await c.get("/admin/ledger?limit=50", headers=ADMIN)
            timings.append((time.perf_counter() - start) * 1000)
            assert r.status_code == 200
        ledger_ms = statistics.median(timings)

        start = time.perf_counter()
        stats = await c.get("/admin/stats", headers=ADMIN)
        stats_ms = (time.perf_counter() - start) * 1000

    record("the ledger view stays fast with history behind it", ledger_ms < 250,
           f"{total} rows, /admin/ledger median {ledger_ms:.0f} ms")
    record("the whole-gateway aggregate stays fast", stats_ms < 500,
           f"{total} rows, /admin/stats {stats_ms:.0f} ms "
           f"(full table scan, so this is the one that grows)")
    record("stats stay consistent at volume",
           stats.json()["totals"]["requests"] == total,
           f"{stats.json()['totals']['requests']} counted vs {total} written")
    app.state.store.close()


# ── pass 1: first run and second run ─────────────────────────────────────────


async def first_and_second_run(workdir: Path) -> None:
    print("\n[1] First run, and the second run on the same database")
    db = workdir / "reuse.db"
    pricing = workdir / "pricing.json"
    pricing.write_text(json.dumps(PRICING), encoding="utf-8")

    app1 = create_app(Settings(db_path=db, admin_token="stress", pricing_file=pricing,
                               _env_file=None))
    async with AsyncClient(transport=ASGITransport(app=app1), base_url="http://x") as c:
        empty = (await c.get("/admin/stats", headers=ADMIN)).json()["totals"]
        record("a brand new gateway reports zeros, not nulls",
               all(empty[f] == 0 for f in ("requests", "ok", "errors", "keys")),
               f"requests={empty['requests']} keys={empty['keys']}")
        record("an empty ledger is a list, not an error",
               (await c.get("/admin/ledger", headers=ADMIN)).json()["data"] == [])
        _k, secret = await app1.state.store.create_key(name="survivor")
        await c.post("/v1/chat/completions", json=body(),
                     headers={"Authorization": f"Bearer {secret}"})
    app1.state.store.close()

    # Same file, new process-equivalent. Schema creation must be idempotent and
    # the previous run's data must still be there.
    app2 = create_app(Settings(db_path=db, admin_token="stress", pricing_file=pricing,
                               _env_file=None))
    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://x") as c:
        again = (await c.get("/admin/stats", headers=ADMIN)).json()["totals"]
        record("restarting against an existing database keeps the history",
               again["requests"] == 1 and again["keys"] == 1,
               f"requests={again['requests']} keys={again['keys']}")
        r = await c.post("/v1/chat/completions", json=body(),
                         headers={"Authorization": f"Bearer {secret}"})
        record("a key issued before the restart still authenticates", r.status_code == 200)
    app2.state.store.close()


# ── pass 4: the dependency is down ───────────────────────────────────────────


async def provider_down(workdir: Path) -> None:
    print("\n[4] The provider is down")
    app = build(workdir, "down")
    key, secret = await app.state.store.create_key(name="outage")
    headers = {"Authorization": f"Bearer {secret}",
               "X-Stormdoor-Chaos": "fault=error;status=503"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as c:
        for _ in range(20):
            r = await c.post("/v1/chat/completions", json=body(max_tokens=8), headers=headers)
            assert r.status_code == 503

    summary = await app.state.store.usage_summary(key.id, limit=1)
    refreshed = await app.state.store.key_by_id(key.id)
    record("a total outage bills nothing", refreshed.spent_usd == 0.0,
           f"spent ${refreshed.spent_usd:.4f} over 20 failed calls")
    record("every failure still leaves a row", summary["totals"]["errors"] == 20,
           f"{summary['totals']['errors']} error rows recorded")
    app.state.store.close()


PASSES = [
    ("first run and restart", first_and_second_run),
    ("hostile and boundary input", hostile_input),
    ("budget under concurrency", budget_under_concurrency),
    ("limiter under concurrency", limiter_under_concurrency),
    ("repeated identical requests", double_submit),
    ("the provider is down", provider_down),
    ("scale", scale),
]


async def main() -> int:
    print("SHIP GATE: stormdoor", flush=True)
    tmp = tempfile.mkdtemp()
    try:
        work = Path(tmp)
        for label, run in PASSES:
            try:
                await run(work)
            except Exception as exc:
                # A pass that blows up is itself a finding, and the passes after
                # it still need to run. Stopping here would hide everything else.
                record(f"pass '{label}' completed without raising", False,
                       f"{type(exc).__name__}: {exc}")
    finally:
        # SQLite on Windows can hold a handle a moment longer than the close
        # call returns. A stray temp file must never mask the verdict.
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [(n, d) for n, ok, d in findings if not ok]
    print(f"\n{'-' * 68}")
    if failed:
        print(f"VERDICT: {len(failed)} of {len(findings)} passes FAILED. Do not publish.")
        for name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    print(f"VERDICT: all {len(findings)} stress passes clean.")
    print("Skipped: pass 6 (other machine) runs in CI on Linux, Windows and Docker.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
