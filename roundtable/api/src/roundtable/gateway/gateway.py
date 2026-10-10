"""The LLM gateway: the only path from Roundtable code to a model.

For each call it walks the role's model preference list and, per model:

1. checks the model's own cooldown (set by a model-specific 429);
2. waits up to ``rate_limit.max_wait_s`` for the provider's rate limiter, else falls back;
3. holds an in-process concurrency slot if ``max_concurrency`` is set;
4. calls the provider, retrying *transient* failures with jittered backoff;
5. on 429 sets a cooldown (provider-wide when the provider said when to come back,
   model-only otherwise) and falls back; on timeout or any other error falls back.

Every attempt, successful or not, is written to the usage sink.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from roundtable.config import AppConfig, ModelRef
from roundtable.gateway.ratelimit import RateLimiter
from roundtable.gateway.usage import UsageRecord, UsageSink, UsageStatus
from roundtable.providers.base import (
    BaseProvider,
    ChatMessage,
    ChatRequest,
    ChatResult,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    ResponseFormat,
    StreamChunk,
)
from roundtable.providers.registry import ProviderRegistry


class AllModelsFailed(ProviderError):
    """Every model in the preference list failed or was rate limited."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("all models failed: " + " | ".join(errors) if errors else "no models")
        self.errors = errors

    @property
    def kind(self) -> str:
        return "all_failed"


@dataclass(frozen=True, slots=True)
class CallContext:
    """Who is calling, for usage attribution."""

    role: str = "default"
    run_id: str | None = None
    agent_id: str | None = None


@dataclass(slots=True)
class Attempt:
    """One provider call: a primary model plus same-provider server-side fallbacks."""

    provider: str
    model: str
    fallbacks: list[str] = field(default_factory=list)


class _Skip(Exception):
    """Internal: move on to the next attempt."""


