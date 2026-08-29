# stormdoor

**An LLM gateway that proves itself under failure.**

A storm door is the outer door that takes the weather so the real one does not.
This is that, in front of your models: one OpenAI-compatible endpoint, virtual
keys, hard budgets, rate limits, and a usage ledger you can bill from.

There are good gateways already. What none of them ship is a way to make the
provider fail on purpose, against your own traffic, and watch what your gateway
actually does. That is the part of a gateway that matters, and it is the part
nobody tests, because provoking a real 503 or a real mid-stream disconnect is
inconvenient. So stormdoor injects them for you, and every reliability claim in
this README is produced by a harness in `bench/` that you can rerun.

> **Status: week 1 of a public build.** The front door is done: providers,
> streaming, virtual keys, budgets, rate limits, fault injection, the ledger.
> Routing, failover and the semantic cache are next. See [Roadmap](#roadmap).

---

## Run it in sixty seconds

No Docker. No Postgres. No Redis. No API key.

```bash
git clone https://github.com/DINAKAR-S/stormdoor && cd stormdoor
uv venv && uv pip install -e ".[dev]"
```

Mint a key and start the gateway with fault injection turned on:

```bash
uv run stormdoor keys create demo --budget 5 --rpm 60 --tpm 20000
```

```bash
uv run stormdoor serve --chaos
```

Open [localhost:8080](http://localhost:8080) and the dashboard asks for the
admin token the gateway printed to its log. From there you can see every key
and what it has spent, watch the ledger fill up live, and break the gateway on
purpose from a dropdown.

Or talk to it like any OpenAI endpoint. `echo-small` is a local, deterministic
model that never leaves the process, so everything below is free:

```bash
curl localhost:8080/v1/chat/completions -H "Authorization: Bearer sd-..." -H "Content-Type: application/json" -d '{"model":"echo-small","messages":[{"role":"user","content":"hello"}]}'
```

Now break it on purpose:

```bash
curl localhost:8080/v1/chat/completions -H "Authorization: Bearer sd-..." -H "X-Stormdoor-Chaos: fault=mid_stream_abort;after_chunks=5" -H "Content-Type: application/json" -d '{"model":"echo-small","stream":true,"messages":[{"role":"user","content":"hello"}]}'
```

Five chunks arrive, then an SSE `error` event. Read the ledger and the request
is recorded as `aborted`, not as a success and not as a total failure, with the
tokens that were actually produced charged to the key:

```bash
uv run stormdoor keys usage <key_id>
```

---

## Why this exists

A gateway is bought for what it does on the bad day. Yet the normal way to gain
confidence in one is to read its feature list. Fault injection here is a
first-class part of the product rather than a test fixture, which means:

- You can rehearse a provider outage against **your** traffic, on a Tuesday.
- Retry, failover and timeout behaviour can be asserted in CI rather than
  discovered in production.
- A fault takes a `seed`, which fixes whether that request fails, so a drill
  becomes a regression test instead of a flaky one. Leave the seed off when you
  want a fault rate spread across many requests rather than one fixed outcome.

Faults are refused unless `STORMDOOR_CHAOS_ENABLED=true`. While it is off, the
header is not even parsed, so a caller cannot induce failures in a deployment
you did not arm.

| Fault | What it does |
|---|---|
| `error` | Fails before the upstream call, with a status code you choose |
| `timeout` | Hangs past the request timeout |
| `slow` | Delays, then proceeds, for time-to-first-token work |
| `mid_stream_abort` | Streams N chunks, then dies mid-response |

```
X-Stormdoor-Chaos: fault=error;status=503;p=1.0
X-Stormdoor-Chaos: fault=mid_stream_abort;after_chunks=5
X-Stormdoor-Chaos: fault=slow;delay_ms=800;p=0.25;seed=7
```

Every injected fault is tagged in the ledger, so a drill is never confused with
a real outage when you read the history back.

---

## What week 1 ships

**One endpoint, many providers.** `POST /v1/chat/completions`, streaming and
non-streaming, in front of Anthropic, OpenAI (or anything OpenAI-compatible:
vLLM, Ollama, Groq, Together, OpenRouter), and a local `echo` provider. Model
ids route by prefix, so a model released after this code was written still
works instead of 404-ing against a hardcoded list.

**Virtual keys.** Issued by the gateway, never the provider's key. Only a
SHA-256 hash is stored. Each key carries its own budget, request and token
rate limits, model allow-list, and expiry.

**Budgets enforced before the call, not after it.** Most gateways total up
spend retrospectively, which means the request that broke the budget already
cost you money. stormdoor prices the worst case a request could cost, using the
prompt length and `max_tokens`, and refuses it with a 402 that shows its
working:

```json
{"error": {"message": "this request could cost up to $0.1024, which would take key 'demo' past its $5.00 budget ($4.9891 already spent)",
           "type": "insufficient_quota", "code": "budget_exceeded",
           "budget": {"spent_usd": 4.9891, "budget_usd": 5.0, "estimated_cost_usd": 0.1024}}}
```

**Token-bucket rate limits.** Per key, on requests and on tokens, with bursts.
In-process by default; Redis when you have more than one replica, with the
refill and the take inside one Lua script so two replicas cannot both spend the
last token.

**An append-only ledger.** One row per request including refusals, errors and
half-finished streams, with tokens, cost, latency, time-to-first-token, and any
injected fault. Rows are never updated or deleted. The running spend on a key
moves in the same transaction as the row that caused it.

**Streaming with event ids.** Every SSE frame carries an `id:`. Nothing reads
it yet. It is the anchor `Last-Event-ID` resume needs in week 4, and adding it
now costs one line where retrofitting it later would change the wire format
under existing clients.

**A dashboard, in one file.** Served at `/`, no build step, no framework, and
no external request of any kind, which a test enforces. It shows the keys and
their budget bars, gateway-wide counters, a live ledger with the outcome of
every request, a latency strip coloured by what happened, and a panel that
fires one real request through the real path with a fault of your choosing. It
talks to the same admin API the CLI does, so there is nothing in it you could
not do with curl.

---

## Two rules this codebase holds to

**A rate that is not sourced is not shipped.** Every entry in the rate card in
`src/stormdoor/pricing.py` carries where it came from and the date it was
checked. A model with no verified rate is recorded at `$0.00` with
`pricing_known = false` and logged as a warning, never quietly estimated. A
guessed rate produces an invoice that is wrong in a way nobody notices until
the customer notices. Override the whole table with `STORMDOOR_PRICING_FILE`.

**Estimates gate, they never bill.** Admission uses a local heuristic of about
four characters per token, which is fine for deciding whether to make a call
and useless for charging for one. Cost always comes from the token counts the
provider returns.

---

## Configuration

Everything takes a `STORMDOOR_` prefix, and `.env` is read if present.

| Variable | Default | What it does |
|---|---|---|
| `STORMDOOR_HOST` / `STORMDOOR_PORT` | `127.0.0.1` / `8080` | Bind address |
| `STORMDOOR_DB_PATH` | `./stormdoor.db` | Keys and ledger |
| `STORMDOOR_ADMIN_TOKEN` | generated, logged once | Guards `/admin/*` |
| `STORMDOOR_LIMITER_BACKEND` | `memory` | `memory` or `redis` |
| `STORMDOOR_REDIS_URL` | unset | Required for the redis limiter |
| `STORMDOOR_CHAOS_ENABLED` | `false` | Arms fault injection |
| `STORMDOOR_CHAOS_DEFAULT` | unset | A spec applied to every request |
| `STORMDOOR_DEFAULT_MAX_TOKENS` | `4096` | Sent when the caller omits it |
| `STORMDOOR_PRICING_FILE` | unset | JSON rate card override |
| `STORMDOOR_ANTHROPIC_API_KEY` | unset | Falls back to the SDK's own resolution |
| `STORMDOOR_OPENAI_API_KEY` / `..._BASE_URL` | unset | Also serves OpenAI-compatible servers |

### A note on `default_max_tokens`

Worst-case admission prices `max_tokens`, so the default the gateway sends on
behalf of a caller who omitted it directly determines how strict budgets feel.
At 64k, a single request against Claude Opus prices at over a dollar and a
small budget refuses everything. 4096 keeps admission useful. Callers who want
more room say so in the request.

---

## API

| Route | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | `Authorization: Bearer sd-...` | Chat, streaming or not |
| `GET /v1/models` | key | What this gateway can route |
| `GET /healthz` | none | Providers, limiter, whether chaos is armed |
| `GET /dashboard` | token entered in the page | The dashboard. `/` redirects here |
| `POST /admin/keys` | `X-Stormdoor-Admin` | Issue a key, secret returned once |
| `GET /admin/keys` | admin | List keys with spend |
| `GET /admin/keys/{id}/usage` | admin | Totals and recent requests |
| `POST /admin/keys/{id}/disable` | admin | Kill a key immediately |
| `GET /admin/ledger` | admin | Recent requests across every key |
| `GET /admin/stats` | admin | Gateway-wide counters |
| `POST /admin/drill` | admin | Fire one request, optionally with a fault |

Errors keep the OpenAI envelope (`{"error": {"message", "type", "code"}}`) so
an existing SDK's error handling still works, with the extra fields that
explain the decision: `retry_after_s` and `limit` on a 429, the budget
arithmetic on a 402, `retryable` and `provider` on an upstream failure.

---

## Proof

Numbers, not adjectives. Regenerate all of it on your own machine:

```bash
uv run python -m bench.harness
```

Six drills against the local `echo` provider, so no API key, no network and no
cost. The harness rewrites the section below with what it measured, and it is
also a gate: timings vary by machine and are never asserted, but the behaviour
behind them is. No overspend, every injected failure classified honestly, every
killed stream accounted for. If one of those stops holding, the harness exits
non-zero and CI goes red.

<!-- BENCH:START -->
Generated by `uv run python -m bench.harness` on CPython 3.11.0, Windows AMD64. Local `echo` provider, so no network and no API key are involved.

### Throughput and latency

| Measure | Value |
|---|---|
| Requests | 600 |
| Concurrency | 32 |
| Successful | 600 / 600 |
| Throughput | 184 req/s |
| Latency p50 | 91.1 ms |
| Latency p95 | 262.5 ms |
| Latency p99 | 1371.8 ms |
| Transport | in-process ASGI |

Full gateway path per request: auth, model check, both rate-limit buckets, budget admission, provider call, ledger write. The provider is local and the transport is in-process, so this isolates the gateway's own overhead from both the model's speed and the socket. Add your network and your model on top.

- PASS: every request under load succeeded

### Streaming, time to first token

| Measure | Value |
|---|---|
| Streams | 120 |
| Transport | real uvicorn server over TCP |
| Simulated per-chunk delay | 4 ms |
| TTFT p50 | 9.6 ms |
| TTFT p95 | 12.4 ms |
| Full response p50 | 24.8 ms |
| First word arrives after | 38.5% of the total wait |

Measured from request start to the first frame carrying content, which is the number a user perceives as the model's speed. The gateway pays its overhead once, at the front, and then gets out of the way of the stream.

- PASS: the first token arrives before the response finishes

### Injected provider outage

| Measure | Value |
|---|---|
| Requests | 400 |
| Fault rate requested | 25% |
| Observed 503s | 83 (20.8%) |
| Succeeded anyway | 317 |
| Marked retryable | 83 / 83 |
| Ledger rows tagged as a drill | 83 |

Every failure is tagged in the ledger with the fault that caused it, so a rehearsal is never mistaken for a real outage when the history is read back. The retryable flag is what week 2's fallback engine will act on.

- PASS: the injected outage actually fired
- PASS: every injected 503 was marked retryable
- PASS: every failure reached the ledger

### Death mid-stream

| Measure | Value |
|---|---|
| Streams killed | 60 |
| HTTP status seen by caller | 200, the headers were already sent |
| Content chunks delivered first | 3 to 3 |
| Streams that signalled the failure | 60 / 60 |
| Recorded as aborted, not as success | 60 |
| Recorded as success | 0 |

The hardest partial failure to handle honestly. The status line has already said 200, so the error has to travel inside the stream, and the caller has to be charged for the tokens that really were produced. Neither a success nor a total failure, and the ledger says so.

- PASS: every killed stream told the caller why
- PASS: partial output reached the caller first
- PASS: no killed stream was recorded as a success
- PASS: every killed stream was recorded as aborted

### Budget admission

| Measure | Value |
|---|---|
| Budget | $0.50 |
| Requests attempted | 40 |
| Admitted | 18 |
| Refused with 402 | 22 |
| Final spend | $0.4860 |
| Overspend | $0.0000 |
| Cost of a refused request | $0.0000, no upstream call is made |
| Refusals recorded | 22 |

Refusal happens before the provider is called, so a request that would break the budget costs nothing but a SQLite write. Sequentially, against a provider whose token count the local estimate matches exactly, the ceiling holds. See the overshoot note below for where it does not.

- PASS: the budget admitted work before it closed
- PASS: the budget eventually closed the door
- PASS: spend never passed the ceiling
- PASS: every refusal reached the ledger

### Rate limiting

| Measure | Value |
|---|---|
| Limit | 30 requests/min |
| Fired at once | 60 |
| Allowed as a burst | 30 |
| Throttled with 429 | 30 |
| Retry-After given | 2 s |
| Throttled requests recorded | 30 |

A bucket holds a full minute's allowance, so the first 30 arrive together and are all served. Real traffic is bursty, and a limiter that rejects a burst it could have absorbed is an outage you built yourself.

- PASS: the full burst was allowed through
- PASS: everything past the burst was throttled
- PASS: every 429 carried a wait time

### Where the budget ceiling does not hold

Two honest limits, both worth stating because a guarantee with unstated conditions is a lie with good manners. First, admission prices the prompt with a local heuristic of about four characters per token, and that heuristic undershoots on code, CJK text and base64, so a real provider can bill more input than was estimated. Second, admission reads the spend recorded so far, so N requests in flight at once can each be admitted against the same remaining budget. The overshoot is bounded by the estimation error plus the worst-case cost of the in-flight requests. Reserving budget at admission and settling it afterwards closes the second gap, and is week 4's work.
<!-- BENCH:END -->

---

## Roadmap

stormdoor is repo one of four that together cover the fifteen backend systems
an AI product needs. This one is the inference plane.

| Week | Ships |
|---|---|
| 1 | Providers, streaming, virtual keys, budgets, rate limits, chaos, ledger |
| 2 | Health checks, circuit breaker, complexity-based routing, failover with retries |
| 3 | Semantic cache on pgvector, PII redaction and injection heuristics as hooks |
| 4 | OpenTelemetry tracing, Stripe metering, SSE resume across a mid-stream failover |

---

## Development

```bash
uv run pytest
```

```bash
uv run ruff check src tests bench
```

The gateway core in `src/stormdoor/gateway.py` has no idea it is behind HTTP,
so tests and the bench harness drive it directly. The `echo` provider is
deterministic, which is what lets the tests assert exact token counts instead
of ranges.

## Licence

MIT. See [LICENSE](LICENSE).
