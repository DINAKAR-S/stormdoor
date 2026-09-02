# Connecting stormdoor to a provider

stormdoor holds your real provider key in **one** place: its own configuration,
read from environment variables (or a `.env` file next to the gateway). Your apps
never see it. They carry a stormdoor virtual key (`sd-...`); stormdoor uses the
real key to make the upstream call.

This page has copy-paste `.env` examples. Every variable is prefixed
`STORMDOOR_`. The full list of settings is the Configuration table in the
[README](../README.md#configuration).

## How stormdoor picks a provider

By the **model name** in the request:

| Model name starts with | Goes to | Needs the extra |
|---|---|---|
| `gpt-`, `o1`, `o3`, `o4`, `chatgpt-` | OpenAI | `stormdoor[openai]` |
| `claude-` | Anthropic | `stormdoor[anthropic]` |
| `echo-small`, `echo-large` | the built-in local test model | nothing |
| anything, prefixed `openai/...` | the OpenAI adapter at your `OPENAI_BASE_URL` | `stormdoor[openai]` |

A provider is only available if its SDK is installed. Install the ones you need:

```bash
uv pip install 'stormdoor[openai]'      # OpenAI and any OpenAI-compatible server
uv pip install 'stormdoor[anthropic]'   # Claude
uv pip install 'stormdoor[all]'         # everything
```

`echo-small` needs none of this and works offline, which is why the quickstart
uses it.

## OpenAI

```bash
# .env
STORMDOOR_OPENAI_API_KEY=sk-proj-your-real-openai-key
# STORMDOOR_OPENAI_BASE_URL defaults to OpenAI itself, so you can leave it unset
```

Then ask for an OpenAI model:

```bash
curl localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sd-your-virtual-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}]}'
```

## Anthropic (Claude)

```bash
# .env
STORMDOOR_ANTHROPIC_API_KEY=sk-ant-your-real-anthropic-key
```

```bash
curl localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sd-your-virtual-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"hello"}]}'
```

## A local or self-hosted model (vLLM, Ollama, LM Studio, Groq, Together, OpenRouter)

Any server that speaks the OpenAI API works through the OpenAI adapter. Point the
base URL at it, and reach its models with the `openai/` prefix so stormdoor sends
them there instead of guessing from the name.

### vLLM

```bash
# .env
STORMDOOR_OPENAI_BASE_URL=http://localhost:8000/v1
STORMDOOR_OPENAI_API_KEY=not-needed-but-the-sdk-wants-a-value
```

```bash
curl localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sd-your-virtual-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/meta-llama/Llama-3.1-70B-Instruct","messages":[{"role":"user","content":"hello"}]}'
```

### Ollama

```bash
# .env
STORMDOOR_OPENAI_BASE_URL=http://localhost:11434/v1
STORMDOOR_OPENAI_API_KEY=ollama
```

```bash
curl localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sd-your-virtual-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/llama3.1","messages":[{"role":"user","content":"hello"}]}'
```

### Groq, Together, OpenRouter

Same shape: set `STORMDOOR_OPENAI_BASE_URL` to their OpenAI-compatible endpoint
and `STORMDOOR_OPENAI_API_KEY` to that service's key, then call
`openai/<their-model-id>`.

## Several providers at once, with failover

Set the keys for every provider you want, and they are all available together.
Give a route a chain of targets and stormdoor will fail over between them. Put the
route file path in `STORMDOOR_ROUTES_FILE`:

```bash
# .env
STORMDOOR_OPENAI_API_KEY=sk-proj-...
STORMDOOR_ANTHROPIC_API_KEY=sk-ant-...
STORMDOOR_ROUTES_FILE=./routes.json
```

```json
// routes.json: a model name that tries OpenAI first, then falls back to Claude
{
  "support-chat": {
    "targets": [
      {"model": "gpt-4o-mini"},
      {"model": "claude-haiku-4-5"}
    ]
  }
}
```

Now a request for `support-chat` tries `gpt-4o-mini`, and if OpenAI is failing in a
retryable way it moves on to `claude-haiku-4-5`, before the first token reaches the
caller. See [When a provider goes down](../README.md#when-a-provider-goes-down).

## Where the key must never go

- Not in the dashboard. The dashboard's "New key" button mints a virtual `sd-...`
  key for an app; it does not take a provider key.
- Not in your app. Your app only ever holds an `sd-...` key.
- Not in a URL or a request body. Only in the server's environment.

If you want a vault that stores real keys and lets a process use them without ever
reading them back, that is a separate tool from stormdoor and the two compose:
stormdoor's own provider key can live in such a vault.

## Two safety notes

- **Pricing.** stormdoor only bills against a rate it can source. If you add a
  model it has no rate for, requests are recorded at `$0.00` and flagged, not
  guessed. Set `STORMDOOR_PRICING_FILE` to a JSON rate card to price your models.
  See `src/stormdoor/pricing.py`.
- **The key is read once, at startup.** Change a key and restart the gateway for
  it to take effect.
