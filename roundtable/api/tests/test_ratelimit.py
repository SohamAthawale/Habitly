"""Rate limiter semantics: in-memory with a fake clock, and Redis against a real server."""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator

import pytest
from redis.asyncio import Redis

from roundtable.config import RateLimitConfig
from roundtable.gateway.ratelimit import MemoryRateLimiter, RedisRateLimiter
from tests.conftest import FakeClock


def limiter(clock: FakeClock) -> MemoryRateLimiter:
    return MemoryRateLimiter(clock=clock, sleep=clock.sleep)


# ---------------------------------------------------------------- in-memory


async def test_token_bucket_allows_burst_then_refills(clock: FakeClock) -> None:
    rl = limiter(clock)
    limits = RateLimitConfig(rpm=3)
    for _ in range(3):
        assert (await rl.try_acquire("p", limits)).allowed
    denied = await rl.try_acquire("p", limits)
    assert not denied.allowed
    assert denied.reason == "rpm"
    assert denied.wait_s == pytest.approx(20.0)  # one token every 60/3 seconds

    clock.now += 20.0
    assert (await rl.try_acquire("p", limits)).allowed
    assert not (await rl.try_acquire("p", limits)).allowed


async def test_daily_cap_resets_at_utc_midnight(clock: FakeClock) -> None:
    rl = limiter(clock)
    limits = RateLimitConfig(rpd=2)
    assert (await rl.try_acquire("p", limits)).allowed
    assert (await rl.try_acquire("p", limits)).allowed
    denied = await rl.try_acquire("p", limits)
    assert (denied.allowed, denied.reason) == (False, "rpd")
    seconds_to_midnight = 86_400 - (clock.now % 86_400)
    assert denied.wait_s == pytest.approx(seconds_to_midnight)
    assert await rl.used_today("p") == 2

    clock.now += seconds_to_midnight + 1
    assert await rl.used_today("p") == 0
    assert (await rl.try_acquire("p", limits)).allowed


async def test_denied_request_consumes_nothing(clock: FakeClock) -> None:
    rl = limiter(clock)
    limits = RateLimitConfig(rpm=1, rpd=1)
    assert (await rl.try_acquire("p", limits)).allowed
    clock.now += 60  # bucket refilled, but the daily cap is spent
    assert (await rl.try_acquire("p", limits)).reason == "rpd"
    # The rpd denial must not have taken the refilled rpm token.
    assert (await rl.try_acquire("p", RateLimitConfig(rpm=1))).allowed


async def test_penalize_sets_cooldown_and_never_shortens(clock: FakeClock) -> None:
    rl = limiter(clock)
    unlimited = RateLimitConfig()
    await rl.penalize("p", 30)
    await rl.penalize("p", 5)  # shorter penalty must not cut the existing one
    denied = await rl.try_acquire("p", unlimited)
    assert (denied.allowed, denied.reason) == (False, "cooldown")
    assert await rl.cooldown_remaining("p") == pytest.approx(30)
    clock.now += 30
    assert await rl.cooldown_remaining("p") == 0
    assert (await rl.try_acquire("p", unlimited)).allowed


async def test_acquire_waits_within_budget(clock: FakeClock) -> None:
    rl = limiter(clock)
    limits = RateLimitConfig(rpm=60, max_wait_s=5)  # one token per second
    for _ in range(60):
        await rl.try_acquire("p", limits)
    decision = await rl.acquire("p", limits)
    assert decision.allowed
    assert decision.waited_s == pytest.approx(1.0)
    assert clock.slept == [pytest.approx(1.0)]


async def test_acquire_gives_up_when_wait_exceeds_budget(clock: FakeClock) -> None:
    rl = limiter(clock)
    limits = RateLimitConfig(rpm=2, max_wait_s=10)  # refill every 30s
    await rl.try_acquire("p", limits)
    await rl.try_acquire("p", limits)
    decision = await rl.acquire("p", limits)
    assert not decision.allowed
    assert decision.reason == "rpm"
    assert decision.wait_s == pytest.approx(30)
    assert clock.slept == []  # fails fast rather than sleeping uselessly


async def test_keys_are_independent(clock: FakeClock) -> None:
    rl = limiter(clock)
    limits = RateLimitConfig(rpm=1)
    assert (await rl.try_acquire("a", limits)).allowed
    assert (await rl.try_acquire("b", limits)).allowed
    assert not (await rl.try_acquire("a", limits)).allowed


# ---------------------------------------------------------------- redis


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    """A throwaway redis-server; skips the Redis tests if none is installed."""
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server not installed")
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    yield f"redis://127.0.0.1:{port}/0"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    await client.flushdb()
    yield client
    await client.aclose()


async def test_redis_bucket_and_daily_cap(redis: Redis) -> None:
    rl = RedisRateLimiter(redis)
    limits = RateLimitConfig(rpm=3, rpd=4)
    assert [(await rl.try_acquire("p", limits)).allowed for _ in range(3)] == [True] * 3
    denied = await rl.try_acquire("p", limits)
    assert denied.reason == "rpm"
    assert 19 < denied.wait_s <= 20
    assert await rl.used_today("p") == 3

    daily = RateLimitConfig(rpd=4)  # same key, daily cap only
    assert (await rl.try_acquire("p", daily)).allowed
    assert (await rl.try_acquire("p", daily)).reason == "rpd"
    assert await rl.used_today("p") == 4


async def test_redis_quota_is_shared_across_processes(redis: Redis, redis_url: str) -> None:
    """Two limiter instances (think: api and worker) hammering one quota concurrently."""
    other = Redis.from_url(redis_url)
    try:
        a, b = RedisRateLimiter(redis), RedisRateLimiter(other)
        limits = RateLimitConfig(rpm=5)
        decisions = await asyncio.gather(*(rl.try_acquire("shared", limits) for rl in [a, b] * 10))
        assert sum(d.allowed for d in decisions) == 5
    finally:
        await other.aclose()


async def test_redis_cooldown(redis: Redis) -> None:
    rl = RedisRateLimiter(redis)
    await rl.penalize("p", 30)
    await rl.penalize("p", 1)  # must not shorten
    assert 29 < await rl.cooldown_remaining("p") <= 30
    denied = await rl.try_acquire("p", RateLimitConfig())
    assert (denied.allowed, denied.reason) == (False, "cooldown")
    assert await rl.cooldown_remaining("other") == 0
