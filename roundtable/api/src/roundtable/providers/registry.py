"""Builds provider adapters from config and tracks which ones are unusable and why."""

from __future__ import annotations

import asyncio

from roundtable.config import AppConfig, ProviderConfig
from roundtable.providers.base import BaseProvider, ProviderHealth, ProviderUnavailable
from roundtable.providers.ollama import OllamaProvider
from roundtable.providers.openai_compat import OpenAICompatProvider

_KINDS: dict[str, type[BaseProvider]] = {
    "ollama": OllamaProvider,
    "openai_compat": OpenAICompatProvider,
}


def build_provider(name: str, config: ProviderConfig) -> BaseProvider:
    """Instantiate the adapter for ``config.kind``."""
    return _KINDS[config.kind](name, config)


class ProviderRegistry:
    """Name -> adapter lookup. Unusable providers are kept with a reason, not dropped."""

    def __init__(
        self, providers: dict[str, BaseProvider], unavailable: dict[str, str] | None = None
    ) -> None:
        self._providers = providers
        self.unavailable: dict[str, str] = dict(unavailable or {})

    @classmethod
    def from_config(cls, config: AppConfig) -> ProviderRegistry:
        """Create adapters for every usable provider in ``config``."""
        providers: dict[str, BaseProvider] = {}
        unavailable: dict[str, str] = {}
        for name, pcfg in config.providers.items():
            reason = pcfg.unusable_reason
            if reason:
                unavailable[name] = reason
            else:
                providers[name] = build_provider(name, pcfg)
        return cls(providers, unavailable)

    def get(self, name: str) -> BaseProvider:
        """Return the adapter or raise ``ProviderUnavailable`` with the reason."""
        if name in self._providers:
            return self._providers[name]
        reason = self.unavailable.get(name, "not configured")
        raise ProviderUnavailable(f"provider {name!r} unavailable: {reason}", provider=name)

    def names(self) -> list[str]:
        """Names of usable providers."""
        return list(self._providers)

    async def health(self, timeout_s: float = 5.0) -> dict[str, ProviderHealth]:
        """Probe every usable provider concurrently; unusable ones report their reason."""

        async def probe(p: BaseProvider) -> ProviderHealth:
            try:
                return await asyncio.wait_for(p.health(), timeout_s)
            except TimeoutError:
                return ProviderHealth(ok=False, detail=f"health probe timed out after {timeout_s}s")

        names = list(self._providers)
        results = await asyncio.gather(*(probe(self._providers[n]) for n in names))
        out = dict(zip(names, results, strict=True))
        for name, reason in self.unavailable.items():
            out[name] = ProviderHealth(ok=False, detail=reason)
        return out

    async def aclose(self) -> None:
        """Close all HTTP clients."""
        await asyncio.gather(*(p.aclose() for p in self._providers.values()))
