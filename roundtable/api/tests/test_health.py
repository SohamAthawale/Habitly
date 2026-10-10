"""Health endpoints through the real FastAPI app, with fake dependencies."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from roundtable.config import AppConfig, Settings
from roundtable.gateway import LLMGateway, MemoryRateLimiter, MemoryUsageSink
from roundtable.health import HealthCheck
from roundtable.main import create_app
from roundtable.providers.base import ProviderError
from roundtable.providers.registry import ProviderRegistry
from roundtable.services import Services
from tests.conftest import FakeProvider, make_config


class DownProvider(FakeProvider):
    async def list_models(self) -> list[str]:
        raise ProviderError("connection refused")


async def _ok() -> str:
    return "fine"


async def _boom() -> str:
    raise ConnectionError("refused")


def client(
    *,
    config: AppConfig | None = None,
    checks: list[HealthCheck] | None = None,
    local_models: list[str] | None = None,
    remote_down: bool = False,
    unavailable: dict[str, str] | None = None,
) -> Iterator[TestClient]:
    config = config or make_config()
    local = FakeProvider(
        "local", config.providers["local"], {m: ["x"] for m in local_models or ["a", "b"]}
    )
    remote_cls = DownProvider if remote_down else FakeProvider
    remote = remote_cls("remote", config.providers["remote"], {"x": ["x"]})
    providers = {"local": local, "remote": remote}
    for name in unavailable or {}:
        providers.pop(name, None)
    registry = ProviderRegistry(dict(providers), unavailable)
    limiter = MemoryRateLimiter()
    usage = MemoryUsageSink()

    async def factory() -> Services:
        return Services(
            settings=Settings(),
            config=config,
            registry=registry,
            limiter=limiter,
            usage=usage,
            gateway=LLMGateway(config, registry, limiter, usage),
            checks=checks if checks is not None else [HealthCheck("postgres", True, _ok)],
        )

    with TestClient(create_app(factory)) as c:
        yield c


@pytest.fixture
def ok_client() -> Iterator[TestClient]:
    yield from client()


def test_healthz(ok_client: TestClient) -> None:
    assert ok_client.get("/healthz").json() == {"status": "ok"}


def test_readyz_all_ok(ok_client: TestClient) -> None:
    resp = ok_client.get("/readyz")
    assert resp.status_code == 200
    report = resp.json()
    assert report["status"] == "ok"
    assert set(report["checks"]) == {"postgres", "provider:local", "provider:remote"}


def test_readyz_fails_when_required_dependency_down() -> None:
    checks = [HealthCheck("postgres", True, _ok), HealthCheck("redis", True, _boom)]
    for c in client(checks=checks):
        resp = c.get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "fail"
        assert "refused" in body["checks"]["redis"]["detail"]


def test_readyz_degraded_when_optional_provider_down() -> None:
    for c in client(remote_down=True):
        resp = c.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"
        assert resp.json()["checks"]["provider:remote"]["ok"] is False


def test_readyz_fails_when_required_provider_down() -> None:
    cfg = make_config(
        providers={
            "local": {"kind": "ollama", "base_url": "http://l"},
            "remote": {"kind": "openai_compat", "base_url": "http://r", "required": True},
        }
    )
    for c in client(config=cfg, remote_down=True):
        assert c.get("/readyz").status_code == 503


def test_readyz_lists_missing_ollama_models() -> None:
    for c in client(local_models=["a"]):  # preferences want local:a and local:b
        body = c.get("/readyz").json()
        assert body["status"] == "degraded"
        assert body["checks"]["provider:local"]["missing_models"] == ["b"]


def test_readyz_skips_disabled_providers_but_reports_missing_keys() -> None:
    cfg = make_config(
        providers={
            "local": {"kind": "ollama", "base_url": "http://l"},
            "remote": {"kind": "openai_compat", "base_url": "http://r", "enabled": False},
        }
    )
    for c in client(config=cfg, unavailable={"remote": "disabled in config"}):
        assert "provider:remote" not in c.get("/readyz").json()["checks"]

    cfg2 = make_config(
        providers={
            "local": {"kind": "ollama", "base_url": "http://l"},
            "remote": {"kind": "openai_compat", "base_url": "http://r", "requires_api_key": True},
        }
    )
    for c in client(config=cfg2, unavailable={"remote": "API key not set"}):
        body = c.get("/readyz").json()
        check = body["checks"]["provider:remote"]
        assert (check["ok"], check["configured"], check["detail"]) == (
            False,
            False,
            "API key not set",
        )
        assert body["status"] == "ok"  # running without the optional provider is fine


def test_providers_endpoint(ok_client: TestClient) -> None:
    rows = {p["name"]: p for p in ok_client.get("/v1/providers").json()}
    assert rows["remote"]["rate_limit"]["rpd"] == 50
    assert rows["remote"]["used_today"] == 0
    assert rows["local"]["models"] == ["a", "b"]
    assert rows["local"]["usable"] is True
