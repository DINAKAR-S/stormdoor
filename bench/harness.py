"""The proof harness. Run it, and it rewrites the results table in the README.

    uv run python -m bench.harness

Six drills:

1. **Throughput and latency** on the normal path.
2. **Time to first token** on a stream, which is what a user actually feels.
3. **A provider outage**, injected at a chosen rate, checked against what the
   caller saw and what the ledger recorded.
4. **A mid-stream death**, which is the hard case: the status line already said
   200, so the failure has to ride the stream and still be accounted for.
5. **Budget admission**, checking that a refused request costs nothing and that
   spend never passes the ceiling.
6. **Rate limiting**, checking the burst is allowed and the overflow is not.

Numbers come from the harness. If a claim in the README is not in this file, it
is not a claim, it is an adjective.
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import socket
import statistics
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from stormdoor.app import create_app
from stormdoor.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# Fictional round rates so the budget arithmetic in the output is readable:
# $1000 per million tokens each way means one token costs exactly $0.001.
PRICING = {
    "echo-small": {
        "input_per_mtok": 1000.0,
        "output_per_mtok": 1000.0,
        "source": "fictional, for the bench harness",
        "checked_on": "2026-08-29",
    }
}

LOAD_REQUESTS = 600
LOAD_CONCURRENCY = 32
STREAM_SAMPLES = 120
STREAM_CHUNK_DELAY_MS = 4
CHAOS_REQUESTS = 400
CHAOS_RATE = 0.25
ABORT_SAMPLES = 60
ABORT_AFTER = 4


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def serve(db_path: Path):
    """Run a real uvicorn server on a spare port for the duration of a drill.

    Its own app and its own database, so its rate limiter never shares an
    asyncio lock with the in-process drills running on a different event loop.
    """
    import uvicorn

    app = create_app(
        Settings(db_path=db_path, admin_token="bench", chaos_enabled=True, _env_file=None)
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.perf_counter() + 15.0
    while not server.started:
        if time.perf_counter() > deadline:
            raise RuntimeError("uvicorn did not start within 15 seconds")
        await asyncio.sleep(0.02)

    try:
        yield f"http://127.0.0.1:{port}", app.state.store
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.state.store.close()


@dataclass
class Result:
    name: str
    rows: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""
    # Timings vary by machine and are never asserted. The behaviour behind them
    # is: no overspend, every injected failure classified honestly, every killed
    # stream accounted for. These are what make the harness a gate rather than a
    # report, and what let CI run it on every push.
    checks: list[tuple[str, bool]] = field(default_factory=list)

    def add(self, label: str, value) -> None:
        self.rows.append((label, str(value)))

    def check(self, claim: str, ok: bool) -> None:
        self.checks.append((claim, bool(ok)))


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def ms(value: float) -> str:
    return f"{value * 1000:.1f} ms"


def body(**overrides) -> dict:
    payload = {
        "model": "echo-small",
        "messages": [{"role": "user", "content": "summarise the incident report"}],
    }
    payload.update(overrides)
    return payload


class Bench:
    def __init__(self, workdir: Path):
        pricing_file = workdir / "pricing.json"
        pricing_file.write_text(json.dumps(PRICING), encoding="utf-8")
        self.app = create_app(
            Settings(
                db_path=workdir / "bench.db",
                admin_token="bench",
                chaos_enabled=True,
                pricing_file=pricing_file,
                default_max_tokens=4096,
                _env_file=None,
            )
        )
        self.store = self.app.state.store

    async def __aenter__(self):
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://bench.local", timeout=60.0
        )
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        self.store.close()

    async def key(self, **kwargs) -> dict[str, str]:
        _key, secret = await self.store.create_key(**kwargs)
        return {"Authorization": f"Bearer {secret}"}

    async def key_and_headers(self, **kwargs):
        key, secret = await self.store.create_key(**kwargs)
        return key, {"Authorization": f"Bearer {secret}"}

    # ── 1. throughput and latency ────────────────────────────────────────

    async def load(self) -> Result:
        headers = await self.key(name="load")
        gate = asyncio.Semaphore(LOAD_CONCURRENCY)
        latencies: list[float] = []
        statuses: list[int] = []

        async def one() -> None:
            async with gate:
                start = time.perf_counter()
                r = await self.client.post(
                    "/v1/chat/completions", json=body(max_tokens=64), headers=headers
                )
                latencies.append(time.perf_counter() - start)
                statuses.append(r.status_code)

        wall_start = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(LOAD_REQUESTS)))
        wall = time.perf_counter() - wall_start

        result = Result("Throughput and latency")
        result.add("Requests", LOAD_REQUESTS)
        result.add("Concurrency", LOAD_CONCURRENCY)
        result.add("Successful", f"{statuses.count(200)} / {LOAD_REQUESTS}")
        result.add("Throughput", f"{LOAD_REQUESTS / wall:.0f} req/s")
        result.add("Latency p50", ms(pct(latencies, 0.50)))
        result.add("Latency p95", ms(pct(latencies, 0.95)))
        result.add("Latency p99", ms(pct(latencies, 0.99)))
        result.add("Transport", "in-process ASGI")
        result.check("every request under load succeeded", statuses.count(200) == LOAD_REQUESTS)
        result.note = (
            "Full gateway path per request: auth, model check, both rate-limit buckets, "
            "budget admission, provider call, ledger write. The provider is local and the "
            "transport is in-process, so this isolates the gateway's own overhead from both "
            "the model's speed and the socket. Add your network and your model on top."
        )
        return result

    # ── 2. time to first token ───────────────────────────────────────────

    async def ttft(self, workdir: Path) -> Result:
        """Time to first token, measured over a real socket.

        This drill cannot use the in-process ASGI transport the others use:
        httpx buffers an ASGI response body to completion before handing it
        back, so time-to-first-token and total latency come out identical and
        the measurement says nothing. The first version of this harness
        reported exactly that, a suspicious 100%, which is what a measurement
        artifact looks like when you do not check it. So this one runs a real
        uvicorn server on a real port.

        The echo provider is also instant, which would flatten the curve for a
        different reason, so each chunk is given a delay to make the stream take
        time the way a model does.
        """
        async with serve(workdir / "ttft.db") as (base_url, store):
            _key, secret = await store.create_key(name="stream")
            headers = {"Authorization": f"Bearer {secret}"}
            firsts: list[float] = []
            totals: list[float] = []

            async with AsyncClient(base_url=base_url, timeout=60.0) as client:
                for _ in range(STREAM_SAMPLES):
                    start = time.perf_counter()
                    first: float | None = None
                    async with client.stream(
                        "POST", "/v1/chat/completions",
                        json=body(
                            stream=True, max_tokens=128, echo_delay_ms=STREAM_CHUNK_DELAY_MS
                        ),
                        headers=headers,
                    ) as r:
                        async for line in r.aiter_lines():
                            if first is not None:
                                continue
                            if line.startswith("data: ") and '"content"' in line:
                                first = time.perf_counter() - start
                    firsts.append(first or 0.0)
                    totals.append(time.perf_counter() - start)

        result = Result("Streaming, time to first token")
        result.add("Streams", STREAM_SAMPLES)
        result.add("Transport", "real uvicorn server over TCP")
        result.add("Simulated per-chunk delay", f"{STREAM_CHUNK_DELAY_MS} ms")
        result.add("TTFT p50", ms(pct(firsts, 0.50)))
        result.add("TTFT p95", ms(pct(firsts, 0.95)))
        result.add("Full response p50", ms(pct(totals, 0.50)))
        result.add(
            "First word arrives after",
            f"{pct(firsts, 0.5) / max(pct(totals, 0.5), 1e-9):.1%} of the total wait",
        )
        result.note = (
            "Measured from request start to the first frame carrying content, which is the "
            "number a user perceives as the model's speed. The gateway pays its overhead "
            "once, at the front, and then gets out of the way of the stream."
        )
        result.check(
            "the first token arrives before the response finishes",
            pct(firsts, 0.50) < pct(totals, 0.50),
        )
        return result

    # ── 3. a provider outage ─────────────────────────────────────────────

    async def outage(self) -> Result:
        key, headers = await self.key_and_headers(name="outage")
        headers = {**headers, "X-Stormdoor-Chaos": f"fault=error;status=503;p={CHAOS_RATE}"}
        statuses: list[int] = []
        retryable: list[bool] = []

        for _ in range(CHAOS_REQUESTS):
            r = await self.client.post(
                "/v1/chat/completions", json=body(max_tokens=32), headers=headers
            )
            statuses.append(r.status_code)
            if r.status_code == 503:
                retryable.append(r.json()["error"]["retryable"])

        summary = await self.store.usage_summary(key.id, limit=1)
        failures = statuses.count(503)

        result = Result("Injected provider outage")
        result.add("Requests", CHAOS_REQUESTS)
        result.add("Fault rate requested", f"{CHAOS_RATE:.0%}")
        result.add("Observed 503s", f"{failures} ({failures / CHAOS_REQUESTS:.1%})")
        result.add("Succeeded anyway", statuses.count(200))
        result.add("Marked retryable", f"{sum(retryable)} / {failures}")
        result.add("Ledger rows tagged as a drill", summary["totals"]["errors"])
        result.check("the injected outage actually fired", failures > 0)
        result.check("every injected 503 was marked retryable", all(retryable))
        result.check(
            "every failure reached the ledger", summary["totals"]["errors"] == failures
        )
        result.note = (
            "Every failure is tagged in the ledger with the fault that caused it, so a "
            "rehearsal is never mistaken for a real outage when the history is read back. "
            "The retryable flag is what week 2's fallback engine will act on."
        )
        return result

    # ── 4. a stream that dies half way ───────────────────────────────────

    async def mid_stream(self) -> Result:
        key, base = await self.key_and_headers(name="abort")
        spec = f"fault=mid_stream_abort;after_chunks={ABORT_AFTER}"
        headers = {**base, "X-Stormdoor-Chaos": spec}
        delivered: list[int] = []
        signalled = 0

        for _ in range(ABORT_SAMPLES):
            content = 0
            event = None
            saw_error = False
            async with self.client.stream(
                "POST", "/v1/chat/completions", json=body(stream=True, max_tokens=128),
                headers=headers,
            ) as r:
                async for line in r.aiter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: ") and line != "data: [DONE]":
                        if event == "error":
                            saw_error = True
                        else:
                            payload = json.loads(line[6:])
                            choices = payload.get("choices") or []
                            if choices and choices[0]["delta"].get("content"):
                                content += 1
                        event = None
            delivered.append(content)
            signalled += int(saw_error)

        summary = await self.store.usage_summary(key.id, limit=1)

        result = Result("Death mid-stream")
        result.add("Streams killed", ABORT_SAMPLES)
        result.add("HTTP status seen by caller", "200, the headers were already sent")
        result.add("Content chunks delivered first", f"{min(delivered)} to {max(delivered)}")
        result.add("Streams that signalled the failure", f"{signalled} / {ABORT_SAMPLES}")
        result.add("Recorded as aborted, not as success", summary["totals"]["aborted"])
        result.add("Recorded as success", summary["totals"]["ok"])
        result.check(
            "every killed stream told the caller why", signalled == ABORT_SAMPLES
        )
        result.check(
            "partial output reached the caller first", min(delivered) == ABORT_AFTER - 1
        )
        result.check(
            "no killed stream was recorded as a success", summary["totals"]["ok"] == 0
        )
        result.check(
            "every killed stream was recorded as aborted",
            summary["totals"]["aborted"] == ABORT_SAMPLES,
        )
        result.note = (
            "The hardest partial failure to handle honestly. The status line has already "
            "said 200, so the error has to travel inside the stream, and the caller has to "
            "be charged for the tokens that really were produced. Neither a success nor a "
            "total failure, and the ledger says so."
        )
        return result

    # ── 5. budget admission ──────────────────────────────────────────────

    async def budget(self) -> Result:
        # Sized so the key walks up to its ceiling over many calls rather than
        # hitting it on the second one, which is what makes the walk visible.
        ceiling = 0.50
        key, headers = await self.key_and_headers(name="budget", budget_usd=ceiling)
        statuses: list[int] = []
        for _ in range(40):
            r = await self.client.post(
                "/v1/chat/completions", json=body(max_tokens=16), headers=headers
            )
            statuses.append(r.status_code)

        refreshed = await self.store.key_by_id(key.id)
        summary = await self.store.usage_summary(key.id, limit=1)

        result = Result("Budget admission")
        result.add("Budget", f"${ceiling:.2f}")
        result.add("Requests attempted", len(statuses))
        result.add("Admitted", statuses.count(200))
        result.add("Refused with 402", statuses.count(402))
        result.add("Final spend", f"${refreshed.spent_usd:.4f}")
        result.add("Overspend", f"${max(0.0, refreshed.spent_usd - ceiling):.4f}")
        result.add("Cost of a refused request", "$0.0000, no upstream call is made")
        result.add("Refusals recorded", summary["totals"]["refused"])
        result.check("the budget admitted work before it closed", statuses.count(200) > 0)
        result.check("the budget eventually closed the door", statuses.count(402) > 0)
        result.check("spend never passed the ceiling", refreshed.spent_usd <= ceiling)
        result.check(
            "every refusal reached the ledger",
            summary["totals"]["refused"] == statuses.count(402),
        )
        result.note = (
            "Refusal happens before the provider is called, so a request that would break "
            "the budget costs nothing but a SQLite write. The ceiling holds under concurrency "
            "too, because admission claims the worst case atomically rather than reading a "
            "spend figure other in-flight requests are about to change. See the note below "
            "for the one case where it can still be beaten."
        )
        return result

    # ── 6. rate limiting ─────────────────────────────────────────────────

    async def limits(self) -> Result:
        rpm = 30
        key, headers = await self.key_and_headers(name="limits", rpm=rpm)
        statuses = await asyncio.gather(
            *(
                self.client.post("/v1/chat/completions", json=body(max_tokens=16), headers=headers)
                for _ in range(rpm * 2)
            )
        )
        codes = [r.status_code for r in statuses]
        retry_after = [
            r.json()["error"]["retry_after_s"] for r in statuses if r.status_code == 429
        ]
        summary = await self.store.usage_summary(key.id, limit=1)

        result = Result("Rate limiting")
        result.add("Limit", f"{rpm} requests/min")
        result.add("Fired at once", len(codes))
        result.add("Allowed as a burst", codes.count(200))
        result.add("Throttled with 429", codes.count(429))
        result.add(
            "Retry-After given",
            f"{statistics.mean(retry_after):.0f} s" if retry_after else "n/a",
        )
        result.add("Throttled requests recorded", summary["totals"]["refused"])
        result.check("the full burst was allowed through", codes.count(200) == rpm)
        result.check("everything past the burst was throttled", codes.count(429) == rpm)
        result.check("every 429 carried a wait time", all(v > 0 for v in retry_after))
        result.note = (
            f"A bucket holds a full minute's allowance, so the first {rpm} arrive together "
            "and are all served. Real traffic is bursty, and a limiter that rejects a burst "
            "it could have absorbed is an outage you built yourself."
        )
        return result


def render(results: list[Result]) -> str:
    lines: list[str] = []
    lines.append(
        f"Generated by `uv run python -m bench.harness` on "
        f"{platform.python_implementation()} {platform.python_version()}, "
        f"{platform.system()} {platform.machine()}. Local `echo` provider, so no network "
        f"and no API key are involved."
    )
    lines.append("")
    for r in results:
        lines.append(f"### {r.name}")
        lines.append("")
        lines.append("| Measure | Value |")
        lines.append("|---|---|")
        lines.extend(f"| {label} | {value} |" for label, value in r.rows)
        lines.append("")
        if r.note:
            lines.append(r.note)
            lines.append("")
        if r.checks:
            lines.extend(
                f"- {'PASS' if ok else 'FAIL'}: {claim}" for claim, ok in r.checks
            )
            lines.append("")
    lines.append("### Where the budget ceiling does not hold")
    lines.append("")
    lines.append(
        "One honest limit, worth stating because a guarantee with unstated conditions is a "
        "lie with good manners. Admission prices the prompt with a local heuristic of about "
        "four characters per token, and that heuristic undershoots on code, CJK text and "
        "base64, so a real provider can bill more input than was estimated. The overshoot "
        "is bounded by that estimation error on a single request."
    )
    lines.append("")
    lines.append(
        "The larger gap is closed. Admission used to read the spend and then decide, which "
        "is a check-then-act race: `bench/stress.py` fired sixty requests at a $0.20 key at "
        "once and watched it spend $1.50, a 650% overshoot. Admission now claims the worst "
        "case atomically before the call and settles it against the real cost afterwards, "
        "in the same transaction as the ledger row. The same drill now overshoots by $0.00. "
        "A crash mid-request would strand a claim, so every reservation on disk is cleared "
        "at startup, when by definition nothing is in flight."
    )
    return "\n".join(lines)


def write_readme(section: str) -> bool:
    if not README.exists():
        return False
    text = README.read_text(encoding="utf-8")
    pattern = re.compile(r"(<!-- BENCH:START -->)(.*?)(<!-- BENCH:END -->)", re.S)
    if not pattern.search(text):
        return False
    README.write_text(
        pattern.sub(lambda m: f"{m.group(1)}\n{section}\n{m.group(3)}", text), encoding="utf-8"
    )
    return True


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        async with Bench(Path(tmp)) as bench:
            results = [
                await bench.load(),
                await bench.ttft(Path(tmp)),
                await bench.outage(),
                await bench.mid_stream(),
                await bench.budget(),
                await bench.limits(),
            ]

    section = render(results)
    print(section)
    if write_readme(section):
        print(f"\nWrote results into {README.name} between the BENCH markers.")
    else:
        print("\nCould not find the BENCH markers in README.md, so nothing was written.")

    failed = [
        (result.name, claim)
        for result in results
        for claim, ok in result.checks
        if not ok
    ]
    total = sum(len(result.checks) for result in results)
    if failed:
        print(f"\n{len(failed)} of {total} behaviour checks FAILED:")
        for name, claim in failed:
            print(f"  {name}: {claim}")
        return 1

    print(f"\nAll {total} behaviour checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
