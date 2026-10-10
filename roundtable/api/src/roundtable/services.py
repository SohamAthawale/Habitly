"""Long-lived service objects, built once per process and shared by the API, CLI and workers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from roundtable.config import AppConfig, ModelRef, Settings, load_app_config
from roundtable.gateway import FanoutUsageSink, LLMGateway, LoggingUsageSink, RedisRateLimiter
from roundtable.gateway.ratelimit import RateLimiter
from roundtable.gateway.usage import UsageSink
from roundtable.health import HealthCheck, postgres_check, redis_check
from roundtable.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


@dataclass
class Services:
    """Everything a request handler needs. Tests build this with fakes."""

    settings: Settings
    config: AppConfig
    registry: ProviderRegistry
    limiter: RateLimiter
    usage: UsageSink
    gateway: LLMGateway
    checks: list[HealthCheck] = field(default_factory=list)
    redis: Redis | None = None
    pg: AsyncConnectionPool | None = None

    def configured_models(self) -> dict[str, list[str]]:
        """Model names per provider, from ``models`` and every preference list."""
        refs = [m.ref for m in self.config.models]
        refs += [r for prefs in self.config.model_preferences.values() for r in prefs]
        out: dict[str, list[str]] = {}
        for ref in dict.fromkeys(refs):
            parsed = ModelRef.parse(ref)
            out.setdefault(parsed.provider, []).append(parsed.model)
        return out

    async def aclose(self) -> None:
        """Release network resources."""
        await self.registry.aclose()
        if self.redis is not None:
            await self.redis.aclose()
        if self.pg is not None:
            await self.pg.close()


async def build_services(settings: Settings | None = None) -> Services:
    """Wire up real infrastructure from the environment and the YAML config."""
    settings = settings or Settings()
    config = load_app_config(settings.roundtable_config)
    redis = Redis.from_url(settings.redis_url)
    pg = AsyncConnectionPool(settings.database_url, min_size=1, max_size=5, open=False)
    # Don't block startup on Postgres; readiness reports it until the pool connects.
    await pg.open(wait=False)
    registry = ProviderRegistry.from_config(config)
    for name, reason in registry.unavailable.items():
        logger.warning("provider %s unavailable: %s", name, reason)
    limiter = RedisRateLimiter(redis)
    usage = FanoutUsageSink(LoggingUsageSink())
    gateway = LLMGateway(config, registry, limiter, usage)
    return Services(
        settings=settings,
        config=config,
        registry=registry,
        limiter=limiter,
        usage=usage,
        gateway=gateway,
        checks=[postgres_check(pg), redis_check(redis)],
        redis=redis,
        pg=pg,
    )
