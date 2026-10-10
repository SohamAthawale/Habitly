"""Per-provider rate limiting.

Three independent gates, checked atomically and consumed only if all pass:

* **rpm**: token bucket of size ``rpm`` refilled at ``rpm/60`` tokens per second;
* **rpd**: requests per UTC day (fixed window, matching how free tiers reset);
* **cooldown**: set after a provider answers 429, honouring its ``Retry-After``.

``RedisRateLimiter`` shares state across processes (api, worker, bots) through one Lua
script; ``MemoryRateLimiter`` has identical semantics for tests and single-process use.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from redis.asyncio import Redis

from roundtable.config import RateLimitConfig

Reason = Literal["ok", "rpm", "rpd", "cooldown"]
_DAY = 86_400.0


@dataclass(frozen=True, slots=True)
class Decision:
    """Result of a limiter check. ``wait_s`` is how long until a retry could pass."""

    allowed: bool
    wait_s: float = 0.0
    reason: Reason = "ok"
    waited_s: float = 0.0


class RateLimiter(ABC):
    """Interface shared by the Redis and in-memory limiters."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep

    @abstractmethod
    async def try_acquire(self, key: str, limits: RateLimitConfig) -> Decision:
        """Consume one request if every gate allows it; never blocks."""

    @abstractmethod
    async def penalize(self, key: str, seconds: float) -> None:
        """Block ``key`` for ``seconds`` (e.g. after a 429)."""

    @abstractmethod
    async def used_today(self, key: str) -> int:
        """Requests counted against the daily window so far."""

    @abstractmethod
    async def cooldown_remaining(self, key: str) -> float:
        """Seconds left on ``key``'s cooldown (0 when none). Consumes nothing."""

    async def acquire(
        self, key: str, limits: RateLimitConfig, max_wait_s: float | None = None
    ) -> Decision:
        """Wait up to ``max_wait_s`` (default ``limits.max_wait_s``) for a slot.

        Returns a denied ``Decision`` immediately when the required wait exceeds the
        remaining allowance, so the caller can fall back instead of stalling.
        """
        budget = limits.max_wait_s if max_wait_s is None else max_wait_s
        start = self._monotonic()
        while True:
            decision = await self.try_acquire(key, limits)
            waited = self._monotonic() - start
            if decision.allowed:
                return Decision(True, 0.0, "ok", waited_s=waited)
            if decision.wait_s > budget - waited:
                return Decision(False, decision.wait_s, decision.reason, waited_s=waited)
            # Small floor avoids a hot loop on float rounding near a refill boundary.
            await self._sleep(max(decision.wait_s, 0.01))


@dataclass
class _Bucket:
    tokens: float
    ts: float


