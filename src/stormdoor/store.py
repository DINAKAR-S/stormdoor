"""Virtual keys and the usage ledger, on SQLite.

SQLite is the default on purpose. The gateway has to run for someone who just
cloned the repo, with no Docker daemon, no Postgres and no Redis, or the
failure demos never get run. The access pattern here is small writes and
point lookups, which SQLite in WAL mode handles well past the scale at which
you would have moved to Postgres for other reasons anyway.

Two tables:

``virtual_keys``   one row per issued key. The secret is never stored, only its
                   SHA-256. ``spent_usd`` is a running total kept in step with
                   the ledger inside one transaction, so admission checks are a
                   single indexed read rather than an aggregate over history.

``usage_records``  append-only. One row per request, successful or not,
                   including requests refused at the door and requests killed
                   by an injected fault. Rows are never updated or deleted,
                   because a ledger you can edit is not a ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

KEY_PREFIX = "sd-"
_PREFIX_DISPLAY_LEN = 11  # "sd-" + 8 hex, enough to recognise a key, useless as a credential

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS virtual_keys (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    key_hash       TEXT NOT NULL UNIQUE,
    key_prefix     TEXT NOT NULL,
    budget_usd     REAL,
    spent_usd      REAL NOT NULL DEFAULT 0.0,
    reserved_usd   REAL NOT NULL DEFAULT 0.0,
    rpm            INTEGER,
    tpm            INTEGER,
    allowed_models TEXT NOT NULL DEFAULT '[]',
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    expires_at     TEXT,
    -- Optional grouping label for billing. Several keys under one tenant roll up
    -- to a single usage line and, if configured, a single Stripe customer.
    tenant         TEXT
);

CREATE TABLE IF NOT EXISTS usage_records (
    id                  TEXT PRIMARY KEY,
    key_id              TEXT NOT NULL,
    request_id          TEXT NOT NULL,
    ts                  TEXT NOT NULL,
    model               TEXT NOT NULL,
    provider            TEXT NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0.0,
    pricing_known       INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL,
    error_code          TEXT,
    latency_ms          INTEGER,
    ttft_ms             INTEGER,
    streamed            INTEGER NOT NULL DEFAULT 0,
    chaos_fault         TEXT,
    attempts            INTEGER NOT NULL DEFAULT 1,
    failed_over_from    TEXT,
    FOREIGN KEY (key_id) REFERENCES virtual_keys(id)
);

-- Small key/value table for things the gateway needs to remember about itself.
-- The generated admin token, which has to survive a restart or the dashboard
-- locks you out every time the process comes back, and the semantic cache's
-- cumulative lookup/hit counters so a hit ratio survives a restart.
CREATE TABLE IF NOT EXISTS gateway_settings (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The semantic cache. One row per cached answer. `scope` pins the key, model and
-- generation parameters together so a lookup only ever compares within one
-- tenant's own same-parameter requests; `vector` is the prompt embedding as
-- float32 bytes; `payload` is the stored completion as JSON. `expires_ts` is a
-- monotonic-clock deadline, so a lookup filters on it and a sweep deletes past
-- it without parsing a timestamp.
CREATE TABLE IF NOT EXISTS cache_entries (
    id          TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,
    vector      BLOB NOT NULL,
    payload     TEXT NOT NULL,
    created_ts  REAL NOT NULL,
    expires_ts  REAL NOT NULL
);

-- A record of usage already pushed to an external meter (Stripe), so a re-run
-- of the push never bills the same period twice. The period_key is deterministic
-- per (sink, window), and the row is written in the same transaction that marks
-- the push done, so a crash leaves either a complete push or none.
CREATE TABLE IF NOT EXISTS metering_pushes (
    period_key  TEXT PRIMARY KEY,
    sink        TEXT NOT NULL,
    pushed_at   TEXT NOT NULL,
    events      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_usage_key_ts   ON usage_records(key_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_request  ON usage_records(request_id);
CREATE INDEX IF NOT EXISTS idx_keys_hash      ON virtual_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_cache_scope    ON cache_entries(scope, created_ts);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_secret() -> str:
    return KEY_PREFIX + secrets.token_hex(20)


@dataclass(slots=True)
class VirtualKey:
    id: str
    name: str
    key_prefix: str
    budget_usd: float | None
    spent_usd: float
    reserved_usd: float
    rpm: int | None
    tpm: int | None
    allowed_models: list[str]
    enabled: bool
    created_at: str
    expires_at: str | None
    tenant: str | None = None

    @property
    def budget_remaining_usd(self) -> float | None:
        """What is left after spend and after everything currently in flight."""
        if self.budget_usd is None:
            return None
        return max(0.0, self.budget_usd - self.spent_usd - self.reserved_usd)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        now = now or datetime.now(UTC)
        return datetime.fromisoformat(self.expires_at) <= now

    def allows_model(self, model: str) -> bool:
        # An empty allow-list means every model the gateway can route.
        return not self.allowed_models or model in self.allowed_models

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "budget_usd": self.budget_usd,
            "spent_usd": round(self.spent_usd, 6),
            "reserved_usd": round(self.reserved_usd, 6),
            "budget_remaining_usd": (
                None if self.budget_remaining_usd is None
                else round(self.budget_remaining_usd, 6)
            ),
            "rpm": self.rpm,
            "tpm": self.tpm,
            "allowed_models": self.allowed_models,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "tenant": self.tenant,
        }


def _row_to_key(row: sqlite3.Row) -> VirtualKey:
    # sqlite3.Row membership tests values, not column names, so ask keys()
    # explicitly. Bound to a name so it reads as a set lookup, not a dict scan.
    columns = row.keys()
    return VirtualKey(
        id=row["id"],
        name=row["name"],
        key_prefix=row["key_prefix"],
        budget_usd=row["budget_usd"],
        spent_usd=row["spent_usd"],
        reserved_usd=row["reserved_usd"],
        rpm=row["rpm"],
        tpm=row["tpm"],
        allowed_models=json.loads(row["allowed_models"]),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        tenant=row["tenant"] if "tenant" in columns else None,
    )


class Store:
    """SQLite-backed key and usage store.

    Every public method is async and runs the blocking call on a worker thread,
    so a slow disk cannot stall the event loop. Connections are cached per
    thread rather than opened per operation, which sidesteps SQLite's
    thread-affinity rules and, as the bench harness showed, is worth roughly
    five times the throughput.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._open: list[sqlite3.Connection] = []
        self._open_lock = threading.Lock()
        self._init_schema()

    def _connection(self) -> sqlite3.Connection:
        """One connection per worker thread, reused for the life of the process.

        Opening a connection per operation cost more than the operation: the
        first bench run measured 88 requests a second against a provider that
        does no I/O at all, and the time was going into connect and close, not
        into SQL. Connections are cached per thread because SQLite objects have
        thread affinity, and tracked centrally so ``close`` can reach them.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path, timeout=10.0, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # WAL already gives durability across process crashes. NORMAL trades
            # the last few writes on a host power loss for a large write speedup,
            # which is the right trade for a usage ledger.
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._open_lock:
                self._open.append(conn)
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        yield self._connection()

    def close(self) -> None:
        """Close every connection this store opened, from any thread."""
        with self._open_lock:
            for conn in self._open:
                with contextlib.suppress(sqlite3.Error):  # already closed is fine
                    conn.close()
            self._open.clear()
        self._local = threading.local()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

            # Databases created before reservations existed are missing the
            # column. Adding it is the whole migration.
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(virtual_keys)")}
            if "reserved_usd" not in columns:
                conn.execute(
                    "ALTER TABLE virtual_keys ADD COLUMN reserved_usd REAL NOT NULL DEFAULT 0.0"
                )
            if "tenant" not in columns:
                conn.execute("ALTER TABLE virtual_keys ADD COLUMN tenant TEXT")

            usage_columns = {r["name"] for r in conn.execute("PRAGMA table_info(usage_records)")}
            if "attempts" not in usage_columns:
                conn.execute(
                    "ALTER TABLE usage_records ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1"
                )
            if "failed_over_from" not in usage_columns:
                conn.execute("ALTER TABLE usage_records ADD COLUMN failed_over_from TEXT")

            # No request can be in flight at startup, so any reservation on disk
            # is a leak from a process that died mid-request. Clearing them here
            # is what stops a crash from permanently shrinking a key's budget.
            # Note this assumes one gateway process per database file, which is
            # the documented topology for the SQLite backend.
            conn.execute("UPDATE virtual_keys SET reserved_usd = 0.0 WHERE reserved_usd != 0.0")

    # ── keys ─────────────────────────────────────────────────────────────

    def _create_key(
        self,
        *,
        name: str,
        budget_usd: float | None,
        rpm: int | None,
        tpm: int | None,
        allowed_models: list[str],
        expires_at: str | None,
        tenant: str | None = None,
    ) -> tuple[VirtualKey, str]:
        secret = generate_secret()
        key = VirtualKey(
            id=f"key_{uuid.uuid4().hex[:16]}",
            name=name,
            key_prefix=secret[:_PREFIX_DISPLAY_LEN],
            budget_usd=budget_usd,
            spent_usd=0.0,
            reserved_usd=0.0,
            rpm=rpm,
            tpm=tpm,
            allowed_models=allowed_models,
            enabled=True,
            created_at=_now(),
            expires_at=expires_at,
            tenant=tenant,
        )
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO virtual_keys
                   (id, name, key_hash, key_prefix, budget_usd, spent_usd, rpm, tpm,
                    allowed_models, enabled, created_at, expires_at, tenant)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    key.id, key.name, hash_secret(secret), key.key_prefix,
                    key.budget_usd, 0.0, key.rpm, key.tpm,
                    json.dumps(key.allowed_models), 1, key.created_at, key.expires_at,
                    key.tenant,
                ),
            )
        return key, secret

    async def create_key(
        self,
        *,
        name: str,
        budget_usd: float | None = None,
        rpm: int | None = None,
        tpm: int | None = None,
        allowed_models: list[str] | None = None,
        expires_at: str | None = None,
        tenant: str | None = None,
    ) -> tuple[VirtualKey, str]:
        """Create a key. The plaintext secret is returned once and never stored."""
        return await asyncio.to_thread(
            self._create_key,
            name=name,
            budget_usd=budget_usd,
            rpm=rpm,
            tenant=tenant,
            tpm=tpm,
            allowed_models=allowed_models or [],
            expires_at=expires_at,
        )

    def _key_by_secret(self, secret: str) -> VirtualKey | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM virtual_keys WHERE key_hash = ?", (hash_secret(secret),)
            ).fetchone()
        return _row_to_key(row) if row else None

    async def key_by_secret(self, secret: str) -> VirtualKey | None:
        return await asyncio.to_thread(self._key_by_secret, secret)

    def _key_by_id(self, key_id: str) -> VirtualKey | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM virtual_keys WHERE id = ?", (key_id,)).fetchone()
        return _row_to_key(row) if row else None

    async def key_by_id(self, key_id: str) -> VirtualKey | None:
        return await asyncio.to_thread(self._key_by_id, key_id)

    def _list_keys(self) -> list[VirtualKey]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM virtual_keys ORDER BY created_at DESC").fetchall()
        return [_row_to_key(r) for r in rows]

    async def list_keys(self) -> list[VirtualKey]:
        return await asyncio.to_thread(self._list_keys)

    def _set_enabled(self, key_id: str, enabled: bool) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE virtual_keys SET enabled = ? WHERE id = ?", (int(enabled), key_id)
            )
        return cur.rowcount > 0

    async def set_enabled(self, key_id: str, enabled: bool) -> bool:
        return await asyncio.to_thread(self._set_enabled, key_id, enabled)

    # ── gateway settings ─────────────────────────────────────────────────
    # Synchronous on purpose: these run at startup and from the CLI, before
    # there is an event loop to hand work to.

    def get_setting(self, name: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM gateway_settings WHERE name = ?", (name,)
            ).fetchone()
        return row["value"] if row else None

    def set_setting(self, name: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO gateway_settings (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value),
            )

    # ── semantic cache ────────────────────────────────────────────────────
    # These are synchronous on purpose: the vector store calls them inside one
    # asyncio.to_thread hop from the gateway, so the cosine scan and the SQL run
    # on the same worker thread and the per-thread connection is reused. Wrapping
    # each one in its own to_thread would fan a single lookup across threads and
    # lose that connection.

    def cache_put(self, *, scope: str, vector: bytes, payload: dict,
                  created_ts: float, expires_ts: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cache_entries (id, scope, vector, payload, created_ts, expires_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"cache_{uuid.uuid4().hex[:16]}", scope, vector,
                 json.dumps(payload), created_ts, expires_ts),
            )

    def cache_candidates(self, *, scope: str, now: float, limit: int):
        """The most recent unexpired entries in one scope, newest first.

        The limit is the bound the SQLite backend documents: only these rows are
        compared, so an older cached answer past the limit is not found. Ordering
        by created_ts newest-first means the bound drops the oldest, which is the
        least likely to still be the right answer."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT vector, payload FROM cache_entries "
                "WHERE scope = ? AND expires_ts > ? "
                "ORDER BY created_ts DESC LIMIT ?",
                (scope, now, limit),
            ).fetchall()

    def cache_invalidate(self, *, scope: str | None = None) -> int:
        with self._conn() as conn:
            if scope is None:
                cur = conn.execute("DELETE FROM cache_entries")
            else:
                cur = conn.execute("DELETE FROM cache_entries WHERE scope = ?", (scope,))
            return cur.rowcount

    def cache_sweep(self, *, now: float) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM cache_entries WHERE expires_ts <= ?", (now,))
            return cur.rowcount

    def cache_size(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM cache_entries").fetchone()["n"]

    def cache_bump(self, *, hit: bool) -> None:
        """One atomic increment of the lookup counter, and the hit counter too on
        a hit. Kept in gateway_settings so the ratio survives a restart."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for name in ("cache_lookups", "cache_hits") if hit else ("cache_lookups",):
                    conn.execute(
                        "INSERT INTO gateway_settings (name, value) VALUES (?, '1') "
                        "ON CONFLICT(name) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
                        (name,),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def cache_stats(self) -> tuple[int, int]:
        lookups = int(self.get_setting("cache_lookups") or 0)
        hits = int(self.get_setting("cache_hits") or 0)
        return lookups, hits

    # ── metering / usage export ───────────────────────────────────────────

    def _usage_export(self, *, since: str | None, until: str | None,
                      group_by: str) -> list[dict]:
        # Only real spend counts toward a bill: refusals and pure errors produced
        # no tokens and cost nothing, so they are excluded here even though they
        # stay in the ledger. A cache hit is billed at zero and is likewise not a
        # charge, but its tokens are real usage, so it is counted in the token
        # totals and contributes $0 to cost, which is the honest picture.
        grp = "k.tenant" if group_by == "tenant" else "u.key_id"
        label = "tenant" if group_by == "tenant" else "key_id"
        where = ["u.status IN ('ok', 'cache_hit')"]
        params: list = []
        if since is not None:
            where.append("u.ts >= ?")
            params.append(since)
        if until is not None:
            where.append("u.ts < ?")
            params.append(until)
        clause = " AND ".join(where)
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT {grp} AS grp,
                          COUNT(*)                         AS requests,
                          COALESCE(SUM(u.input_tokens), 0)  AS input_tokens,
                          COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(u.cost_usd), 0.0)    AS cost_usd,
                          SUM(CASE WHEN u.status = 'cache_hit' THEN 1 ELSE 0 END) AS cache_hits
                   FROM usage_records u
                   LEFT JOIN virtual_keys k ON k.id = u.key_id
                   WHERE {clause}
                   GROUP BY grp
                   ORDER BY cost_usd DESC""",
                params,
            ).fetchall()
        return [
            {
                label: r["grp"],
                "requests": r["requests"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "total_tokens": r["input_tokens"] + r["output_tokens"],
                "cost_usd": round(r["cost_usd"], 8),
                "cache_hits": r["cache_hits"],
            }
            for r in rows
        ]

    async def usage_export(self, *, since: str | None = None, until: str | None = None,
                           group_by: str = "key") -> list[dict]:
        return await asyncio.to_thread(
            self._usage_export, since=since, until=until, group_by=group_by
        )

    def _metering_reserve(self, *, period_key: str, sink: str) -> bool:
        """Claim a period before pushing it. Returns True if this caller won the
        claim, False if it was already taken. The atomic INSERT is the lock: two
        concurrent pushes cannot both win, so the meter is not called twice for
        the same window. A claim is finalized or released once the push settles."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                exists = conn.execute(
                    "SELECT 1 FROM metering_pushes WHERE period_key = ?", (period_key,)
                ).fetchone()
                if exists:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(
                    "INSERT INTO metering_pushes (period_key, sink, pushed_at, events) "
                    "VALUES (?, ?, ?, -1)",  # -1 events marks a claim not yet finalized
                    (period_key, sink, _now()),
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    async def metering_reserve(self, *, period_key: str, sink: str) -> bool:
        return await asyncio.to_thread(
            self._metering_reserve, period_key=period_key, sink=sink
        )

    def _metering_release(self, period_key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM metering_pushes WHERE period_key = ?", (period_key,))

    async def metering_release(self, period_key: str) -> None:
        """Give a claim back when the push failed, so it can be retried."""
        await asyncio.to_thread(self._metering_release, period_key)

    def _metering_finalize(self, *, period_key: str, events: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE metering_pushes SET events = ?, pushed_at = ? WHERE period_key = ?",
                (events, _now(), period_key),
            )

    async def metering_finalize(self, *, period_key: str, events: int) -> None:
        await asyncio.to_thread(self._metering_finalize, period_key=period_key, events=events)

    def ensure_admin_token(self) -> tuple[str, bool]:
        """Return the stored admin token, creating one the first time.

        Generating a fresh token on every start and printing it once meant that
        missing one log line locked you out of your own dashboard, and that the
        token you wrote down stopped working after a restart. Persisting it
        makes it something you can look up later with `stormdoor admin-token`.

        Returns ``(token, created)`` so the caller can tell a first run from
        an ordinary one.
        """
        existing = self.get_setting("admin_token")
        if existing:
            return existing, False
        token = secrets.token_hex(16)
        self.set_setting("admin_token", token)
        return token, True

    # ── budget reservations ──────────────────────────────────────────────

    def _reserve(self, key_id: str, amount: float) -> tuple[bool, float, float]:
        """Claim ``amount`` of a key's remaining budget, atomically.

        Reading the spend and then deciding is a check-then-act race: sixty
        requests arriving together each read the same spend and each conclude
        there is room. Measured on this codebase, that let a $0.20 key spend
        $1.50, a 650% overshoot, which is not a rounding error, it is a
        different number.

        The read and the claim happen inside one ``BEGIN IMMEDIATE``
        transaction, which SQLite serialises against every other writer, so
        concurrent callers queue rather than race. Returns
        ``(granted, spent, committed)`` where ``committed`` is spend plus
        everything currently reserved, so a refusal can explain itself.
        """
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT budget_usd, spent_usd, reserved_usd FROM virtual_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False, 0.0, 0.0

            budget, spent, reserved = row["budget_usd"], row["spent_usd"], row["reserved_usd"]
            committed = spent + reserved

            if budget is not None and committed + amount > budget:
                conn.execute("ROLLBACK")
                return False, spent, committed

            conn.execute(
                "UPDATE virtual_keys SET reserved_usd = reserved_usd + ? WHERE id = ?",
                (amount, key_id),
            )
            conn.execute("COMMIT")
            return True, spent, committed
        except Exception:
            conn.execute("ROLLBACK")
            raise

    async def reserve(self, key_id: str, amount: float) -> tuple[bool, float, float]:
        return await asyncio.to_thread(self._reserve, key_id, amount)

    def _release(self, key_id: str, amount: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE virtual_keys SET reserved_usd = MAX(0.0, reserved_usd - ?) WHERE id = ?",
                (amount, key_id),
            )

    async def release(self, key_id: str, amount: float) -> None:
        """Give back a reservation that will never be spent."""
        if amount:
            await asyncio.to_thread(self._release, key_id, amount)

    # ── usage ────────────────────────────────────────────────────────────

    def _record_usage(
        self,
        *,
        key_id: str,
        request_id: str,
        model: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
        cost_usd: float,
        pricing_known: bool,
        status: str,
        error_code: str | None,
        latency_ms: int | None,
        ttft_ms: int | None,
        streamed: bool,
        chaos_fault: str | None,
        reservation: float = 0.0,
        attempts: int = 1,
        failed_over_from: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO usage_records
                       (id, key_id, request_id, ts, model, provider, input_tokens,
                        output_tokens, cached_input_tokens, cost_usd, pricing_known,
                        status, error_code, latency_ms, ttft_ms, streamed, chaos_fault,
                        attempts, failed_over_from)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"use_{uuid.uuid4().hex[:16]}", key_id, request_id, _now(),
                        model, provider, input_tokens, output_tokens, cached_input_tokens,
                        cost_usd, int(pricing_known), status, error_code,
                        latency_ms, ttft_ms, int(streamed), chaos_fault,
                        attempts, failed_over_from,
                    ),
                )
                # Turning the reservation into real spend has to be one step.
                # Releasing separately would leave a window where the money is
                # neither reserved nor spent, and a request arriving in that
                # window would be admitted against a budget that is not there.
                if cost_usd or reservation:
                    conn.execute(
                        """UPDATE virtual_keys
                              SET spent_usd    = spent_usd + ?,
                                  reserved_usd = MAX(0.0, reserved_usd - ?)
                            WHERE id = ?""",
                        (cost_usd, reservation, key_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    async def record_usage(self, **kwargs) -> None:
        """Append one ledger row and move the running spend, in one transaction."""
        await asyncio.to_thread(self._record_usage, **kwargs)

    def _usage_summary(self, key_id: str, limit: int) -> dict:
        with self._conn() as conn:
            totals = conn.execute(
                """SELECT COUNT(*)                AS requests,
                          COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                          COALESCE(SUM(output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(cost_usd), 0.0)    AS cost_usd,
                          SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)      AS ok,
                          SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END)   AS errors,
                          SUM(CASE WHEN status = 'aborted' THEN 1 ELSE 0 END) AS aborted,
                          SUM(CASE WHEN status = 'refused' THEN 1 ELSE 0 END) AS refused,
                          SUM(CASE WHEN pricing_known = 0 THEN 1 ELSE 0 END)  AS unpriced
                   FROM usage_records WHERE key_id = ?""",
                (key_id,),
            ).fetchone()
            recent = conn.execute(
                """SELECT ts, model, provider, status, error_code, input_tokens,
                          output_tokens, cost_usd, latency_ms, ttft_ms, streamed, chaos_fault,
                          attempts, failed_over_from
                   FROM usage_records WHERE key_id = ?
                   ORDER BY ts DESC, rowid DESC LIMIT ?""",
                (key_id, limit),
            ).fetchall()
        return {
            "totals": {
                "requests": totals["requests"],
                "ok": totals["ok"] or 0,
                "errors": totals["errors"] or 0,
                "aborted": totals["aborted"] or 0,
                "refused": totals["refused"] or 0,
                "unpriced_requests": totals["unpriced"] or 0,
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
                "cost_usd": round(totals["cost_usd"], 6),
            },
            "recent": [dict(r) for r in recent],
        }

    async def usage_summary(self, key_id: str, limit: int = 25) -> dict:
        return await asyncio.to_thread(self._usage_summary, key_id, limit)

    def _count_records(self, key_id: str, status: str | None = None) -> int:
        with self._conn() as conn:
            if status is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM usage_records WHERE key_id = ?", (key_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM usage_records WHERE key_id = ? AND status = ?",
                    (key_id, status),
                ).fetchone()
        return row["n"]

    async def count_records(self, key_id: str, status: str | None = None) -> int:
        return await asyncio.to_thread(self._count_records, key_id, status)

    # ── whole-gateway views, for the dashboard ───────────────────────────

    def _recent_ledger(self, limit: int, day: str | None) -> list[dict]:
        sql = """SELECT u.ts, u.request_id, u.model, u.provider, u.status, u.error_code,
                        u.input_tokens, u.output_tokens, u.cost_usd, u.pricing_known,
                        u.latency_ms, u.ttft_ms, u.streamed, u.chaos_fault,
                        u.attempts, u.failed_over_from,
                        k.name AS key_name, k.id AS key_id
                 FROM usage_records u
                 JOIN virtual_keys k ON k.id = u.key_id"""
        params: list = []
        if day:
            # substr rather than date(): timestamps are stored as ISO-8601 with
            # an offset, and the first ten characters are the calendar day.
            sql += " WHERE substr(u.ts, 1, 10) = ?"
            params.append(day)
        sql += " ORDER BY u.rowid DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    async def recent_ledger(self, limit: int = 50, day: str | None = None) -> list[dict]:
        """The last N requests across every key, newest first.

        ``day`` narrows to one calendar day, ``YYYY-MM-DD``, so the dashboard can
        answer "what did we actually run on the day that cost the most".
        """
        return await asyncio.to_thread(self._recent_ledger, limit, day)

    def _spend_by_day(self, days: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT substr(ts, 1, 10)                    AS day,
                          COALESCE(SUM(cost_usd), 0.0)         AS cost_usd,
                          COUNT(*)                             AS requests,
                          COALESCE(SUM(input_tokens), 0)       AS input_tokens,
                          COALESCE(SUM(output_tokens), 0)      AS output_tokens,
                          SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)      AS ok,
                          SUM(CASE WHEN status = 'refused' THEN 1 ELSE 0 END) AS refused,
                          SUM(CASE WHEN status IN ('error','aborted') THEN 1 ELSE 0 END)
                                                               AS failed
                   FROM usage_records
                   GROUP BY day
                   ORDER BY day DESC
                   LIMIT ?""",
                (days,),
            ).fetchall()
        # Oldest first, so the chart reads left to right like a calendar.
        return [
            {
                "day": r["day"],
                "cost_usd": round(r["cost_usd"], 6),
                "requests": r["requests"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "ok": r["ok"] or 0,
                "refused": r["refused"] or 0,
                "failed": r["failed"] or 0,
            }
            for r in reversed(rows)
        ]

    async def spend_by_day(self, days: int = 14) -> list[dict]:
        """Daily spend, newest last. Days are UTC, matching how rows are stamped."""
        return await asyncio.to_thread(self._spend_by_day, days)

    def _spend_by_day_and_key(self, day: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT k.name AS key_name,
                          COALESCE(SUM(u.cost_usd), 0.0) AS cost_usd,
                          COUNT(*) AS requests
                   FROM usage_records u
                   JOIN virtual_keys k ON k.id = u.key_id
                   WHERE substr(u.ts, 1, 10) = ?
                   GROUP BY u.key_id
                   ORDER BY cost_usd DESC""",
                (day,),
            ).fetchall()
        return [{"key_name": r["key_name"], "cost_usd": round(r["cost_usd"], 6),
                 "requests": r["requests"]} for r in rows]

    async def spend_for_day(self, day: str) -> list[dict]:
        """Which keys spent the money on one particular day."""
        return await asyncio.to_thread(self._spend_by_day_and_key, day)

    def _totals(self) -> dict:
        with self._conn() as conn:
            usage = conn.execute(
                """SELECT COUNT(*) AS requests,
                          COALESCE(SUM(cost_usd), 0.0)      AS cost_usd,
                          COALESCE(SUM(input_tokens), 0)    AS input_tokens,
                          COALESCE(SUM(output_tokens), 0)   AS output_tokens,
                          SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)      AS ok,
                          SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END)   AS errors,
                          SUM(CASE WHEN status = 'aborted' THEN 1 ELSE 0 END) AS aborted,
                          SUM(CASE WHEN status = 'refused' THEN 1 ELSE 0 END) AS refused,
                          SUM(CASE WHEN chaos_fault IS NOT NULL THEN 1 ELSE 0 END) AS drills,
                          SUM(CASE WHEN failed_over_from IS NOT NULL THEN 1 ELSE 0 END)
                                                               AS failed_over,
                          SUM(CASE WHEN pricing_known = 0 THEN 1 ELSE 0 END)  AS unpriced
                   FROM usage_records"""
            ).fetchone()
            keys = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled
                   FROM virtual_keys"""
            ).fetchone()
        return {
            "requests": usage["requests"],
            "ok": usage["ok"] or 0,
            "errors": usage["errors"] or 0,
            "aborted": usage["aborted"] or 0,
            "refused": usage["refused"] or 0,
            "drills": usage["drills"] or 0,
            "failed_over": usage["failed_over"] or 0,
            "unpriced_requests": usage["unpriced"] or 0,
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cost_usd": round(usage["cost_usd"], 6),
            "keys": keys["total"],
            "keys_enabled": keys["enabled"] or 0,
        }

    async def totals(self) -> dict:
        """Gateway-wide counters for the dashboard tiles."""
        return await asyncio.to_thread(self._totals)
