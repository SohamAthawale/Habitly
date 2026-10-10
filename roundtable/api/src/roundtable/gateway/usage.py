"""Usage records: one per LLM attempt, successful or not.

M1 ships logging and in-memory sinks; M6 adds a Postgres sink behind the same interface.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

UsageStatus = Literal[
    "ok", "error", "transient", "timeout", "rate_limited", "model_not_found", "unavailable"
]

logger = logging.getLogger("roundtable.usage")


class UsageRecord(BaseModel):
    """What the gateway logs for every attempt."""

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str | None = None
    agent_id: str | None = None
    role: str = "default"
    provider: str
    requested_model: str
    model: str = Field(description="Model that answered; equals requested_model on failure")
    attempt: int = Field(ge=1, description="1-based position in this call's fallback sequence")
    status: UsageStatus
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_s: float = 0.0
    limiter_wait_s: float = 0.0
    load_s: float | None = None
    queue_s: float | None = None
    ttft_s: float | None = None
    tokens_per_s: float | None = None
    cost_usd: float = 0.0


class UsageSink(Protocol):
    """Anything that can persist usage records."""

    async def record(self, rec: UsageRecord) -> None:
        """Persist one record. Must not raise for transient storage problems."""
        ...


class LoggingUsageSink:
    """Emits each record as a structured log line."""

    async def record(self, rec: UsageRecord) -> None:
        """Log ``rec`` at INFO (success) or WARNING (failure)."""
        level = logging.INFO if rec.status == "ok" else logging.WARNING
        logger.log(level, "llm_call", extra={"usage": rec.model_dump(mode="json")})


class MemoryUsageSink:
    """Keeps records in a list; used by tests and the CLI summary."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, rec: UsageRecord) -> None:
        """Append ``rec``."""
        self.records.append(rec)


class FanoutUsageSink:
    """Sends each record to several sinks; one failing sink does not block the others."""

    def __init__(self, *sinks: UsageSink) -> None:
        self._sinks = sinks

    async def record(self, rec: UsageRecord) -> None:
        """Forward ``rec`` to every sink, logging (not raising) sink failures."""
        for sink in self._sinks:
            try:
                await sink.record(rec)
            except Exception:
                logger.exception("usage sink %s failed", type(sink).__name__)
