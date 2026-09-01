# What this build got wrong first

An honest log of the defects found while building the first release, kept because the
interesting part is not that they were fixed but that the normal checks all
passed while they were there.

Every one of these was found by looking for it on purpose. None was found by the
test suite going red on its own.

---

## 1. A passing test suite is not evidence

**Looked fine:** 73 tests green. Budgets had four tests. One of them fired twelve
sequential requests at a key and proved it stopped at its ceiling.

**Was actually true:** admission read the recorded spend, then decided. Sixty
requests arriving together each read the same spend and each concluded there was
room. A $0.20 key spent $1.50, a **650% overshoot**.

**Caught by:** an adversarial harness that fires the burst in parallel rather
than in a loop. `bench/stress.py`.

**Changed:** the claim is now atomic. Admission reserves the worst case inside a
`BEGIN IMMEDIATE` transaction and settles it against the real cost in the same
transaction as the ledger row. Same drill, overshoot $0.00.

> Any read-then-decide over shared state is a race. The test that passed was
> sequential, and sequential tests cannot see races by construction.

---

## 2. A loose assertion is worse than no assertion

The first version of the concurrency check allowed sixty-four requests of slack.
It ran, it said **PASS**, and the number underneath it was a 650% overshoot.

A check with a threshold picked to accommodate the current behaviour tests
nothing except that the behaviour has not changed. The threshold is now zero
overshoot, which is the actual invariant.

> If you cannot state the assertion as the property you want, you are writing
> down the bug.

---

## 3. Measure before optimising, and be suspicious of round numbers

Two findings from the same harness, in opposite directions.

**Too slow, and invisible in review.** The gateway did 88 requests a second
against a provider that performs no I/O at all. Nothing in the code looked
wrong. It was opening a SQLite connection per operation, and connecting cost
more than the query. Caching one connection per worker thread took it to about
**390 req/s**, and p50 latency from 330ms to 39ms.

**Too clean, and therefore wrong.** Time to first token measured at exactly
**100%** of total response time. Not a slow stream: httpx buffers an in-process
ASGI response to completion before handing it back, so both numbers were the
same number. The drill now runs a real uvicorn server on a real port and reports
TTFT at about 40% of total.

> A suspiciously round result is usually a fact about your instrument.

---

## 4. The thing you use to demonstrate an invariant must not violate it

The local `echo` provider generates deterministic text so tests can assert exact
token counts. It generated `max_tokens` **words**, which is roughly twice
`max_tokens` **tokens**.

So actual cost exceeded the pre-flight worst case, and the budget guarantee was
false in exactly the tool used to demonstrate it. It now truncates to the token
budget.

---

## 5. A monitoring view that fails silently is worse than no view

The dashboard's poll loop handled a 401 and nothing else. When the gateway went
down, the fetch rejected, the rejection went unhandled, and the page kept
displaying the last numbers it had seen. No spinner, no error, no staleness.

It looked healthy because it had stopped asking.

There is now a banner that names the failure, says the numbers below are history,
gives the time they were last live, and clears itself on recovery. Verified by
killing the server under a loaded page.

> Anything that displays state must be able to say "I do not know".

---

## 6. A secret that regenerates is a lockout

The admin token was generated per process and printed once. Miss that log line
and you cannot reach your own dashboard. Write it down and it stops working
after the next restart.

It is now stored, stable across restarts, printed in a banner, and readable with
`stormdoor admin-token`. The sign-in screen names that command, because the
place someone realises they do not know the token is the sign-in screen.

---

## 7. Guessing wrong should not be a 404

Model ids routed by prefix, so `gpt-4o-mini` worked and `openai/gpt-4o-mini`,
which is what most people try first, returned "model not found".

Both spellings now work, and the error for a genuinely unroutable model names
the registered providers and explains both forms.

> When users guess a format, the guess is data. Either accept it or explain it.

---

## 8. CI can pass without testing what it claims

The first workflow ran `uv pip install --system` across a matrix of Python 3.11
and 3.12. On Linux it failed outright, because the runner's system Python is
externally managed.

The quieter problem: `--system` ignores the matrix version. Had it worked, all
four jobs would have tested whatever Python the runner shipped, and the matrix
would have been decoration. A step now prints the interpreter so it cannot
silently collapse again.

Related: the Dockerfile in the deployment guide was untested, on a page telling
people to run it on a box holding their real provider key. CI now builds the
image, boots it, waits for `/healthz`, asserts it is not running as root, and
runs the CLI inside it.

> An unverified command in a deployment guide is a claim, not a recipe.

---

## 9. Floats are not money, and clocks are not stopwatches

Two flaky assertions, both mine.

Reserving and releasing the same amount sixty times leaves residue in a float
accumulator. `reserved_usd == 0.0` failed while the value printed as `$0.0000`.
The invariant is "nothing meaningful is left", so the check is now a tolerance.

A test asserted that a 50ms injected delay produced at least 50ms of latency. On
Windows, timer granularity is about 15ms and `asyncio.sleep` can return a few
milliseconds early, so it intermittently measured 46ms. It now compares against
an unfaulted request rather than against the wall clock, which is the claim that
actually mattered.

---

## 10. State the limit with a number

Before the concurrency fix, the README said budget overshoot was "bounded by the
estimation error plus the worst-case cost of the in-flight requests".

True. Also useless, and comfortably vaguer than the reality, which was 650%.
Once it was measured it was obviously worth fixing rather than documenting.

> A limit you cannot put a number on is a limit you have not measured.

---

## 11. A falsy zero is a timestamp you threw away

