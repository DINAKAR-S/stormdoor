"""Where cache vectors live and how the nearest one is found.

One interface, two backends, chosen the same way providers are: the default
needs nothing, the scale option is opt-in.

**SQLite (default).** Vectors are float32 blobs in the same database as
everything else. A lookup pulls the most recent N unexpired entries in the
request's scope and finds the nearest by cosine in Python. That "most recent N"
is a real bound, stated so it is not mistaken for an exhaustive search: past it,
an older cached answer will not be found and the request goes upstream. For a
cache in front of an API that is exactly the right shape of wrong, and it keeps a
lookup O(N) with no index to build or extension to install.

**Pinecone (opt-in).** A hosted vector database does the nearest-neighbour search
server-side over the whole set, with no N bound, and shares the cache across
replicas. It costs a network round trip per lookup and a dependency that can be
down, which is why it is not the default: a gateway built to survive its
dependencies failing should not require one just to check its own cache.

Scope is a single opaque string per (key, model, generation-parameters) bucket.
Isolation is not an afterthought: one tenant must never be served another
tenant's cached answer, so the scope carries the key id and the backend filters
on it (a WHERE clause in SQLite, a namespace in Pinecone). Similarity decides a
hit *within* a scope, never across scopes.
"""

from __future__ import annotations

import json
from array import array
from dataclasses import dataclass
from typing import Protocol

from .embeddings import cosine


@dataclass(frozen=True, slots=True)
class Match:
    similarity: float
    payload: dict


def pack_vector(vec: list[float]) -> bytes:
    """float32 is plenty for a similarity cache and halves the stored size."""
    return array("f", vec).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)


class VectorStore(Protocol):
    def put(self, *, scope: str, vector: list[float], payload: dict,
            ttl_s: float, now: float) -> None:
        ...

    def nearest(self, *, scope: str, vector: list[float], floor: float,
                now: float) -> Match | None:
        ...

    def invalidate(self, *, scope: str | None = None) -> int:
        ...

    def sweep(self, *, now: float) -> int:
        ...

    def size(self) -> int:
        ...


class SQLiteVectorStore:
    """Brute-force cosine over the recent-N unexpired entries in a scope.

    The store owns the SQL; this class owns the vector maths. Splitting them that
    way keeps every query in one file and means a different embedding dimension
    needs no schema change, because a vector is just a blob whose length the
    reader already knows.
    """

    def __init__(self, store, *, max_candidates: int = 200):
        self._store = store
        self._max_candidates = max_candidates

    def put(self, *, scope: str, vector: list[float], payload: dict,
            ttl_s: float, now: float) -> None:
        self._store.cache_put(
            scope=scope, vector=pack_vector(vector), payload=payload,
            created_ts=now, expires_ts=now + ttl_s,
        )

    def nearest(self, *, scope: str, vector: list[float], floor: float,
                now: float) -> Match | None:
        best: Match | None = None
        for row in self._store.cache_candidates(scope=scope, now=now,
                                                 limit=self._max_candidates):
            stored = unpack_vector(row["vector"])
            # An entry written under a different embedding dimension — someone
            # changed cache_embedder or cache_embedding_dim with a warm cache — is
            # not comparable. Skip it rather than raise: it is stale by definition
            # and will expire and be swept, and one lookup must not 500 because
            # the config changed under a populated cache.
            if len(stored) != len(vector):
                continue
            sim = cosine(vector, stored)
            if sim >= floor and (best is None or sim > best.similarity):
                best = Match(similarity=sim, payload=json.loads(row["payload"]))
        return best

    def invalidate(self, *, scope: str | None = None) -> int:
        return self._store.cache_invalidate(scope=scope)

    def sweep(self, *, now: float) -> int:
        return self._store.cache_sweep(now=now)

    def size(self) -> int:
        return self._store.cache_size()


class PineconeVectorStore:  # pragma: no cover - needs a live Pinecone index
    """Hosted nearest-neighbour search. Opt-in, one scope per namespace.

    Lazily imports the SDK exactly like the provider adapters, so nothing here is
    paid for unless it is configured. Expiry is written into each vector's
    metadata and enforced on read, because Pinecone's own TTL is index-wide
    rather than per-vector and the cache wants a TTL per entry.
    """

    def __init__(self, *, api_key: str, index: str, cloud_ttl_check: bool = True):
        try:
            from pinecone import Pinecone
        except ImportError as exc:
            raise RuntimeError(
                "the pinecone backend needs the extra: pip install 'stormdoor[pinecone]'"
            ) from exc
        self._pc = Pinecone(api_key=api_key)
        self._index = self._pc.Index(index)
        self._check_ttl = cloud_ttl_check
        self._counter = 0

    def put(self, *, scope: str, vector: list[float], payload: dict,
            ttl_s: float, now: float) -> None:
        self._counter += 1
        self._index.upsert(
            namespace=scope,
            vectors=[{
                "id": f"c{now:.0f}-{self._counter}",
                "values": vector,
                "metadata": {"payload": json.dumps(payload),
                             "expires_ts": now + ttl_s},
            }],
        )

    def nearest(self, *, scope: str, vector: list[float], floor: float,
                now: float) -> Match | None:
        res = self._index.query(namespace=scope, vector=vector, top_k=1,
                                include_metadata=True)
        matches = getattr(res, "matches", None) or res.get("matches", [])
        if not matches:
            return None
        top = matches[0]
        score = top["score"] if isinstance(top, dict) else top.score
        meta = top["metadata"] if isinstance(top, dict) else top.metadata
        if score < floor:
            return None
        if self._check_ttl and float(meta.get("expires_ts", 0)) <= now:
            return None
        return Match(similarity=float(score), payload=json.loads(meta["payload"]))

    def invalidate(self, *, scope: str | None = None) -> int:
        if scope is None:
            self._index.delete(delete_all=True)
        else:
            self._index.delete(namespace=scope, delete_all=True)
        return -1  # Pinecone does not report a deleted count; -1 means "unknown"

    def sweep(self, *, now: float) -> int:
        # Expiry is enforced on read; a hosted index is not scanned to evict.
        return 0

    def size(self) -> int:
        stats = self._index.describe_index_stats()
        return int(stats.get("total_vector_count", 0))


def build_vector_store(settings, store) -> VectorStore:
    """Pick the backend named in settings. SQLite unless told otherwise."""
    kind = getattr(settings, "cache_backend", "sqlite")
    if kind == "sqlite":
        return SQLiteVectorStore(
            store, max_candidates=getattr(settings, "cache_max_candidates", 200)
        )
    if kind == "pinecone":  # pragma: no cover - needs a live index
        if not settings.pinecone_api_key or not settings.pinecone_index:
            raise ValueError(
                "cache_backend=pinecone needs STORMDOOR_PINECONE_API_KEY and "
                "STORMDOOR_PINECONE_INDEX"
            )
        return PineconeVectorStore(
            api_key=settings.pinecone_api_key, index=settings.pinecone_index
        )
    raise ValueError(f"unknown cache_backend {kind!r}, expected 'sqlite' or 'pinecone'")
