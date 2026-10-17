"""Provider registry: maps the ``type`` in config to an implementation."""

from __future__ import annotations

from typing import Any

from .base import BookingNotSupported, Provider, ProviderError
from .http_api import HttpApiProvider
from .irctc import IrctcProvider
from .mock import MockProvider

PROVIDER_TYPES: dict[str, type[Provider]] = {
    cls.provider_type: cls for cls in (MockProvider, HttpApiProvider, IrctcProvider)
}


def register_provider(cls: type[Provider]) -> type[Provider]:
    """Register a custom provider class (usable as a decorator)."""
    PROVIDER_TYPES[cls.provider_type] = cls
    return cls


def build_provider(spec: dict[str, Any]) -> Provider:
    settings = dict(spec)
    name = settings.pop("name", None)
    kind = settings.pop("type", None)
    if not name or not kind:
        raise ProviderError(f"Provider config needs 'name' and 'type': {spec}")
    if kind not in PROVIDER_TYPES:
        raise ProviderError(f"Unknown provider type '{kind}'. Known: {sorted(PROVIDER_TYPES)}")
    return PROVIDER_TYPES[kind](name, settings)


__all__ = [
    "BookingNotSupported",
    "HttpApiProvider",
    "IrctcProvider",
    "MockProvider",
    "PROVIDER_TYPES",
    "Provider",
    "ProviderError",
    "build_provider",
    "register_provider",
]