@dataclass
class _KeyState:
    bucket: _Bucket | None = None
    day: int = -1
    used: int = 0
    cooldown_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MemoryRateLimiter(RateLimiter):
    """In-process limiter. ``clock`` returns epoch seconds and is injectable for tests."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(monotonic=clock, sleep=sleep)
        self._clock = clock
        self._state: dict[str, _KeyState] = {}

    def _get(self, key: str) -> _KeyState:
        return self._state.setdefault(key, _KeyState())

    async def try_acquire(self, key: str, limits: RateLimitConfig) -> Decision:
        """See ``RateLimiter.try_acquire``."""
        st = self._get(key)
        async with st.lock:
            now = self._clock()
            if st.cooldown_until > now:
                return Decision(False, st.cooldown_until - now, "cooldown")
            tokens = 0.0
            if limits.rpm is not None:
                rpm = float(limits.rpm)
                if st.bucket is None:
                    st.bucket = _Bucket(tokens=rpm, ts=now)
                tokens = min(rpm, st.bucket.tokens + (now - st.bucket.ts) * rpm / 60.0)
                if tokens < 1.0:
                    return Decision(False, (1.0 - tokens) * 60.0 / rpm, "rpm")
            day = int(now // _DAY)
            if st.day != day:
                st.day, st.used = day, 0
            if limits.rpd is not None and st.used >= limits.rpd:
                return Decision(False, (day + 1) * _DAY - now, "rpd")
            if limits.rpm is not None and st.bucket is not None:
                st.bucket = _Bucket(tokens=tokens - 1.0, ts=now)
            st.used += 1
            return Decision(True)

    async def penalize(self, key: str, seconds: float) -> None:
        """See ``RateLimiter.penalize``."""
        st = self._get(key)
        st.cooldown_until = max(st.cooldown_until, self._clock() + max(0.0, seconds))

    async def cooldown_remaining(self, key: str) -> float:
        """See ``RateLimiter.cooldown_remaining``."""
        return max(0.0, self._get(key).cooldown_until - self._clock())

    async def used_today(self, key: str) -> int:
        """See ``RateLimiter.used_today``."""
        st = self._get(key)
        return st.used if st.day == int(self._clock() // _DAY) else 0


_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local cd = redis.call('PTTL', KEYS[3])
if cd > 0 then return {0, tostring(cd / 1000), 'cooldown'} end
local rpm = tonumber(ARGV[1])
local rpd = tonumber(ARGV[2])
local tokens = 0
if rpm > 0 then
  local st = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
  local prev = tonumber(st[1]) or rpm
  local ts = tonumber(st[2]) or now
  tokens = math.min(rpm, prev + math.max(0, now - ts) * rpm / 60)
  if tokens < 1 then return {0, tostring((1 - tokens) * 60 / rpm), 'rpm'} end
end
local day = math.floor(now / 86400)
local day_key = KEYS[2] .. ':' .. day
if rpd > 0 then
  local used = tonumber(redis.call('GET', day_key) or '0')
  if used >= rpd then return {0, tostring((day + 1) * 86400 - now), 'rpd'} end
end
if rpm > 0 then
  redis.call('HSET', KEYS[1], 'tokens', tostring(tokens - 1), 'ts', tostring(now))
  redis.call('EXPIRE', KEYS[1], 3600)
end
redis.call('INCR', day_key)
redis.call('EXPIRE', day_key, 172800)
return {1, '0', 'ok'}
"""


def _s(v: bytes | str) -> str:
    return v.decode() if isinstance(v, bytes) else v


class RedisRateLimiter(RateLimiter):
    """Limiter whose state lives in Redis so every process shares one quota."""

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = "rt:rl",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(sleep=sleep)
        self._redis = redis
        self._prefix = prefix
        self._script = redis.register_script(_LUA)

    def _keys(self, key: str) -> list[str]:
        # The {key} hash tag keeps all three keys in one slot on Redis Cluster.
        base = f"{self._prefix}:{{{key}}}"
        return [f"{base}:bucket", f"{base}:day", f"{base}:cooldown"]

    async def try_acquire(self, key: str, limits: RateLimitConfig) -> Decision:
        """See ``RateLimiter.try_acquire``."""
        allowed, wait, reason = await self._script(
            keys=self._keys(key), args=[limits.rpm or 0, limits.rpd or 0]
        )
        return Decision(bool(int(allowed)), float(_s(wait)), cast(Reason, _s(reason)))

    async def penalize(self, key: str, seconds: float) -> None:
        """See ``RateLimiter.penalize``. Only ever extends an existing cooldown."""
        ms = int(max(0.0, seconds) * 1000)
        if ms <= 0:
            return
        cooldown_key = self._keys(key)[2]
        current = await self._redis.pttl(cooldown_key)
        if current < ms:
            await self._redis.set(cooldown_key, "1", px=ms)

    async def cooldown_remaining(self, key: str) -> float:
        """See ``RateLimiter.cooldown_remaining``."""
        ms = await self._redis.pttl(self._keys(key)[2])
        return ms / 1000.0 if ms > 0 else 0.0

    async def used_today(self, key: str) -> int:
        """See ``RateLimiter.used_today``."""
        now = await self._redis.time()
        day = int(now[0] // _DAY)
        value = await self._redis.get(f"{self._keys(key)[1]}:{day}")
        return int(value) if value else 0
