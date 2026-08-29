# Self-hosting stormdoor on a VPS, and wiring n8n into it

This is the exact setup for the problem stormdoor was built for: **one provider
key, many projects, each project's spend tracked and capped separately.**

## The problem, concretely

Right now each of your projects holds its own provider key. Meetings AI has one
OpenAI key, another project has another, and the only way you know what each one
cost is the provider's own dashboard, after the fact, with no per-project cap.

You want the opposite. One real OpenAI key, held in one place. Each project gets
its own **virtual key** with its own budget and its own rate limit. Every call
is written to one ledger you own, so "what did Meetings AI spend this month" is
one query, and a runaway project hits its own ceiling instead of your card.

```
                         ┌──────────────── your VPS ────────────────┐
  Meetings AI  ─ sd-aaa ─┤                                          │
  Project two  ─ sd-bbb ─┤   stormdoor    ── one real OpenAI key ──▶│──▶ OpenAI
  n8n workflow ─ sd-ccc ─┤   one ledger                             │
  Project four ─ sd-ddd ─┤   per-key budgets                        │
  Project five ─ sd-eee ─┤                                          │
                         └──────────────────────────────────────────┘
```

The real OpenAI key lives in stormdoor and nowhere else. Your projects never
hold it. If a virtual key leaks, you disable that one key; the provider key is
untouched.

---

## 1. Put it on the VPS

You need Docker and the Compose plugin. Then:

```bash
git clone https://github.com/DINAKAR-S/stormdoor && cd stormdoor
```

Create a `.env` next to `docker-compose.yml`. This file holds your real
credentials, so it must never be committed (the repo's `.gitignore` already
excludes it):

```bash
STORMDOOR_ADMIN_TOKEN=$(openssl rand -hex 16)
STORMDOOR_OPENAI_API_KEY=sk-...your real OpenAI key...
# STORMDOOR_ANTHROPIC_API_KEY=sk-ant-...  # only if you also route Claude
```

Write those into `.env` and bring it up:

```bash
docker compose up -d --build
```

Check it is alive:

```bash
curl localhost:8080/healthz
```

At this point the gateway is running but bound to `127.0.0.1` only. That is
deliberate: nothing is exposed to the internet yet.

---

## 2. Give each project its own key

From the VPS, mint one virtual key per project. Each gets a budget and a rate
limit that belong to that project alone:

```bash
docker compose exec stormdoor \
  stormdoor keys create "meetings-ai" --budget 30 --rpm 120

docker compose exec stormdoor \
  stormdoor keys create "n8n-workflows" --budget 10 --rpm 60
```

Each command prints a secret **once**. That is the only time it exists in
plaintext, so copy it immediately. What stormdoor stores is a SHA-256 hash; a
lost secret cannot be recovered, only replaced.

Now point each project at the gateway instead of at OpenAI. In your app's OpenAI
client, change two things and nothing else:

- **base URL** → `https://your-vps-domain/v1`
- **API key** → the `sd-...` secret for that project, not your OpenAI key

Because stormdoor speaks the OpenAI API, no other code changes. The project
keeps calling `chat.completions.create` exactly as before.

### What to put in the model field

The same thing you put there today. `gpt-4o-mini` stays `gpt-4o-mini`. There is
no prefix to add and no renaming to do, because model ids route by prefix:
`gpt-`, `o1`, `o3`, `o4` and `chatgpt-` go to OpenAI, `claude-` goes to
Anthropic, `echo-` is the local test model.

If you would rather be explicit, or you are pointing
`STORMDOOR_OPENAI_BASE_URL` at a local vLLM or Ollama server whose model is
named something the prefix rules would never guess, write
`<provider>/<model>` instead:

```json
{ "model": "openai/gpt-4o-mini" }
{ "model": "anthropic/claude-opus-5" }
{ "model": "openai/llama-3.1-70b" }
```

Both spellings work, the prefix is stripped before the call, and the README has
the full table under "Naming a model".

---

## 3. Wire n8n into it

You said you have n8n on the same box and can make HTTP credentials in it. Two
ways in, depending on which node you use.

### If you use the HTTP Request node (works today, recommended)

1. In n8n, go to **Credentials → New → Header Auth** (a generic credential type
   the HTTP Request node supports).
2. Set:
   - **Name**: `Authorization`
   - **Value**: `Bearer sd-...` (the virtual key you minted for n8n)
3. In the HTTP Request node:
   - **Method**: `POST`
   - **URL**: `http://stormdoor:8080/v1/chat/completions` if n8n is in the same
     Compose project, or `http://127.0.0.1:8080/v1/chat/completions` if n8n runs
     directly on the host, or your public `https://…/v1/…` URL otherwise
   - **Authentication**: Generic Credential Type → the Header Auth credential above
   - **Body** (JSON):
     ```json
     {
       "model": "gpt-4o-mini",
       "messages": [{ "role": "user", "content": "{{ $json.prompt }}" }]
     }
     ```

The response is the normal OpenAI shape, plus a `stormdoor` block telling you
the request id and what that call cost. n8n's spend now shows up in the ledger
under the `n8n-workflows` key like any other project.

