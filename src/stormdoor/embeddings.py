"""Turning a prompt into a vector, so a near-repeat can be recognised.

A semantic cache needs two separable things: something that turns text into a
vector (this file) and something that stores vectors and finds the nearest one
(``vectorstore.py``). They are separate because the honest default for each is
different. The default store is SQLite; the default embedder is local.

**The default embedder is lexical and needs no key, no network, no model.** It
hashes tokens into a fixed-width vector and normalises it, so two prompts that
share words land close together and two that do not land apart. It recognises
"what is the capital of France" and "what's the capital of France?" as the same
question, which is most of what a cache in front of an API is actually asked to
do. It does not recognise "cancel my order" and "I'd like to stop my purchase"
as the same, because it has no idea what the words mean. That is the honest
limit of a lexical embedder, and it is why a real embedding model plugs in.

**A real embedding model is opt-in.** ``OpenAIEmbedder`` calls a hosted model
and captures meaning, at the cost of a key, a network round trip and a per-call
charge. It is behind the ``openai`` extra and is never the default, because the
whole gateway is built to run with nothing, and a cache that needs a second API
just to answer "have I seen this before" is a strange thing to require.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

# Split on anything that is not a word character. Deliberately dumb: a cache
# embedder wants to collapse punctuation and case, not to tokenise perfectly.
_TOKEN = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """Text in, a unit vector out. ``dim`` is fixed for the life of the object."""

    name: str
    dim: int

    def embed(self, text: str) -> list[float]:
        ...


def _l2_normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        # An empty or all-out-of-vocab string has no direction. Returning the
        # zero vector is correct: cosine against it is undefined, and the cache
        # treats an undefined similarity as a miss rather than a coincidental
        # hit against every other empty prompt.
        return vec
    return [x / norm for x in vec]


class LocalEmbedder:
    """Feature-hashed bag of words, L2-normalised. Deterministic and offline.

    Feature hashing rather than a learned vocabulary because a cache must embed a
    word it has never seen before without retraining anything, and because a
    fixed width keeps every vector the same size with no dictionary to persist.
    Collisions exist at this width and cost a little precision; they never cost
    correctness, because the similarity floor is what decides a hit, not the
    embedder.
    """

    def __init__(self, dim: int = 256):
        if dim <= 0:
            raise ValueError("embedding dim must be positive")
        self.name = f"local-hash-{dim}"
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN.findall(text.lower()):
            # Hash the token to a bucket and a sign. The sign halves the rate at
            # which two different tokens in the same bucket reinforce each other
            # into a false match; it is the standard signed feature-hashing trick.
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            h = int.from_bytes(digest, "big")
            bucket = h % self.dim
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[bucket] += sign
        return _l2_normalise(vec)


class OpenAIEmbedder:
    """A hosted embedding model. Opt-in, needs a key, captures meaning.

    Mirrors the provider adapters: the SDK is imported lazily so importing this
    module never drags in a dependency the default path does not use, and a
    missing install fails with the exact command to fix it rather than a
    ModuleNotFoundError three layers down.
    """

    def __init__(self, *, api_key: str | None = None, model: str = "text-embedding-3-small",
                 base_url: str | None = None, dim: int = 1536):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "the openai embedder needs the extra: pip install 'stormdoor[openai]'"
            ) from exc
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self._model = model
        self.name = f"openai:{model}"
        self.dim = dim

    def embed(self, text: str) -> list[float]:  # pragma: no cover - needs a live key
        # OpenAI already returns L2-normalised embeddings, but normalising again
        # is cheap and makes the cosine maths in the vector store independent of
        # which embedder produced the vector.
        resp = self._client.embeddings.create(model=self._model, input=text)
        return _l2_normalise(list(resp.data[0].embedding))


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors of equal length.

    Both embedders return L2-normalised vectors, so this is just a dot product,
    but the normalisation is done here as well so a vector from anywhere is
    handled correctly. Returns 0.0 when either side has no magnitude, which the
    cache reads as "not similar" rather than raising.
    """
    if len(a) != len(b):
        raise ValueError(f"cannot compare vectors of length {len(a)} and {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def build_embedder(settings) -> Embedder:
    """Pick the embedder named in settings. Local unless told otherwise."""
    kind = getattr(settings, "cache_embedder", "local")
    if kind == "local":
        return LocalEmbedder(dim=getattr(settings, "cache_embedding_dim", 256))
    if kind == "openai":
        return OpenAIEmbedder(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=getattr(settings, "cache_embedding_model", "text-embedding-3-small"),
        )
    raise ValueError(f"unknown cache_embedder {kind!r}, expected 'local' or 'openai'")
