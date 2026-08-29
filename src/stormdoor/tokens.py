"""Cheap local token estimation, used only for decisions made before the call.

Two rules govern this module:

1. It is a heuristic and it says so. Roughly four characters per token holds
   well enough for English prose to gate admission, and badly for code, CJK
   text and base64. Treat every number here as an upper-bound guess.
2. It never touches billing. Cost is always computed from the token counts the
   provider returns in its own response. The estimate exists so the gateway can
   refuse a request *before* spending money on it, which is the whole point of
   pre-flight admission control.

Counting exactly would mean a tokenizer per provider, a network round trip, or
both, on the hot path of every request. That trade is not worth it for a gate
whose only job is to be roughly right and never slow.
"""

from __future__ import annotations

CHARS_PER_TOKEN = 4.0

# Rough per-message framing cost (role markers, separators). Small, but it
# matters when a caller sends hundreds of short messages.
PER_MESSAGE_OVERHEAD = 4


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a string. Never returns less than 1 for non-empty input."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN + 0.5))


def estimate_prompt_tokens(texts: list[str]) -> int:
    """Estimate a whole conversation, including per-message framing."""
    if not texts:
        return 0
    return sum(estimate_tokens(t) for t in texts) + PER_MESSAGE_OVERHEAD * len(texts)
