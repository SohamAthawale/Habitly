"""Config loading, env interpolation and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from roundtable.config import ModelRef, expand_env, load_app_config
from tests.conftest import make_config

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "roundtable.yaml"


def test_model_ref_splits_on_first_colon_only() -> None:
    assert ModelRef.parse("ollama:qwen3:8b") == ModelRef("ollama", "qwen3:8b")
    ref = ModelRef.parse("openrouter:meta-llama/llama-3.3-70b-instruct:free")
    assert ref.provider == "openrouter"
    assert ref.model == "meta-llama/llama-3.3-70b-instruct:free"
    assert str(ref) == "openrouter:meta-llama/llama-3.3-70b-instruct:free"


@pytest.mark.parametrize("bad", ["qwen3", ":model", "provider:", ""])
def test_model_ref_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError, match="provider:model"):
        ModelRef.parse(bad)


def test_expand_env_defaults_and_missing() -> None:
    env = {"SET": "value", "EMPTY": ""}
    assert expand_env("${SET}", env) == "value"
    assert expand_env("${UNSET:-fallback}", env) == "fallback"
    assert expand_env("${EMPTY:-fallback}", env) == "fallback"
    assert expand_env("${UNSET}", env) == ""
    assert expand_env({"a": ["x-${SET}-y"]}, env) == {"a": ["x-value-y"]}


def test_shipped_config_loads_with_no_env() -> None:
    cfg = load_app_config(REPO_CONFIG, env={})
    assert cfg.limits.max_agents == 12
    assert cfg.providers["openrouter"].rate_limit.rpm == 20
    assert cfg.providers["openrouter"].rate_limit.rpd == 50
    # Without a key OpenRouter is configured but unusable, with a reason.
    assert cfg.providers["openrouter"].unusable_reason == "API key not set"
    assert cfg.providers["custom"].unusable_reason == "disabled in config"
    assert cfg.providers["ollama"].usable
    assert cfg.preferences_for("unknown-role")[0] == ModelRef("ollama", "qwen3:8b")
    assert cfg.preferences_for("captain")[0].provider == "openrouter"


def test_shipped_config_reads_env_overrides() -> None:
    env = {
        "OPENROUTER_API_KEY": "sk-test",
        "OPENROUTER_RPD": "1000",
        "OLLAMA_MODEL_PRIMARY": "gemma3:12b",
        "OLLAMA_NUM_PARALLEL": "2",
    }
    cfg = load_app_config(REPO_CONFIG, env=env)
    openrouter = cfg.providers["openrouter"]
    assert openrouter.usable
    assert openrouter.api_key is not None
    assert openrouter.api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(cfg)  # secrets never show up in reprs or logs
    assert openrouter.rate_limit.rpd == 1000
    assert cfg.providers["ollama"].max_concurrency == 2
    assert cfg.preferences_for("default")[0] == ModelRef("ollama", "gemma3:12b")


def test_rejects_unknown_provider_in_preferences() -> None:
    with pytest.raises(ValidationError, match="unknown provider 'nope'"):
        make_config(preferences={"default": ["nope:x"]})


def test_requires_default_preferences() -> None:
    with pytest.raises(ValidationError, match="'default'"):
        make_config(preferences={"captain": ["local:a"]})


def test_rejects_team_sizing_above_max_agents() -> None:
    with pytest.raises(ValidationError, match=r"exceeds limits\.max_agents"):
        make_config(limits={"max_agents": 4}, team_sizing={"research": [2, 8]})


def test_rejects_inverted_team_sizing() -> None:
    with pytest.raises(ValidationError, match="min <= max"):
        make_config(team_sizing={"hard": [6, 4]})


def test_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        make_config(limitz={})
