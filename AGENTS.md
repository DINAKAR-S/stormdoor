# AGENTS.md

A short, dense guide for an AI agent (or a person in a hurry) pointed at this
repo. Read this instead of the whole README when you need to answer "what is
this, what does it do, and how do I set it up." Deeper detail is in `README.md`,
`docs/`, and the docstring at the top of each file in `src/stormdoor/`.

## What stormdoor is, in one line

An OpenAI-compatible LLM gateway you put in front of your models: it enforces
per-app budgets and rate limits, fails over between providers, can inject faults
on purpose to rehearse outages, caches near-repeat prompts, redacts PII, traces
requests, and meters usage for billing.

## The mental model you must get right first

stormdoor is **not** a place to store your OpenAI/Anthropic key, and it is **not**
a general secrets vault. There are two kinds of key and people confuse them:

- **Your real provider key** (an OpenAI or Anthropic key). It goes in **one**
  place: stormdoor's server config (`STORMDOOR_OPENAI_API_KEY` /
  `STORMDOOR_ANTHROPIC_API_KEY`). Your apps never see it.
- **A stormdoor virtual key** (`sd-...`). stormdoor issues these. Each one has a
  budget, rate limits, an allow-list and an expiry. You give a virtual key to an
  app, and the app sends it to stormdoor. If it leaks, you disable that one key;
  the real provider key never moves.

So in the dashboard, the **"New key"** button creates a **virtual key** (`sd-...`).
That is the "add a key" UI. It does not store your OpenAI key.

If you want a proper vault for real API keys that an agent can *use but never
read*, that is a different tool (e.g. keywarden). stormdoor and a secrets vault
solve different problems and can be used together: stormdoor's own provider key
can live in the vault.

## Input, output, usage (the request flow)

- **Input:** your app sends an OpenAI-style `POST /v1/chat/completions` to
  stormdoor, authenticated with a virtual key: `Authorization: Bearer sd-...`.
- **What stormdoor does:** authenticate the key, apply its rate limits, reserve
  against its budget, run any guardrail hooks, check the cache, then call the real
  provider (using the real key it holds), and write one ledger row.
- **Output:** the model's answer in the normal OpenAI response shape, plus a
  namespaced `stormdoor` block (cost, latency, cache hit, failover trail). Every
  request, including refusals and errors, is recorded and visible in the
  dashboard and the ledger.

The point: your app code barely changes (two lines: `base_url` and `api_key`),
and in exchange you get spend control, reliability, and a full audit trail.

## Quickstart (works on a clean clone, no Docker, no API key)

```bash
git clone https://github.com/DINAKAR-S/stormdoor && cd stormdoor
uv venv && uv pip install -e ".[dev]"
uv run stormdoor keys create demo --budget 5 --rpm 60   # prints a virtual key sd-...
uv run stormdoor serve --chaos                          # dashboard at http://localhost:8080
uv run stormdoor admin-token                            # the token to sign in to the dashboard
```

`echo-small` is a built-in local model that needs no key and costs nothing, so
every feature can be tried offline.

## How to point your own app at stormdoor

Change two lines. Any OpenAI SDK works because the wire format is OpenAI's.

```python
from openai import OpenAI
client = OpenAI(
    base_url="http://localhost:8080/v1",   # was https://api.openai.com/v1
    api_key="sd-...",                      # a stormdoor virtual key, not your OpenAI key
)
client.chat.completions.create(model="echo-small",
    messages=[{"role": "user", "content": "hello"}])
```

For a real provider, set the provider key on the stormdoor server once
(`STORMDOOR_OPENAI_API_KEY=sk-...`) and ask for that provider's model (e.g.
`gpt-4o-mini`). The app still only ever holds the `sd-...` key. Copy-paste `.env`
examples for OpenAI, Claude and a local vLLM or Ollama are in
[`docs/providers.md`](docs/providers.md).

## Features, each in one line

