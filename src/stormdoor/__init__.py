"""stormdoor: an LLM gateway that proves itself under failure."""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "create_app"]


def create_app(settings=None):
    """Build the ASGI app. Imported lazily so `stormdoor.__version__` stays cheap."""
    from .app import create_app as _create_app

    return _create_app(settings)
