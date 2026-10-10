"""Provider-neutral request/response types, the error taxonomy and the adapter base class."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from roundtable.config import ProviderConfig

Role = Literal["system", "user", "assistant", "tool"]
# "json" asks for any JSON object; a dict is a JSON schema the output must match.
ResponseFormat = Literal["json"] | dict[str, Any] | None


class ChatMessage(BaseModel):
    """One chat turn."""

    role: Role
    content: str


class ChatRequest(BaseModel):
    """A single model call, already resolved to one provider."""

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: ResponseFormat = None
    fallback_models: list[str] = Field(
        default_factory=list, description="Same-provider fallbacks tried server-side, if supported"
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Provider-specific extras")


class ChatResult(BaseModel):
    """A completed call with the timings and token counts the gateway logs."""

    text: str
    provider: str
    model: str = Field(description="Model that actually answered (may differ on fallback)")
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_s: float = 0.0
    ttft_s: float | None = Field(default=None, description="Time to first token (streaming)")
    tokens_per_s: float | None = None
    load_s: float | None = Field(default=None, description="Server scheduling + model load")
    queue_s: float | None = Field(default=None, description="Server wait for a parallel slot")
    finish_reason: str | None = None
    cost_usd: float = 0.0


class StreamChunk(BaseModel):
    """A streamed text delta; the last chunk has ``result`` with the totals."""

    delta: str = ""
    result: ChatResult | None = None


class ProviderHealth(BaseModel):
    """Outcome of a provider health probe."""

    ok: bool
    detail: str = ""
    models: list[str] = Field(default_factory=list)
    latency_s: float | None = None


class ProviderError(Exception):
    """Base error. ``retryable`` means the same model may succeed if tried again."""

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        model: str = "",
        status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status = status
        if retryable is not None:
            self.retryable = retryable

    @property
    def kind(self) -> str:
        """Short machine-readable error kind used in usage records."""
        return "error"


class RateLimited(ProviderError):
    """The provider rejected the call for quota reasons (HTTP 429 or equivalent)."""

    def __init__(self, message: str, *, retry_after_s: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after_s = retry_after_s

    @property
    def kind(self) -> str:
        return "rate_limited"


class ProviderTimeout(ProviderError):
    """The call exceeded its timeout. Not retried on the same model: we fall back instead."""

    @property
    def kind(self) -> str:
        return "timeout"


class ModelNotFound(ProviderError):
    """The provider does not have the requested model (e.g. not pulled into Ollama)."""

    @property
    def kind(self) -> str:
        return "model_not_found"


class ProviderUnavailable(ProviderError):
    """The provider is not usable at all (disabled, missing key, unreachable)."""

    @property
    def kind(self) -> str:
        return "unavailable"


class TransientError(ProviderError):
    """Connection reset, 5xx and similar; worth retrying the same model."""

    retryable = True

    @property
    def kind(self) -> str:
        return "transient"


class BaseProvider(ABC):
    """Shared plumbing for HTTP-based adapters: client, timeouts and error mapping."""

    def __init__(self, name: str, config: ProviderConfig, client: httpx.AsyncClient | None = None):
        self.name = name
        self.config = config
        headers = dict(config.headers)
        if config.api_key is not None:
            headers["Authorization"] = f"Bearer {config.api_key.get_secret_value()}"
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s, connect=config.connect_timeout_s),
        )

    @abstractmethod
    async def chat(self, req: ChatRequest) -> ChatResult:
        """Run one non-streaming completion."""

    @abstractmethod
    def stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Run one streaming completion; the final chunk carries ``result``."""

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Return model ids the provider can serve right now."""

    async def health(self) -> ProviderHealth:
        """Probe the provider by listing models."""
        start = time.perf_counter()
        try:
            models = await self.list_models()
        except ProviderError as exc:
            return ProviderHealth(ok=False, detail=str(exc))
        return ProviderHealth(
            ok=True,
            detail=f"{len(models)} models",
            models=models,
            latency_s=round(time.perf_counter() - start, 4),
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    def _err(self, exc_type: type[ProviderError], msg: str, model: str, **kw: Any) -> ProviderError:
        return exc_type(f"{self.name}/{model}: {msg}", provider=self.name, model=model, **kw)

    def _map_transport_error(self, exc: httpx.HTTPError, model: str) -> ProviderError:
        """Translate an httpx exception into our taxonomy."""
        if isinstance(exc, httpx.TimeoutException):
            # A connect timeout means the server is unreachable; a read timeout means slow.
            if isinstance(exc, httpx.ConnectTimeout):
                return self._err(TransientError, "connect timeout", model)
            return self._err(ProviderTimeout, f"timed out after {self.config.timeout_s}s", model)
        return self._err(TransientError, f"{type(exc).__name__}: {exc}", model)


def error_from_status(
    provider: str, model: str, status: int, message: str, headers: httpx.Headers | None = None
) -> ProviderError:
    """Map an HTTP status (or an in-body error code) to our error taxonomy."""
    text = f"{provider}/{model}: HTTP {status}: {message[:300]}"
    kw: dict[str, Any] = {"provider": provider, "model": model, "status": status}
    if status == 429:
        retry_after = parse_retry_after(headers) if headers is not None else None
        return RateLimited(text, retry_after_s=retry_after, **kw)
    if status == 404:
        return ModelNotFound(text, **kw)
    if status in (408, 409, 425, 500, 502, 503, 504, 520, 522, 524, 529):
        return TransientError(text, **kw)
    return ProviderError(text, retryable=False, **kw)


def parse_retry_after(headers: httpx.Headers, now: float | None = None) -> float | None:
    """Seconds to wait from ``Retry-After`` or OpenRouter's ``X-RateLimit-Reset`` (epoch ms)."""
    value = headers.get("retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    reset = headers.get("x-ratelimit-reset")
    if reset:
        try:
            reset_s = float(reset) / 1000.0
        except ValueError:
            return None
        return max(0.0, reset_s - (time.time() if now is None else now))
    return None
