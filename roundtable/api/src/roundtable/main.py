"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from roundtable import __version__
from roundtable.config import Settings
from roundtable.logging_setup import configure_logging
from roundtable.routes import health
from roundtable.services import Services, build_services

ServicesFactory = Callable[[], Awaitable[Services]]


def create_app(services_factory: ServicesFactory | None = None) -> FastAPI:
    """Build the app. Tests pass a factory that returns ``Services`` wired with fakes."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = await (services_factory() if services_factory else build_services())
        app.state.services = services
        try:
            yield
        finally:
            await services.aclose()

    app = FastAPI(title="Roundtable", version=__version__, lifespan=lifespan)
    app.include_router(health.router)
    return app


def app_from_env() -> FastAPI:
    """Entry point for uvicorn: ``uvicorn roundtable.main:app_from_env --factory``."""
    settings = Settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    return create_app(lambda: build_services(settings))