- **Virtual keys** with per-key budget, request/token rate limits, model
  allow-list and expiry. Only a hash is stored.
- **Budgets enforced before the call**, pricing the worst case; refused with a
  402 that shows its arithmetic, so the request that breaks the budget never runs.
- **Token-bucket rate limits** per key (in-process, or Redis for many replicas).
- **Fault injection** as a product feature: a header makes a provider return a
  chosen status, hang, run slow, or die mid-stream, so you can rehearse outages.
- **Routing, retries, circuit breakers, failover** across a chain of targets,
  with complexity-based tier routing (cheap model first, escalate when earned).
- **Semantic cache** (off by default): near-repeat prompts served free. SQLite +
  local embedder by default; Pinecone + OpenAI embeddings opt-in.
- **Guardrail hooks** (off by default): PII redaction and prompt-injection
  flag/block, composed as an ordered chain.
- **OpenTelemetry tracing** (off by default): one span per request, metadata only
  unless content is explicitly opted in.
- **Usage metering**: roll up per key or per tenant, export via
  `GET /admin/usage/export`, optionally push to Stripe billing meters.
- **Stream resume**: a dropped SSE stream resumes from its last event id.
- **Dashboard** at `/`: one HTML file, no build step, no external request. Shows
  counters, keys with budget bars, spend by day, a cost report you can filter by
  date/provider and break down by key/provider/model, a live ledger, provider
  health, and a panel that breaks the gateway on purpose.

## Turning features on

Everything optional is off by default and enabled with a `STORMDOOR_` env var.
The full list with defaults is the Configuration table in `README.md`. The common
ones: `STORMDOOR_CACHE_ENABLED=true`, `STORMDOOR_GUARDRAIL_HOOKS=pii_redact`,
`STORMDOOR_OTEL_ENABLED=true`, `STORMDOOR_RESUME_ENABLED=true`.

## API surface (all under the running server)

- `POST /v1/chat/completions` (key auth) - the gateway, streaming or not.
- `GET /v1/models` (key) - what it can route.
- `GET /v1/stream/{request_id}` (key) - resume a dropped stream.
- `GET /` - the dashboard (admin token, entered in the page).
- Admin plane (header `X-Stormdoor-Admin`): `/admin/keys` (create/list/disable),
  `/admin/keys/{id}/usage`, `/admin/ledger`, `/admin/stats`, `/admin/spend`,
  `/admin/usage/export`, `/admin/usage/push`, `/admin/health`,
  `/admin/breaker/reset`, `/admin/cache`, `/admin/drill`.

## Honest limits

Stated plainly in the README's "What is not built yet" table: the local cache
embedder matches wording not meaning and can rarely collide; guardrail injection
detection is a heuristic tripwire, not a boundary; the stream buffer and the
SQLite cache are per process, not shared across replicas. Read that table before
claiming a guarantee.

## Where things live (to navigate the code)

- `src/stormdoor/gateway.py` - the request path: admission, cache, hooks, the
  provider call with retries and failover, the ledger write, one trace span.
- `src/stormdoor/store.py` - all SQL: keys, the append-only ledger, atomic budget
  reservations, the cache table, usage export.
- `src/stormdoor/app.py` - the HTTP surface and the admin plane.
- `src/stormdoor/{routing,breaker,attempts}.py` - routes, circuit breakers, backoff.
- `src/stormdoor/{cache,embeddings,vectorstore}.py` - the semantic cache.
- `src/stormdoor/{hooks,tracing,metering,resume}.py` - guardrails, tracing,
  metering, stream resume.
- `src/stormdoor/static/dashboard.html` - the whole dashboard, one file.
- `bench/harness.py` - regenerates every number the README claims.
- `bench/stress.py` - the adversarial pre-publish checks.
- `docs/lessons.md` - defects found while building, and how each was caught.

## Verify it works

```bash
uv run pytest -q            # the test suite
uv run python -m bench.harness   # regenerates the README's numbers
uv run python -m bench.stress    # the adversarial checks
```