> **Networking note.** If both n8n and stormdoor run under the same
> `docker compose`, they share a network and reach each other by service name,
> so `http://stormdoor:8080` works. If n8n is a separate container, either put
> them on the same Docker network or use the host's address. Do not send
> internal traffic out over the public URL and back; it is slower and needlessly
> exposed.

### If you use n8n's built-in OpenAI node

n8n's OpenAI credential has an **API Key** field and, on current versions, a
**Base URL** field. If your version shows Base URL:

- **Base URL**: `http://stormdoor:8080/v1` (or your public `https://…/v1`)
- **API Key**: the `sd-...` virtual key

If your version's OpenAI credential does **not** show a Base URL field, you
cannot point the built-in node at stormdoor, and the HTTP Request route above is
the way. I have not asserted the field exists on your specific build because it
has moved between versions; look at the credential screen and use whichever of
the two routes matches what you see.

---

## 4. Lock it down before it faces the internet

The compose file binds the gateway to `127.0.0.1` on purpose. `/admin` and
`/dashboard` must never be publicly reachable, because the admin token is the
only thing standing between the internet and your keys and budgets.

Put a reverse proxy in front that exposes **only** the caller-facing routes and
terminates TLS. With Caddy:

```caddyfile
# Caddyfile: exposes only /v1 and /healthz to the world.
llm.your-domain.com {
    @public path /v1/* /healthz
    handle @public {
        reverse_proxy 127.0.0.1:8080
    }
    # Everything else, /admin and /dashboard included, is not served publicly.
    handle {
        respond "not found" 404
    }
}
```

Reach the dashboard yourself over an SSH tunnel instead of exposing it:

```bash
ssh -L 8080:127.0.0.1:8080 you@your-vps
# then open http://localhost:8080/dashboard on your laptop
```

### Where the dashboard token comes from

The sign-in token is whatever you put in `STORMDOOR_ADMIN_TOKEN`. If you leave
that unset, the gateway generates one on first start and stores it next to the
keys, so it survives restarts. Either way, ask it:

```bash
docker compose exec stormdoor stormdoor admin-token
```

Add `--reset` to replace a stored token, which is what to do if it ever leaks.
Setting `STORMDOOR_ADMIN_TOKEN` overrides the stored one, and for a real
deployment that is the better habit, because then the token lives in your `.env`
with the rest of your secrets rather than only inside the database.

Checklist before you call it done:

- [ ] `.env` is not in git and is readable only by you (`chmod 600 .env`)
- [ ] `STORMDOOR_ADMIN_TOKEN` is a random 32-char value, not a word you chose
- [ ] the proxy exposes `/v1` and `/healthz` and nothing else
- [ ] TLS is on, so virtual keys never cross the network in plaintext
- [ ] `STORMDOOR_CHAOS_ENABLED` is `false` in production (it defaults to false)
- [ ] each project has its own key with its own budget

---

## 5. Read your spend

Per project, from the VPS:

```bash
docker compose exec stormdoor stormdoor keys list
docker compose exec stormdoor stormdoor keys usage <key_id>
```

Or open the dashboard over the tunnel from step 4 and watch it live: every key's
budget bar, the running total, and a ledger row for every call including the
ones a budget turned away.

---

## A note on pricing accuracy

stormdoor only knows the cost of a call if it has a verified rate for that
model. **OpenAI rates are deliberately not shipped in the built-in table**,
because a guessed rate produces a wrong invoice. Before you rely on the cost
figures, add the models you use to a pricing file and point
`STORMDOOR_PRICING_FILE` at it:

```json
{
  "gpt-4o-mini": {
    "input_per_mtok": 0.15,
    "output_per_mtok": 0.60,
    "source": "https://openai.com/api/pricing/",
    "checked_on": "2026-08-29"
  }
}
```

Use the real numbers from OpenAI's pricing page on the day you look, and put
that date in `checked_on`. Until a model is priced, its calls are recorded at
`$0.00` and flagged in the ledger rather than guessed at, so you will see them,
you just will not see a cost until you supply the rate.

---

## The trade-off, stated plainly

Consolidating five projects behind one gateway concentrates a risk you did not
have before. Today, if one project's key or provider hiccups, the other four
keep working. After this change, if the VPS goes down, all five stop.

That is a real cost and it is worth taking with your eyes open:

- **Keep the old path documented.** Leave each project able to fall back to a
  direct provider key via an environment variable you can flip, until routing
  and failover land in the gateway itself.
- **Back up the ledger.** It is one SQLite file and it holds your billing
  history, which is the only state here that cannot be recomputed:
  ```bash
  docker compose exec stormdoor sqlite3 /data/stormdoor.db ".backup '/data/backup.db'"
  ```
  Copy it off the box on a schedule.
- **Watch `/healthz`.** Point whatever uptime monitor you already use at it. It
  reports the registered providers and whether chaos is armed.

One thing this version cannot do yet: hold a **different real provider key per
project**. There is one Anthropic key and one OpenAI key for the whole gateway,
and separation between projects comes from the ledger rather than from separate
provider accounts. If you need the provider itself to bill each project
separately, keep those projects on their own keys for now.

