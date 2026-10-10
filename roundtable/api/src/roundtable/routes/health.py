"""Health and provider status endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from roundtable.config import RateLimitConfig
from roundtable.health import ReadyReport, readiness
from roundtable.services import Services

router = APIRouter()


def get_services(request: Request) -> Services:
    """FastAPI dependency returning the process-wide ``Services``."""
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


@router.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving requests."""
    return {"status": "ok"}


@router.get("/readyz", response_model=ReadyReport, tags=["health"])
async def readyz(services: ServicesDep, response: Response) -> ReadyReport:
    """Readiness: dependencies and providers. 503 when a required one is down."""
    report = await readiness(services)
    if report.status == "fail":
        response.status_code = 503
    return report


class ProviderStatus(BaseModel):
    """Public view of one provider's configuration and quota usage."""

    name: str
    kind: str
    usable: bool
    reason: str | None
    required: bool
    rate_limit: RateLimitConfig
    used_today: int
    cooldown_s: float
    models: list[str]


@router.get("/v1/providers", response_model=list[ProviderStatus], tags=["providers"])
async def providers(services: ServicesDep) -> list[ProviderStatus]:
    """Configured providers with today's request count and any active 429 cooldown."""
    wanted = services.configured_models()
    out: list[ProviderStatus] = []
    for name, pcfg in services.config.providers.items():
        out.append(
            ProviderStatus(
                name=name,
                kind=pcfg.kind,
                usable=pcfg.usable,
                reason=pcfg.unusable_reason,
                required=pcfg.required,
                rate_limit=pcfg.rate_limit,
                used_today=await services.limiter.used_today(name),
                cooldown_s=round(await services.limiter.cooldown_remaining(name), 1),
                models=wanted.get(name, []),
            )
        )
    return out
