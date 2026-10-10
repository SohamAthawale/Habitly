"""Gateway fallback, retries, rate limiting and usage logging, with scripted fake providers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from roundtable.config import AppConfig, ModelRef
from roundtable.gateway import AllModelsFailed, CallContext, LLMGateway, MemoryUsageSink
from roundtable.gateway.ratelimit import MemoryRateLimiter
from roundtable.providers.base import (
    ModelNotFound,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransientError,
)
from roundtable.providers.registry import ProviderRegistry
from tests.conftest import FakeClock, FakeProvider, Outcome, make_config

MSG = [{"role": "user", "content": "hi"}]


@dataclass
class Rig:
    gateway: LLMGateway
    local: FakeProvider
    remote: FakeProvider
    usage: MemoryUsageSink
    limiter: MemoryRateLimiter
    clock: FakeClock

    def statuses(self) -> list[tuple[str, str]]:
        return [(f"{r.provider}:{r.requested_model}", r.status) for r in self.usage.records]


def rig(
    clock: FakeClock,
    local: dict[str, list[Outcome]],
    remote: dict[str, list[Outcome]] | None = None,
    config: AppConfig | None = None,
    unavailable: dict[str, str] | None = None,
) -> Rig:
    config = config or make_config()
    lp = FakeProvider("local", config.providers["local"], local)
    rp = FakeProvider("remote", config.providers["remote"], remote or {})
    providers = {"local": lp, "remote": rp}
    for name in unavailable or {}:
        providers.pop(name)
    registry = ProviderRegistry(dict(providers), unavailable)
    limiter = MemoryRateLimiter(clock=clock, sleep=clock.sleep)
    usage = MemoryUsageSink()
    gw = LLMGateway(config, registry, limiter, usage, sleep=clock.sleep)
    return Rig(gw, lp, rp, usage, limiter, clock)


def _rl(retry_after: float | None = None) -> RateLimited:
    return RateLimited("429", retry_after_s=retry_after, provider="x", model="x")


async def test_first_model_answers(clock: FakeClock) -> None:
    r = rig(clock, {"a": ["hello"]})
    result = await r.gateway.complete(MSG, ctx=CallContext(role="researcher", run_id="run1"))
    assert result.text == "hello"
    assert r.statuses() == [("local:a", "ok")]
    rec = r.usage.records[0]
    assert (rec.role, rec.run_id, rec.attempt) == ("researcher", "run1", 1)
    assert (rec.prompt_tokens, rec.completion_tokens) == (10, 5)


async def test_falls_back_on_rate_limit_and_cools_down_only_that_model(clock: FakeClock) -> None:
    r = rig(clock, {"a": [_rl()], "b": ["from b"]})
    result = await r.gateway.complete(MSG)
    assert result.text == "from b"
    assert r.statuses() == [("local:a", "rate_limited"), ("local:b", "ok")]
    # No Retry-After: only the model cools down (default 30s), not the whole provider.
    assert await r.limiter.cooldown_remaining("local/a") == pytest.approx(30)
    assert await r.limiter.cooldown_remaining("local") == 0

    r.local.calls.clear()
    await r.gateway.complete(MSG)
    assert [c.model for c in r.local.calls] == ["b"]  # 'a' skipped without a request


async def test_rate_limit_with_retry_after_cools_down_whole_provider(clock: FakeClock) -> None:
    cfg = make_config(preferences={"default": ["remote:x", "local:a"]})
    r = rig(clock, {"a": ["local answer"]}, {"x": [_rl(retry_after=120)]}, config=cfg)
    assert (await r.gateway.complete(MSG)).text == "local answer"
    assert await r.limiter.cooldown_remaining("remote") == pytest.approx(120)

    r.remote.calls.clear()
    await r.gateway.complete(MSG)
    assert r.remote.calls == []  # provider-wide cooldown: no request sent
    assert r.statuses()[-2:] == [("remote:x", "rate_limited"), ("local:a", "ok")]


async def test_timeout_falls_back_without_retrying(clock: FakeClock) -> None:
    r = rig(clock, {"a": [ProviderTimeout("slow")], "b": ["ok"]})
    assert (await r.gateway.complete(MSG)).text == "ok"
    assert [c.model for c in r.local.calls] == ["a", "b"]
    assert r.statuses() == [("local:a", "timeout"), ("local:b", "ok")]
    assert r.usage.records[0].latency_s >= 0.0  # failures are timed too


async def test_transient_errors_retry_same_model(clock: FakeClock) -> None:
    r = rig(clock, {"a": [TransientError("reset"), TransientError("reset"), "third time"]})
    assert (await r.gateway.complete(MSG)).text == "third time"
    assert [c.model for c in r.local.calls] == ["a", "a", "a"]
    assert [rec.attempt for rec in r.usage.records] == [1, 2, 3]
    assert len(clock.slept) == 2  # backoff between retries


async def test_transient_errors_beyond_max_retries_fall_back(clock: FakeClock) -> None:
    r = rig(clock, {"a": [TransientError("down")], "b": ["b answers"]})
    assert (await r.gateway.complete(MSG)).text == "b answers"
    # max_retries=2 -> 3 tries on 'a', then 'b'.
    assert [c.model for c in r.local.calls] == ["a", "a", "a", "b"]


async def test_non_retryable_errors_fall_back_immediately(clock: FakeClock) -> None:
    r = rig(clock, {"a": [ModelNotFound("not pulled")], "b": ["b"]})
    await r.gateway.complete(MSG)
    assert r.statuses() == [("local:a", "model_not_found"), ("local:b", "ok")]
    r2 = rig(clock, {"a": [ProviderError("400 bad request")], "b": ["b"]})
    await r2.gateway.complete(MSG)
    assert r2.statuses() == [("local:a", "error"), ("local:b", "ok")]


async def test_local_rate_limiter_skips_provider_before_calling_it(clock: FakeClock) -> None:
    cfg = make_config(
        providers={
            "local": {"kind": "ollama", "base_url": "http://l"},
            "remote": {
                "kind": "openai_compat",
                "base_url": "http://r",
                "rate_limit": {"rpm": 1, "max_wait_s": 0},
            },
        },
        preferences={"default": ["remote:x", "local:a"]},
    )
    r = rig(clock, {"a": ["local"]}, {"x": ["remote"]}, config=cfg)
    assert (await r.gateway.complete(MSG)).text == "remote"
    assert (await r.gateway.complete(MSG)).text == "local"
    assert len(r.remote.calls) == 1
    skipped = r.usage.records[1]
    assert skipped.status == "rate_limited"
    assert skipped.error is not None
    assert "local rpm limit" in skipped.error


async def test_unavailable_provider_is_skipped(clock: FakeClock) -> None:
    cfg = make_config(preferences={"default": ["remote:x", "local:a"]})
    r = rig(clock, {"a": ["ok"]}, config=cfg, unavailable={"remote": "API key not set"})
    assert (await r.gateway.complete(MSG)).text == "ok"
    assert r.statuses() == [("local:a", "ok")]


async def test_all_models_failing_raises_with_every_reason(clock: FakeClock) -> None:
    r = rig(clock, {"a": [ProviderTimeout("a slow")], "b": [ModelNotFound("b missing")]})
    with pytest.raises(AllModelsFailed) as info:
        await r.gateway.complete(MSG)
    assert len(info.value.errors) == 2
    assert "a slow" in info.value.errors[0]
    assert "b missing" in info.value.errors[1]


async def test_explicit_models_and_role_preferences(clock: FakeClock) -> None:
    cfg = make_config(preferences={"default": ["local:a"], "contrarian": ["local:b"]})
    r = rig(clock, {"a": ["A"], "b": ["B"]}, config=cfg)
    assert (await r.gateway.complete(MSG, ctx=CallContext(role="contrarian"))).text == "B"
    assert (await r.gateway.complete(MSG, ctx=CallContext(role="unknown"))).text == "A"
    assert (await r.gateway.complete(MSG, models=["local:b"])).text == "B"
    assert (await r.gateway.complete(MSG, models=[ModelRef("local", "a")])).text == "A"


def test_server_fallback_groups_consecutive_same_provider_models(clock: FakeClock) -> None:
    cfg = make_config(
        providers={
            "local": {"kind": "ollama", "base_url": "http://l"},
            "remote": {
                "kind": "openai_compat",
                "base_url": "http://r",
                "server_fallback": True,
                "max_server_fallback": 3,
            },
        },
        preferences={"default": ["local:a"]},
    )
    r = rig(clock, {}, config=cfg)
    refs = [ModelRef.parse(x) for x in ["remote:1", "remote:2", "remote:3", "remote:4", "local:a"]]
    plan = [(a.provider, a.model, a.fallbacks) for a in r.gateway.plan_attempts(refs)]
    assert plan == [
        ("remote", "1", ["2", "3"]),
        ("remote", "4", []),
        ("local", "a", []),
    ]
    # Providers without server fallback are never grouped.
    local_refs = [ModelRef.parse("local:a"), ModelRef.parse("local:b")]
    assert len(r.gateway.plan_attempts(local_refs)) == 2


async def test_model_params_merge_and_call_params_override(clock: FakeClock) -> None:
    cfg = make_config(
        models=[{"ref": "local:a", "params": {"think": False, "keep": 1}}],
        preferences={"default": ["local:a"]},
    )
    r = rig(clock, {"a": ["ok"]}, config=cfg)
    await r.gateway.complete(MSG, params={"think": True}, temperature=0.2, max_tokens=50)
    req = r.local.calls[0]
    assert req.params == {"think": True, "keep": 1}
    assert (req.temperature, req.max_tokens) == (0.2, 50)


async def test_cost_from_configured_pricing(clock: FakeClock) -> None:
    cfg = make_config(
        models=[{"ref": "local:a", "input_cost_per_mtok": 1.0, "output_cost_per_mtok": 2.0}],
        preferences={"default": ["local:a"]},
    )
    r = rig(clock, {"a": ["ok"]}, config=cfg)
    result = await r.gateway.complete(MSG)
    assert result.cost_usd == pytest.approx((10 * 1.0 + 5 * 2.0) / 1_000_000)
    assert r.usage.records[0].cost_usd == result.cost_usd


async def test_stream_falls_back_before_first_token(clock: FakeClock) -> None:
    r = rig(clock, {"a": [_rl()], "b": ["streamed answer"]})
    chunks = [c async for c in r.gateway.stream(MSG)]
    assert "".join(c.delta for c in chunks).strip() == "streamed answer"
    final = chunks[-1].result
    assert final is not None
    assert final.model == "b"
    assert r.statuses() == [("local:a", "rate_limited"), ("local:b", "ok")]


async def test_stream_error_after_first_token_is_raised_not_retried(clock: FakeClock) -> None:
    r = rig(clock, {"a": ["PARTIAL:half an ans"], "b": ["never used"]})
    seen: list[str] = []

    async def consume() -> None:
        async for chunk in r.gateway.stream(MSG):
            seen.append(chunk.delta)

    with pytest.raises(ProviderError, match="dropped mid-stream"):
        await consume()
    assert seen == ["half an ans"]
    assert [c.model for c in r.local.calls] == ["a"]
    assert r.statuses() == [("local:a", "error")]


async def test_concurrency_slots_limit_in_flight_calls(clock: FakeClock) -> None:
    cfg = make_config(
        providers={
            "local": {"kind": "ollama", "base_url": "http://l", "max_concurrency": 2},
            "remote": {"kind": "openai_compat", "base_url": "http://r"},
        }
    )
    r = rig(clock, {"a": ["ok"]}, config=cfg)
    in_flight = peak = 0
    original = r.local.chat

    async def slow_chat(req: Any) -> Any:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return await original(req)

    r.local.chat = slow_chat  # type: ignore[method-assign]
    await asyncio.gather(*(r.gateway.complete(MSG) for _ in range(6)))
    assert peak == 2