class LLMGateway:
    """Fallback, rate limiting, retries and usage logging around the provider adapters."""

    def __init__(
        self,
        config: AppConfig,
        registry: ProviderRegistry,
        limiter: RateLimiter,
        usage: UsageSink,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._registry = registry
        self._limiter = limiter
        self._usage = usage
        self._sleep = sleep
        self._slots: dict[str, asyncio.Semaphore] = {
            name: asyncio.Semaphore(p.max_concurrency)
            for name, p in config.providers.items()
            if p.max_concurrency
        }

    # ------------------------------------------------------------------ planning

    def resolve(self, role: str, models: Sequence[str | ModelRef] | None) -> list[ModelRef]:
        """Explicit ``models`` win; otherwise the role's preference list (deduplicated)."""
        refs = (
            [m if isinstance(m, ModelRef) else ModelRef.parse(m) for m in models]
            if models
            else self._config.preferences_for(role)
        )
        return list(dict.fromkeys(refs))

    def plan_attempts(self, refs: Sequence[ModelRef]) -> list[Attempt]:
        """Group consecutive same-provider refs into one call where server fallback is on."""
        attempts: list[Attempt] = []
        for ref in refs:
            pcfg = self._config.providers.get(ref.provider)
            prev = attempts[-1] if attempts else None
            if (
                pcfg is not None
                and pcfg.server_fallback
                and prev is not None
                and prev.provider == ref.provider
                and len(prev.fallbacks) + 1 < pcfg.max_server_fallback
            ):
                prev.fallbacks.append(ref.model)
            else:
                attempts.append(Attempt(ref.provider, ref.model))
        return attempts

    def _request(
        self, att: Attempt, messages: list[ChatMessage], opts: dict[str, Any]
    ) -> ChatRequest:
        mcfg = self._config.model_config_for(ModelRef(att.provider, att.model))
        params = {**(mcfg.params if mcfg else {}), **(opts.pop("params", None) or {})}
        return ChatRequest(
            model=att.model, messages=messages, fallback_models=att.fallbacks, params=params, **opts
        )

    # ------------------------------------------------------------------ public API

    async def complete(
        self,
        messages: Sequence[ChatMessage | dict[str, str]],
        *,
        ctx: CallContext | None = None,
        models: Sequence[str | ModelRef] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: ResponseFormat = None,
        params: dict[str, Any] | None = None,
    ) -> ChatResult:
        """Run a completion with fallback; raises ``AllModelsFailed`` if nothing answers."""
        ctx = ctx or CallContext()
        msgs = _messages(messages)
        opts: dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": response_format,
        }
        errors: list[str] = []
        attempt_no = 0
        for att in self.plan_attempts(self.resolve(ctx.role, models)):
            try:
                provider = await self._admit(att, ctx, errors)
            except _Skip:
                continue
            req = self._request(att, msgs, {**opts, "params": params})
            tries = 0
            while True:
                tries += 1
                attempt_no += 1
                try:
                    waited = await self._acquire(provider, att, ctx, attempt_no, errors)
                    start = time.perf_counter()
                    async with self._slot(att.provider):
                        waited += time.perf_counter() - start
                        result = await provider.chat(req)
                except _Skip:
                    break
                except ProviderError as exc:
                    if await self._on_error(provider, att, ctx, attempt_no, exc, tries, errors):
                        continue
                    break
                return await self._on_success(att, ctx, attempt_no, result, waited)
        raise AllModelsFailed(errors)

    async def stream(
        self,
        messages: Sequence[ChatMessage | dict[str, str]],
        *,
        ctx: CallContext | None = None,
        models: Sequence[str | ModelRef] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: ResponseFormat = None,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream with fallback. Falls back only before the first token is emitted."""
        ctx = ctx or CallContext()
        msgs = _messages(messages)
        opts: dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": response_format,
        }
        errors: list[str] = []
        attempt_no = 0
        for att in self.plan_attempts(self.resolve(ctx.role, models)):
            try:
                provider = await self._admit(att, ctx, errors)
            except _Skip:
                continue
            req = self._request(att, msgs, {**opts, "params": params})
            tries = 0
            while True:
                tries += 1
                attempt_no += 1
                emitted = False
                try:
                    waited = await self._acquire(provider, att, ctx, attempt_no, errors)
                    start = time.perf_counter()
                    async with self._slot(att.provider):
                        waited += time.perf_counter() - start
                        async for chunk in provider.stream(req):
                            if chunk.result is not None:
                                chunk.result = await self._on_success(
                                    att, ctx, attempt_no, chunk.result, waited
                                )
                                yield chunk
                                return
                            emitted = True
                            yield chunk
                except _Skip:
                    break
                except ProviderError as exc:
                    if emitted:
                        await self._record(att, ctx, attempt_no, _status(exc), error=str(exc))
                        raise
                    if await self._on_error(provider, att, ctx, attempt_no, exc, tries, errors):
                        continue
                    break
                # Provider stream ended without a result chunk: treat as a failure.
                break
        raise AllModelsFailed(errors)

    # ------------------------------------------------------------------ steps

    async def _admit(self, att: Attempt, ctx: CallContext, errors: list[str]) -> BaseProvider:
        """Resolve the provider and check model cooldown; raises ``_Skip`` if unusable."""
        try:
            provider = self._registry.get(att.provider)
        except ProviderUnavailable as exc:
            errors.append(str(exc))
            raise _Skip from exc
        remaining = await self._limiter.cooldown_remaining(_model_key(att))
        if remaining > 0:
            errors.append(f"{att.provider}/{att.model}: model cooling down {remaining:.0f}s")
            raise _Skip
        return provider

    async def _acquire(
        self,
        provider: BaseProvider,
        att: Attempt,
        ctx: CallContext,
        attempt_no: int,
        errors: list[str],
    ) -> float:
        """Wait for a rate-limit token; on denial record it and raise ``_Skip``."""
        decision = await self._limiter.acquire(att.provider, provider.config.rate_limit)
        if decision.allowed:
            return decision.waited_s
        msg = (
            f"{att.provider}/{att.model}: local {decision.reason} limit, "
            f"next slot in {decision.wait_s:.0f}s"
        )
        errors.append(msg)
        await self._record(
            att, ctx, attempt_no, "rate_limited", error=msg, limiter_wait_s=decision.waited_s
        )
        raise _Skip

    async def _on_error(
        self,
        provider: BaseProvider,
        att: Attempt,
        ctx: CallContext,
        attempt_no: int,
        exc: ProviderError,
        tries: int,
        errors: list[str],
    ) -> bool:
        """Record the failure; return True to retry the same model, False to fall back."""
        await self._record(att, ctx, attempt_no, _status(exc), error=str(exc))
        pcfg = provider.config
        if isinstance(exc, RateLimited):
            if exc.retry_after_s is not None:
                await self._limiter.penalize(att.provider, exc.retry_after_s)
            else:
                await self._limiter.penalize(_model_key(att), pcfg.rate_limit.default_cooldown_s)
            errors.append(str(exc))
            return False
        if exc.retryable and tries <= pcfg.max_retries:
            base = pcfg.retry_backoff_s * (2 ** (tries - 1))
            await self._sleep(base * random.uniform(0.5, 1.0))
            return True
        errors.append(str(exc))
        return False

    async def _on_success(
        self, att: Attempt, ctx: CallContext, attempt_no: int, result: ChatResult, waited: float
    ) -> ChatResult:
        if not result.cost_usd:
            result.cost_usd = self._cost(ModelRef(att.provider, result.model), result)
        await self._record(
            att,
            ctx,
            attempt_no,
            "ok",
            result=result,
            limiter_wait_s=waited,
        )
        return result

    def _cost(self, ref: ModelRef, result: ChatResult) -> float:
        mcfg = self._config.model_config_for(ref)
        if mcfg is None:
            return 0.0
        return (
            (result.prompt_tokens or 0) * mcfg.input_cost_per_mtok
            + (result.completion_tokens or 0) * mcfg.output_cost_per_mtok
        ) / 1_000_000

    @asynccontextmanager
    async def _slot(self, provider: str) -> AsyncIterator[None]:
        sem = self._slots.get(provider)
        if sem is None:
            yield
            return
        async with sem:
            yield

    async def _record(
        self,
        att: Attempt,
        ctx: CallContext,
        attempt_no: int,
        status: UsageStatus,
        *,
        result: ChatResult | None = None,
        error: str | None = None,
        limiter_wait_s: float = 0.0,
    ) -> None:
        rec = UsageRecord(
            run_id=ctx.run_id,
            agent_id=ctx.agent_id,
            role=ctx.role,
            provider=att.provider,
            requested_model=att.model,
            model=result.model if result else att.model,
            attempt=attempt_no,
            status=status,
            error=error,
            limiter_wait_s=round(limiter_wait_s, 4),
        )
        if result is not None:
            rec = rec.model_copy(
                update={
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "latency_s": result.latency_s,
                    "load_s": result.load_s,
                    "queue_s": result.queue_s,
                    "ttft_s": result.ttft_s,
                    "tokens_per_s": result.tokens_per_s,
                    "cost_usd": result.cost_usd,
                }
            )
        await self._usage.record(rec)


def _model_key(att: Attempt) -> str:
    return f"{att.provider}/{att.model}"


def _status(exc: ProviderError) -> UsageStatus:
    kind = exc.kind
    if kind in ("transient", "timeout", "rate_limited", "model_not_found", "unavailable"):
        return kind  # type: ignore[return-value]
    return "error"


def _messages(messages: Sequence[ChatMessage | dict[str, str]]) -> list[ChatMessage]:
    return [m if isinstance(m, ChatMessage) else ChatMessage.model_validate(m) for m in messages]
