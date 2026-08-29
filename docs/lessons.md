# What this build got wrong first

An honest log of the defects found while building week 1, kept because the
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

## The rule underneath all of these

The test suite proves the code does what you thought of. Something else has to
prove it survives what you did not: many at once, empty, huge, hostile, the
second run, the dependency down, the other machine.

That is what `bench/stress.py` is, and why it runs in CI on every push.
