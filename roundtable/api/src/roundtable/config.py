"""Configuration loading.

Two sources, on purpose:

* **Environment** (`Settings`): secrets, URLs and deployment knobs. Never committed.
* **YAML** (`AppConfig`): behaviour such as rate limits, model preference lists and team
  sizing. YAML values may reference the environment with ``${VAR}`` or ``${VAR:-default}``,
  which keeps secrets out of the file while keeping one place to read the whole setup.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Complexity = Literal["trivial", "standard", "hard", "research"]

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class Settings(BaseSettings):
    """Process-level settings read from environment variables."""

    model_config = SettingsConfigDict(extra="ignore")

    roundtable_config: Path = Path("config/roundtable.yaml")
    database_url: str = "postgresql://roundtable:roundtable@localhost:5432/roundtable"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"
    log_json: bool = True


@dataclass(frozen=True, slots=True)
class ModelRef:
    """A model addressed as ``provider:model`` (split on the first colon only).

    Ollama tags (``qwen3:8b``) and OpenRouter ids (``meta-llama/x:free``) contain colons
    and slashes themselves, so only the first colon separates the provider name.
    """

    provider: str
    model: str

    @classmethod
    def parse(cls, ref: str) -> ModelRef:
        """Parse ``provider:model``; raises ``ValueError`` on malformed input."""
        provider, sep, model = ref.partition(":")
        if not sep or not provider or not model:
            raise ValueError(f"model ref must look like 'provider:model', got {ref!r}")
        return cls(provider=provider.strip(), model=model.strip())

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RateLimitConfig(_Strict):
    """Per-provider limits. ``None`` means unlimited on that axis."""

    rpm: int | None = Field(default=None, gt=0, description="Token bucket size and refill/min")
    rpd: int | None = Field(default=None, gt=0, description="Requests per UTC day")
    max_wait_s: float = Field(
        default=20.0, ge=0, description="Longest wait for a token before falling back"
    )
    default_cooldown_s: float = Field(
        default=30.0, ge=0, description="Cooldown after a 429 that carried no Retry-After"
    )


class ProviderConfig(_Strict):
    """One configured provider endpoint."""

    kind: Literal["ollama", "openai_compat"]
    base_url: str = ""
    api_key: SecretStr | None = None
    requires_api_key: bool = False
    enabled: bool = True
    required: bool = Field(default=False, description="Readiness fails when this is down")
    timeout_s: float = Field(default=120.0, gt=0)
    connect_timeout_s: float = Field(default=5.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10, description="Retries on transient errors")
    retry_backoff_s: float = Field(default=0.5, ge=0)
    max_concurrency: int | None = Field(default=None, gt=0)
    rate_limit: RateLimitConfig = RateLimitConfig()
    headers: dict[str, str] = Field(default_factory=dict)
    server_fallback: bool = Field(
        default=False, description="Send the per-request fallback list (OpenRouter `models`)"
    )
    max_server_fallback: int = Field(default=3, ge=1)
    json_mode: bool = True
    keep_alive: str | None = None

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, v: Any) -> Any:
        return None if v in ("", None) else v

    @property
    def usable(self) -> bool:
        """True when enabled and every prerequisite (URL, key) is present."""
        return self.unusable_reason is None

    @property
    def unusable_reason(self) -> str | None:
        """Why the provider cannot be used, or ``None`` if it can."""
        if not self.enabled:
            return "disabled in config"
        if not self.base_url:
            return "no base_url configured"
        if self.requires_api_key and self.api_key is None:
            return "API key not set"
        return None


class ModelConfig(_Strict):
    """Per-model settings. Models not listed here still work with defaults."""

    ref: str
    params: dict[str, Any] = Field(default_factory=dict, description="Passed to the provider")
    input_cost_per_mtok: float = Field(default=0.0, ge=0)
    output_cost_per_mtok: float = Field(default=0.0, ge=0)

    @field_validator("ref")
    @classmethod
    def _valid_ref(cls, v: str) -> str:
        ModelRef.parse(v)
        return v


class LimitsConfig(_Strict):
    """Global limits for team size, debate rounds and request spend."""

    max_agents: int = Field(default=12, ge=1, le=64)
    max_rounds: int = Field(default=2, ge=0, le=6)
    request_budget: int = Field(default=60, ge=1)


def _default_team_sizing() -> dict[Complexity, tuple[int, int]]:
    return {"trivial": (0, 0), "standard": (2, 4), "hard": (4, 6), "research": (6, 12)}


class AppConfig(_Strict):
    """Validated contents of ``roundtable.yaml``."""

    limits: LimitsConfig = LimitsConfig()
    team_sizing: dict[Complexity, tuple[int, int]] = Field(default_factory=_default_team_sizing)
    providers: dict[str, ProviderConfig]
    models: list[ModelConfig] = Field(default_factory=list)
    model_preferences: dict[str, list[str]]

    @model_validator(mode="after")
    def _cross_checks(self) -> AppConfig:
        for complexity, (lo, hi) in self.team_sizing.items():
            if lo < 0 or lo > hi:
                raise ValueError(f"team_sizing.{complexity}: need 0 <= min <= max, got {lo}..{hi}")
            if hi > self.limits.max_agents:
                raise ValueError(
                    f"team_sizing.{complexity} max {hi} exceeds limits.max_agents "
                    f"{self.limits.max_agents}"
                )
        if "default" not in self.model_preferences:
            raise ValueError("model_preferences must define a 'default' list")
        refs = [m.ref for m in self.models]
        refs += [r for prefs in self.model_preferences.values() for r in prefs]
        for ref in refs:
            parsed = ModelRef.parse(ref)
            if parsed.provider not in self.providers:
                raise ValueError(f"model {ref!r} names unknown provider {parsed.provider!r}")
        for role, prefs in self.model_preferences.items():
            if not prefs:
                raise ValueError(f"model_preferences.{role} is empty")
        return self

    def model_config_for(self, ref: ModelRef) -> ModelConfig | None:
        """Return the per-model settings for ``ref``, if any are configured."""
        key = str(ref)
        return next((m for m in self.models if m.ref == key), None)

    def preferences_for(self, role: str) -> list[ModelRef]:
        """Model preference list for ``role``, falling back to ``default``."""
        prefs = self.model_preferences.get(role) or self.model_preferences["default"]
        return [ModelRef.parse(r) for r in prefs]


def expand_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:-default}`` in strings.

    An unset variable without a default becomes an empty string, so optional providers can
    be left unconfigured; ``ProviderConfig.unusable_reason`` then reports why.
    """
    source = os.environ if env is None else env
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: source.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    return value


def load_app_config(path: Path, env: dict[str, str] | None = None) -> AppConfig:
    """Read, interpolate and validate the YAML config at ``path``."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return AppConfig.model_validate(expand_env(raw, env))
