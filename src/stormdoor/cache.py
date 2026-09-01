"""The semantic cache: has this question been answered recently enough to reuse?

Sits in front of the upstream call. On a hit the model is never contacted, so
the request costs nothing and returns in the time it takes to embed one prompt
and scan a scope. That is the whole point: the cheapest call is the one you do
not make.

Three decisions define the cache, and each is a place to be honest rather than
clever:

**What is cacheable.** Only a non-streaming request with temperature 0 or unset.
A request that asked for randomness must not be handed one frozen sample forever;
returning a cached creative answer to `temperature=0.9` would be answering a
different question than the one asked. Streaming is served live and documented as
uncached, because replaying a stream from cache is a separate feature with its
own edge cases and pretending it is free here would be a lie.

**What counts as the same request.** Similarity decides a near-match on the
*prompt*, but only ever within a scope that pins down everything else that
changes the answer: the key (so one tenant is never served another's cached
reply), the model, and the generation parameters. Same scope, near-identical
prompt, unexpired: that is a hit. Anything else is a miss.

**When a hit is wrong.** A too-low floor serves a stale or subtly-different
answer as if it were fresh. The floor defaults high and is the single knob an
operator turns to trade hit rate against that risk. It is applied by the vector
store, so the same threshold governs the SQLite and the Pinecone backends.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from .types import ChatCompletionRequest, Completion, TokenUsage

log = logging.getLogger("stormdoor.cache")


@dataclass(frozen=True, slots=True)
class CacheHit:
    completion: Completion
    similarity: float


def _fingerprint(key_id: str, req: ChatCompletionRequest) -> str:
    """A scope string for everything except the prompt text.

    Anything in here that differs sends a request to a different bucket, so it
    can never be served a cached answer produced under different conditions. The
    prompt itself is deliberately absent: that is what the embedding compares.
    """
    stop = req.stop
    if isinstance(stop, list):
        stop = " ".join(stop)
    parts = [
        key_id,
        req.model,
        f"t={req.temperature}",
        f"p={req.top_p}",
        f"m={req.max_tokens}",
        f"s={stop}",
    ]
    raw = "".join(parts)
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


class SemanticCache:
    def __init__(self, embedder, vector_store, store, *, similarity_floor: float = 0.95,
                 ttl_s: float = 3600.0, enabled: bool = False):
        self._embedder = embedder
        self._vec = vector_store
        self._store = store
        self.similarity_floor = similarity_floor
        self.ttl_s = ttl_s
        self.enabled = enabled

    def cacheable(self, req: ChatCompletionRequest) -> bool:
        if not self.enabled:
            return False
        if req.stream:
            return False
        # None means the caller did not ask, which providers treat as their low
        # default; 0 is an explicit request for determinism. Both are safe to
        # reuse. Anything above 0 asked for variety and must get it.
        return req.temperature in (None, 0, 0.0)

    def lookup(self, key_id: str, req: ChatCompletionRequest, *, now: float) -> CacheHit | None:
        if not self.cacheable(req):
            return None
        scope = _fingerprint(key_id, req)
        vector = self._embedder.embed(req.prompt_text())
        match = self._vec.nearest(
            scope=scope, vector=vector, floor=self.similarity_floor, now=now
        )
        hit = match is not None
        self._store.cache_bump(hit=hit)
        if not hit:
            return None
        p = match.payload
        completion = Completion(
            text=p["text"],
            usage=TokenUsage(
                input_tokens=p.get("input_tokens", 0),
                output_tokens=p.get("output_tokens", 0),
            ),
            model=p["model"],
            finish_reason=p.get("finish_reason", "stop"),
        )
        return CacheHit(completion=completion, similarity=match.similarity)

    def store(self, key_id: str, req: ChatCompletionRequest, completion: Completion,
              *, now: float) -> None:
        if not self.cacheable(req):
            return
        # An empty completion is not worth caching and would embed to a zero
        # vector on the prompt side only if the prompt were empty too; guard the
        # response side explicitly so a provider that returned nothing does not
        # poison the cache with a blank answer.
        if not completion.text:
            return
        scope = _fingerprint(key_id, req)
        vector = self._embedder.embed(req.prompt_text())
        payload = {
            "text": completion.text,
            "model": completion.model,
            "input_tokens": completion.usage.input_tokens,
            "output_tokens": completion.usage.output_tokens,
            "finish_reason": completion.finish_reason,
        }
        self._vec.put(scope=scope, vector=vector, payload=payload,
                      ttl_s=self.ttl_s, now=now)

    def invalidate(self, *, key_id: str | None = None,
                   req: ChatCompletionRequest | None = None) -> int:
        """Forget everything, or one scope. A changed backing document is the
        usual reason: the answer is now wrong and no floor will catch that."""
        scope = None
        if key_id is not None and req is not None:
            scope = _fingerprint(key_id, req)
        return self._vec.invalidate(scope=scope)

    def sweep(self, *, now: float) -> int:
        return self._vec.sweep(now=now)

    def stats(self) -> dict:
        lookups, hits = self._store.cache_stats()
        ratio = (hits / lookups) if lookups else 0.0
        return {
            "enabled": self.enabled,
            "backend": type(self._vec).__name__,
            "embedder": self._embedder.name,
            "similarity_floor": self.similarity_floor,
            "ttl_s": self.ttl_s,
            "lookups": lookups,
            "hits": hits,
            "hit_ratio": round(ratio, 4),
            "entries": self._vec.size(),
        }