The circuit breaker decided whether a cooldown had expired with
``opened_at = health.opened_at or now``.

``0.0`` is a legitimate timestamp and it is falsy, so a circuit opened at zero
reset its own cooldown on every check and could never probe. It stayed open
forever.

In production this could not happen: ``time.monotonic()`` never returns zero in
a real process. Which is exactly why it would have survived to production. It
only appeared because the tests pass the clock in as an argument instead of
reading it from the environment, and the first test that opened a circuit at
``t=0`` failed immediately.

> Make time an argument. A clock you cannot set is a branch you cannot test.

---

## 12. A feature flag that only half turns the feature off

Failover could be disabled with a setting, and a drill measured what it was
worth by running the same outage twice, once each way. The run with failover
**off** succeeded 99% of the time. It was supposed to fail every request.

The loop skipped any target whose circuit was open with ``continue``, which
jumped straight past the check that was supposed to stop at the first target. So
once the primary tripped its own breaker, requests quietly used the fallback
anyway, with the feature switched off.

The fix was to decide the list of targets once, up front, rather than filtering
inside the loop. Nothing about the outcome looked wrong: requests succeeded, the
ledger was consistent, no test failed. Only comparing a run against its own
control showed it.

> A control run is not ceremony. It is the only thing that can tell you a
> feature did anything at all.

---

## 13. Shared state between measurements

Adding the breaker made four unrelated drills fail at once: mid-stream aborts,
budget admission, rate limiting. None of them had changed.

The drills share one gateway, so the injected-outage drill left a circuit open
and every later drill was measuring the breaker rather than the thing it was
written to measure.

> If two measurements share a process, they share everything in it. Reset
> between them, or accept that you are measuring the order you ran them in.

---

## 14. A test can guard the wrong shape of the thing it guards

There was a test called "a key restricted to one model is never failed over",
and it passed. It made a key allowed only `echo-small`, asked for a route named
`resilient` the key was not allowed, and asserted a 403. Green.

It never once exercised the case the routing docs call the common one: a route
keyed after a real model the key *is* allowed, `{"echo-small": {targets:
[echo-small, echo-large]}}`. There, admission's allow-list check short-circuited
the moment the named model passed, and failover carried the request onto
`echo-large`, a model the key had been explicitly denied. A quiet privilege
escalation, under a test whose name promised it could not happen.

The unit test could not find it because it was asserting the boundary held for
one route shape while the boundary was broken for another. A ship-gate probe that
built the exact config from the docstring found it on the first request.

> A passing test named after an invariant is not proof of the invariant. It is
> proof of the one example it runs. Enumerate the shapes the feature actually
> takes in the docs, and test the boundary on each of them.

---

## 15. The cache billed its own hits

The cache exists to make a repeated question free. The first end-to-end budget
test found it charging full price for every hit.

The ledger-writing helper priced whatever token counts it was handed. A cache hit
carried the original answer's real token counts, for transparency on the
dashboard, so the helper dutifully priced them again. The model was never called
and the money was charged anyway, which is the exact opposite of what a cache is
for.

The fix was a `billed=False` path that records the tokens but forces the cost to
zero. The test that caught it did not check the cache; it checked the budget, and
asserted that the key's spend after a hit equalled its spend before.

> A number that is correct for display is not automatically correct for billing.
> Decide, per field, whether it is a fact to show or a figure to charge, because
> the same tokens are both.

---

## 16. A persisted deadline cannot use a monotonic clock

The breaker measures its cooldown with `time.monotonic()`, which is right: a
monotonic clock never jumps and is immune to the wall clock being adjusted. The
cache copied that instinct for its TTL, and it was wrong, because a cache row
outlives the process that wrote it.

`monotonic()` counts from an arbitrary zero that resets every time the process
starts. A deadline of `monotonic() + 3600`, written to disk and read back by the
next process, is compared against a clock that has just restarted near zero: every
entry looks either immortal or already dead, depending on which way the reset
fell. The breaker never hit this because its state lives only in memory.

The cache uses `time.time()`. A ship-gate probe that wrote an entry, closed the
store, reopened it and checked the entry was still valid caught it.

> In-memory state may use a monotonic clock. Anything written to disk must use a
> wall clock, because the reader is a different process with a different zero.

---

## 17. The benchmark found the false hit before a user could

The local cache embedder hashes words into a fixed-width vector. The cache drill,
written to report a hit ratio, reported more hits than there were repeats. Two
prompts that were supposed to be distinct had collided into the same vector and
served each other's answers.

The drill's first prompts differed by a single token ("question number 7" versus
"question number 23"), which is the pathological case for feature hashing: one
differing token against two shared ones, and if that token collides in the hash,
the vectors are identical and the cosine is a perfect, wrong, 1.0. The floor
cannot save you, because the score is 1.0.

Two honest responses, not one. The default vector width went up, which makes a
collision rare for realistic prompts. And the limit is stated in the README
rather than hidden, because a lexical embedder that can, occasionally, serve the
wrong answer is a real property a reader deserves to know before turning the cache
on. The drill itself was changed to use realistically varied prompts, because
single-token-apart questions are not what real traffic looks like and a benchmark
should measure the real case while the docs cover the adversarial one.

> A cache that matches approximately can match wrongly. Measure the failure, put
> a number on how rare it is, state it, and give the reader the exact knob that
> trades it away.

---

## The rule underneath all of these

The test suite proves the code does what you thought of. Something else has to
prove it survives what you did not: many at once, empty, huge, hostile, the
second run, the dependency down, the other machine.

That is what `bench/stress.py` is, and why it runs in CI on every push.
