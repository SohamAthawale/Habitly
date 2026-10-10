"""Liveness and readiness checks.

Readiness is ``fail`` (HTTP 503) when a *required* dependency is down, ``degraded`` when an
optional provider is down or a configured model is not pulled yet, and ``ok`` otherwise.
An optional provider left unconfigured (e.g. no OpenRouter key) is reported but does not
degrade readiness: running fully local is a supported setup, not a fault.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field
from redis.asyncio import Redis

if TYPE_CHECKING:
    from roundtable.services import Services

ReadyStatus = Literal["ok", "degraded", "fail"]


class CheckResult(BaseModel):
    """One dependency's state."""

    ok: bool
    required: bool
    configured: bool = Field(
        default=True, description="False for optional providers left unconfigured on purpose"
    )
    detail: str = ""
    latency_s: float | None = None
    missing_models: list[str] = Field(default_factory=list)


class ReadyReport(BaseModel):
    """Body of ``GET /readyz``."""

    status: ReadyStatus
    checks: dict[str, CheckResult]


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """A named probe. ``probe`` returns a detail string or raises on failure."""

    name: str
    required: bool
    probe: Callable[[], Awaitable[str]]


def postgres_check(pool: AsyncConnectionPool) -> HealthCheck:
    """Readiness probe that runs ``SELECT 1``."""

    async def probe() -> str:
        async with pool.connection(timeout=3) as conn:
            cur = await conn.execute("SELECT version()")
            row = await cur.fetchone()
        return str(row[0]).split(" on ")[0] if row else "connected"

    return HealthCheck("postgres", True, probe)


def redis_check(redis: Redis) -> HealthCheck:
    """Readiness probe that runs ``PING``."""

    async def probe() -> str:
        await redis.ping()
        info = await redis.info("server")
        return f"redis {info.get('redis_version', '?')}"

    return HealthCheck("redis", True, probe)


async def _run(check: HealthCheck, timeout_s: float) -> CheckResult:
    start = time.perf_counter()
    try:
        detail = await asyncio.wait_for(check.probe(), timeout_s)
        ok = True
    except TimeoutError:
        detail, ok = f"timed out after {timeout_s}s", False
    except Exception as exc:
        detail, ok = f"{type(exc).__name__}: {exc}", False
    return CheckResult(
        ok=ok,
        required=check.required,
        detail=detail,
        latency_s=round(time.perf_counter() - start, 4),
    )


async def readiness(services: Services, timeout_s: float = 5.0) -> ReadyReport:
    """Run infrastructure and provider checks concurrently and summarise them."""
    infra_task = asyncio.gather(*(_run(c, timeout_s) for c in services.checks))
    provider_task = services.registry.health(timeout_s)
    infra, providers = await asyncio.gather(infra_task, provider_task)

    checks: dict[str, CheckResult] = {
        c.name: r for c, r in zip(services.checks, infra, strict=True)
    }
    wanted = services.configured_models()
    for name, health in providers.items():
        pcfg = services.config.providers[name]
        if not pcfg.enabled:
            continue  # switched off on purpose; not a readiness concern
        missing: list[str] = []
        if health.ok and pcfg.kind == "ollama":
            have = set(health.models) | {m.removesuffix(":latest") for m in health.models}
            missing = [m for m in wanted.get(name, []) if m not in have]
        checks[f"provider:{name}"] = CheckResult(
            ok=health.ok,
            required=pcfg.required,
            configured=name not in services.registry.unavailable,
            detail=health.detail,
            latency_s=health.latency_s,
            missing_models=missing,
        )

    if any(c.required and not c.ok for c in checks.values()):
        status: ReadyStatus = "fail"
    elif any((not c.ok and c.configured) or c.missing_models for c in checks.values()):
        status = "degraded"
    else:
        status = "ok"
    return ReadyReport(status=status, checks=checks)
