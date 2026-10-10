"""Shared test fixtures: a fake clock, a scripted fake provider and config builders."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from roundtable.config import AppConfig, ProviderConfig
from roundtable.providers.base import (
    BaseProvider,
    ChatRequest,
    ChatResult,
    ProviderError,
    StreamChunk,
)


@dataclass
class FakeClock:
    """Deterministic clock; ``sleep`` advances time instead of waiting."""

    now: float = 1_700_000_000.0  # 2023-11-14T22:13:20Z, mid-day UTC
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


Outcome = str | ProviderError


class FakeProvider(BaseProvider):
    """Provider whose responses are scripted per model, in order.

    ``script[model]`` is a list of outcomes consumed one per call: a string is returned as
    the completion text, an exception is raised. The last outcome repeats when exhausted.
    """

    def __init__(self, name: str, config: ProviderConfig, script: dict[str, list[Outcome]]):
        self.name = name
        self.config = config
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[ChatRequest] = []

    def _next(self, req: ChatRequest) -> Outcome:
        self.calls.append(req)
        outcomes = self.script.get(req.model)
        if not outcomes:
            raise ProviderError(f"no script for {req.model}", provider=self.name, model=req.model)
        return outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]

    def _result(self, req: ChatRequest, text: str) -> ChatResult:
        return ChatResult(
            text=text,
            provider=self.name,
            model=req.model,
            prompt_tokens=10,
            completion_tokens=5,
            latency_s=0.1,
        )

    async def chat(self, req: ChatRequest) -> ChatResult:
        outcome = self._next(req)
        if isinstance(outcome, ProviderError):
            raise outcome
        return self._result(req, outcome)

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        outcome = self._next(req)
        if isinstance(outcome, ProviderError):
            raise outcome
        if outcome.startswith("PARTIAL:"):
            yield StreamChunk(delta=outcome.removeprefix("PARTIAL:"))
            raise ProviderError("dropped mid-stream", provider=self.name, model=req.model)
        for word in outcome.split(" "):
            yield StreamChunk(delta=word + " ")
        yield StreamChunk(result=self._result(req, outcome))

    async def list_models(self) -> list[str]:
        return list(self.script)

    async def aclose(self) -> None:
        return None


def make_config(
    providers: dict[str, dict[str, Any]] | None = None,
    preferences: dict[str, list[str]] | None = None,
    **extra: Any,
) -> AppConfig:
    """Minimal valid ``AppConfig`` with two providers, ``local`` and ``remote``."""
    providers = providers or {
        "local": {"kind": "ollama", "base_url": "http://local", "retry_backoff_s": 0},
        "remote": {
            "kind": "openai_compat",
            "base_url": "http://remote",
            "retry_backoff_s": 0,
            "rate_limit": {"rpm": 20, "rpd": 50, "max_wait_s": 0},
        },
    }
    preferences = preferences or {"default": ["local:a", "local:b"]}
    return AppConfig.model_validate(
        {"providers": providers, "model_preferences": preferences, **extra}
    )
