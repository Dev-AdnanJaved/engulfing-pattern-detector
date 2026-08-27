from __future__ import annotations

import logging
from typing import Any, Mapping


class ProviderUnavailable(RuntimeError):
    """Raised when a configured market-data provider is not usable."""


class DataProviderManager:
    """Routes market-data calls to independently configured providers."""

    def __init__(
        self,
        providers: Mapping[str, Any] | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.providers = dict(providers or {})
        self.unavailable: dict[str, str] = {}
        self.logger = logger or logging.getLogger(__name__)

    def add_provider(self, name: str, provider: Any) -> None:
        self.providers[name] = provider
        self.unavailable.pop(name, None)

    def mark_unavailable(self, name: str, reason: str) -> None:
        self.providers.pop(name, None)
        self.unavailable[name] = reason

    def is_available(self, name: str) -> bool:
        return name in self.providers

    def get_provider(self, name: str) -> Any:
        provider = self.providers.get(name)
        if provider is not None:
            return provider
        reason = self.unavailable.get(name, "provider is not configured")
        raise ProviderUnavailable(f"{name.upper()} provider unavailable: {reason}")

    def close(self) -> None:
        for name, provider in self.providers.items():
            close = getattr(provider, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                self.logger.exception("%s provider shutdown failed", name.upper())