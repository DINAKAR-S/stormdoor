"""Provider registry: model id in, provider out.

Registration is by capability, not by configuration. A provider is registered
when its SDK is importable, and it is asked whether it handles a given model at
resolve time. That means a Claude model released after this code was written
still routes correctly, instead of 404-ing against a hardcoded list.

Resolution order is registration order, and ``echo`` is always first so the
local drill models can never be shadowed by an upstream.
"""

from __future__ import annotations

import logging

from ..errors import UnknownModel
from .base import Provider
from .echo import EchoProvider

log = logging.getLogger("stormdoor.providers")

__all__ = ["Provider", "ProviderRegistry", "build_registry"]


class ProviderRegistry:
    def __init__(self, providers: list[Provider]):
        self._providers = providers

    def resolve(self, model: str) -> tuple[Provider, str]:
        """Turn a model id into ``(provider, the id to send upstream)``.

        Two spellings are accepted, because people arrive expecting one or the
        other and guessing wrong should not be a 404:

        ``gpt-4o-mini``           routed by prefix, and sent on as-is
        ``openai/gpt-4o-mini``    routed to the named provider explicitly

        The prefixed form is worth having beyond taste. It disambiguates when a
        model name exists on more than one provider, and it is the only way to
        reach an OpenAI-compatible server whose model is called something the
        prefix rules would never guess, like ``openai/llama-3.1-70b`` pointed at
        a local vLLM. The provider name is stripped before the call, so upstream
        sees the id it knows.
        """
        if "/" in model:
            name, _, upstream = model.partition("/")
            for provider in self._providers:
                if provider.name == name.lower():
                    if not upstream:
                        raise UnknownModel(
                            f"{model!r} names a provider but no model. "
                            f"Write it as {name}/<model>.",
                            param="model",
                        )
                    return provider, upstream
            raise UnknownModel(
                f"no provider called {name!r} is registered. "
                f"Available: {[p.name for p in self._providers]}. "
                f"You can also drop the prefix and send {upstream!r} on its own.",
                param="model",
            )

        for provider in self._providers:
            if provider.handles(model):
                return provider, model

        raise UnknownModel(
            f"no provider is registered for model {model!r}. "
            f"Registered providers: {[p.name for p in self._providers]}. "
            f"Model ids route by prefix (claude-... to anthropic, gpt-... to openai), "
            f"or name the provider explicitly as <provider>/<model>.",
            param="model",
        )

    def names(self) -> list[str]:
        return [p.name for p in self._providers]

    def catalogue(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for provider in self._providers:
            out.extend({"id": model, "owned_by": provider.name} for model in provider.models())
        return out


def build_registry(settings) -> ProviderRegistry:
    providers: list[Provider] = [EchoProvider()]

    try:
        from .anthropic_provider import AnthropicProvider

        providers.append(
            AnthropicProvider(
                settings.anthropic_api_key,
                default_max_tokens=settings.default_max_tokens,
            )
        )
        if not settings.anthropic_api_key:
            log.info(
                "anthropic provider registered without an explicit key; "
                "the SDK will resolve credentials from the environment"
            )
    except RuntimeError:
        log.info("anthropic provider not registered: install stormdoor[anthropic] to enable it")
    except Exception as exc:  # pragma: no cover - misconfiguration, not a code path
        log.warning("anthropic provider not registered: %s", exc)

    try:
        from .openai_provider import OpenAIProvider

        providers.append(OpenAIProvider(settings.openai_api_key, settings.openai_base_url))
    except RuntimeError:
        log.info("openai provider not registered: install stormdoor[openai] to enable it")
    except Exception as exc:  # pragma: no cover - misconfiguration, not a code path
        log.warning("openai provider not registered: %s", exc)

    log.info("providers registered: %s", [p.name for p in providers])
    return ProviderRegistry(providers)
