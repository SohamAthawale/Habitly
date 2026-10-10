"""Model provider adapters behind one interface."""

from roundtable.providers.base import (
    BaseProvider,
    ChatMessage,
    ChatRequest,
    ChatResult,
    ModelNotFound,
    ProviderError,
    ProviderHealth,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    StreamChunk,
    TransientError,
)
from roundtable.providers.registry import ProviderRegistry, build_provider

__all__ = [
    "BaseProvider",
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "ModelNotFound",
    "ProviderError",
    "ProviderHealth",
    "ProviderRegistry",
    "ProviderTimeout",
    "ProviderUnavailable",
    "RateLimited",
    "StreamChunk",
    "TransientError",
    "build_provider",
]
