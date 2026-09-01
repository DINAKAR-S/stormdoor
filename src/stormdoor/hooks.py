"""Guardrails, as a chain of pre and post hooks an operator composes.

A hook sees the request on the way in or the response on the way out. Pre hooks
run in order before the upstream call and may rewrite the request (redact a
credit-card number out of a prompt) or refuse it outright (an obvious injection
attempt). Post hooks run in order on the answer before it reaches the caller
(redact a phone number a model repeated back). The chain is configured by name
and order, so a deployment runs exactly the guardrails it wants and nothing it
does not, and adding a guardrail later is a config change, not a code change.

Two honest warnings live in this file, because a guardrail that oversells itself
is worse than none:

**PII redaction here is regex, not understanding.** It catches the shapes of
emails, cards, phone numbers, SSNs, IPs and key-like tokens. It does not catch a
name, an address, or a card number a model spelled out in words, and it will
occasionally redact a long number that was not sensitive. It reduces exposure; it
does not guarantee its absence. Named-entity redaction (a person, an org) needs a
model and is deliberately not bundled, so the dependency is a choice and not a
surprise.

**Injection detection here is a heuristic, not a boundary.** It flags the tired,
obvious attempts ("ignore previous instructions", "reveal your system prompt").
A novel or obfuscated attack walks straight past it, and an innocent prompt that
happens to discuss prompt injection will trip it. Treat a flag as a signal to
log and rate, and treat `block` as a blunt instrument to reach for knowingly. The
real defence against injection is not trusting model output with authority; this
is a tripwire in front of that, not a replacement for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .errors import GuardrailBlocked
from .types import ChatCompletionRequest, ChatMessage


@dataclass(slots=True)
class HookNotes:
    """What the guardrails observed, surfaced on the response so it is auditable
    rather than silent. A redaction that no one can see happened is a surprise
    waiting to happen."""

    redacted: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    def count(self, kind: str, n: int) -> None:
        if n:
            self.redacted[kind] = self.redacted.get(kind, 0) + n

    def flag(self, signal: str) -> None:
        if signal not in self.flags:
            self.flags.append(signal)

    def public(self) -> dict | None:
        if not self.redacted and not self.flags:
            return None
        out: dict = {}
        if self.redacted:
            out["redacted"] = dict(self.redacted)
        if self.flags:
            out["flags"] = list(self.flags)
        return out


# ── PII patterns ─────────────────────────────────────────────────────────────
# Each is a shape, not a proof. The card pattern is followed by a Luhn check
# because a run of digits is common and a valid card number is not.

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PHONE = re.compile(r"\b(?:\+?\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?)?\d{3}[\s-]?\d{3,4}\b")
_KEYISH = re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|sd-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")

PII_KINDS = ("email", "phone", "card", "ssn", "ip", "key")


def _luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _valid_ipv4(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts)


def redact_pii(text: str, kinds: tuple[str, ...], notes: HookNotes) -> str:
    """Replace the shapes of PII with a labelled placeholder. Order matters:
    keys and emails before the looser number patterns, so a key or an email host
    is not half-eaten by the phone pattern first."""

    def sub(pattern: re.Pattern, label: str, kind: str, text: str,
            guard=None) -> str:
        count = 0

        def repl(m: re.Match) -> str:
            nonlocal count
            if guard is not None and not guard(m.group(0)):
                return m.group(0)
            count += 1
            return f"[REDACTED_{label}]"

        out = pattern.sub(repl, text)
        notes.count(kind, count)
        return out

    if "key" in kinds:
        text = sub(_KEYISH, "KEY", "key", text)
    if "email" in kinds:
        text = sub(_EMAIL, "EMAIL", "email", text)
    if "ssn" in kinds:
        text = sub(_SSN, "SSN", "ssn", text)
    if "card" in kinds:
        text = sub(_CARD, "CARD", "card", text, guard=_luhn_ok)
    if "ip" in kinds:
        text = sub(_IPV4, "IP", "ip", text, guard=_valid_ipv4)
    if "phone" in kinds:
        text = sub(_PHONE, "PHONE", "phone", text)
    return text


# ── injection heuristics ─────────────────────────────────────────────────────

_INJECTION = [
    ("ignore_previous", re.compile(
        r"\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|earlier|all)\b"
        r".{0,30}\b(instruction|instructions|prompt|prompts|direction|directions|rule|rules)\b",
        re.I)),
    ("reveal_system", re.compile(
        r"\b(reveal|show|print|repeat|tell me|what is)\b.{0,30}\b(your |the )?"
        r"(system )?(prompt|instructions|rules)\b", re.I)),
    ("role_override", re.compile(
        r"\byou are (now )?(a |an )?(dan|do anything now|unrestricted|jailbroken|developer mode)\b",
        re.I)),
    ("no_longer_bound", re.compile(
        r"\b(no longer|not) (bound|restricted|limited) by\b", re.I)),
    ("pretend", re.compile(r"\bpretend (that )?(you are|to be)\b", re.I)),
    ("override_guardrails", re.compile(
        r"\b(bypass|override|turn off|disable)\b.{0,20}"
        r"\b(guardrail|guardrails|safety|filter|filters|restriction|restrictions)\b", re.I)),
]


def injection_signals(text: str) -> list[str]:
    return [name for name, pattern in _INJECTION if pattern.search(text)]


# ── hooks ────────────────────────────────────────────────────────────────────


def _map_messages(req: ChatCompletionRequest, fn) -> ChatCompletionRequest:
    """Return a copy of the request with each message's text passed through fn.

    String content is redacted in place. Structured (multimodal) content has its
    text blocks redacted and its non-text blocks left untouched, so a redaction
    hook never silently drops an image."""
    new_messages: list[ChatMessage] = []
    for m in req.messages:
        if isinstance(m.content, str):
            new_messages.append(m.model_copy(update={"content": fn(m.content)}))
        elif isinstance(m.content, list):
            blocks = []
            for block in m.content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    blocks.append({**block, "text": fn(block["text"])})
                else:
                    blocks.append(block)
            new_messages.append(m.model_copy(update={"content": blocks}))
        else:
            new_messages.append(m)
    return req.model_copy(update={"messages": new_messages})


class PIIRedactHook:
    """Redact PII on the way in, the way out, or both."""

    def __init__(self, name: str, *, kinds: tuple[str, ...], on_request: bool,
                 on_response: bool):
        self.name = name
        self._kinds = kinds
        self._on_request = on_request
        self._on_response = on_response

    def request(self, req: ChatCompletionRequest, notes: HookNotes) -> ChatCompletionRequest:
        if not self._on_request:
            return req
        return _map_messages(req, lambda t: redact_pii(t, self._kinds, notes))

    def response(self, text: str, notes: HookNotes) -> str:
        if not self._on_response:
            return text
        return redact_pii(text, self._kinds, notes)


class InjectionHook:
    """Flag or block the obvious prompt-injection shapes on the way in."""

    def __init__(self, name: str, *, action: Literal["flag", "block"], threshold: int = 1):
        self.name = name
        self._action = action
        self._threshold = threshold

    def request(self, req: ChatCompletionRequest, notes: HookNotes) -> ChatCompletionRequest:
        signals = injection_signals(req.prompt_text())
        if len(signals) < self._threshold:
            return req
        for s in signals:
            notes.flag(f"injection:{s}")
        if self._action == "block":
            raise GuardrailBlocked(
                "the request was blocked by an injection guardrail",
                guardrail=self.name, signals=signals,
            )
        return req

    def response(self, text: str, notes: HookNotes) -> str:
        return text


@dataclass(slots=True)
class HookChain:
    pre: list = field(default_factory=list)
    post: list = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.pre or self.post)

    def on_request(self, req: ChatCompletionRequest, notes: HookNotes) -> ChatCompletionRequest:
        for hook in self.pre:
            req = hook.request(req, notes)
        return req

    def on_response(self, text: str, notes: HookNotes) -> str:
        for hook in self.post:
            text = hook.response(text, notes)
        return text


def _pii_kinds(settings) -> tuple[str, ...]:
    raw = getattr(settings, "guardrail_pii_kinds", "") or ""
    chosen = tuple(k.strip() for k in raw.split(",") if k.strip())
    if not chosen:
        return PII_KINDS
    bad = [k for k in chosen if k not in PII_KINDS]
    if bad:
        raise ValueError(f"unknown PII kind(s) {bad}, expected any of {list(PII_KINDS)}")
    return chosen


def build_hook_chain(settings) -> HookChain:
    """Assemble the chain named in ``guardrail_hooks``, in the order written.

    An empty setting means no hooks and the exact behaviour the gateway had
    before guardrails existed. Every hook is opt-in; nothing here runs unless a
    deployment asks for it by name.
    """
    names = [n.strip() for n in (getattr(settings, "guardrail_hooks", "") or "").split(",")
             if n.strip()]
    kinds = _pii_kinds(settings)
    threshold = getattr(settings, "guardrail_injection_threshold", 1)

    chain = HookChain()
    for name in names:
        if name == "pii_redact":
            hook = PIIRedactHook(name, kinds=kinds, on_request=True, on_response=False)
            chain.pre.append(hook)
        elif name == "pii_redact_output":
            hook = PIIRedactHook(name, kinds=kinds, on_request=False, on_response=True)
            chain.post.append(hook)
        elif name == "injection_flag":
            chain.pre.append(InjectionHook(name, action="flag", threshold=threshold))
        elif name == "injection_block":
            chain.pre.append(InjectionHook(name, action="block", threshold=threshold))
        else:
            raise ValueError(
                f"unknown guardrail hook {name!r}. Known hooks: pii_redact, "
                "pii_redact_output, injection_flag, injection_block"
            )
    return chain
