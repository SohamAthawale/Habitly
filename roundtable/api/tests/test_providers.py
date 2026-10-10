"""Provider adapters against mocked HTTP (respx)."""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from roundtable.config import ProviderConfig
from roundtable.providers.base import (
    ChatMessage,
    ChatRequest,
    ModelNotFound,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransientError,
    parse_retry_after,
)
from roundtable.providers.ollama import OllamaProvider
from roundtable.providers.openai_compat import OpenAICompatProvider

OLLAMA = "http://ollama.test"
OPENAI = "http://openai.test/v1"
MSGS = [ChatMessage(role="user", content="hi")]
S = 1_000_000_000  # ns


def ollama(**kw: object) -> OllamaProvider:
    return OllamaProvider("ollama", ProviderConfig(kind="ollama", base_url=OLLAMA, **kw))  # type: ignore[arg-type]


def compat(**kw: object) -> OpenAICompatProvider:
    cfg = ProviderConfig(kind="openai_compat", base_url=OPENAI, api_key="sk-x", **kw)  # type: ignore[arg-type]
    return OpenAICompatProvider("openrouter", cfg)


def body(route: respx.Route, i: int = 0) -> dict[str, object]:
    data: dict[str, object] = json.loads(route.calls[i].request.content)
    return data


# ---------------------------------------------------------------- Ollama


@respx.mock
async def test_ollama_chat_payload_and_timings() -> None:
    route = respx.post(f"{OLLAMA}/api/chat").respond(
        json={
            "model": "qwen3:8b",
            "message": {"role": "assistant", "content": '{"ok": true}'},
            "done": True,
            "done_reason": "stop",
            "total_duration": 10 * S,
            "load_duration": 1 * S,
            "prompt_eval_count": 20,
            "prompt_eval_duration": 1 * S,
            "eval_count": 200,
            "eval_duration": 4 * S,
        }
    )
    p = ollama(keep_alive="30m")
    req = ChatRequest(
        model="qwen3:8b",
        messages=MSGS,
        temperature=0.3,
        max_tokens=128,
        response_format="json",
        params={"think": False, "options": {"num_ctx": 8192}},
    )
    result = await p.chat(req)
    sent = body(route)
    assert sent["format"] == "json"
    assert sent["think"] is False
    assert sent["keep_alive"] == "30m"
    assert sent["stream"] is False
    assert sent["options"] == {"num_ctx": 8192, "temperature": 0.3, "num_predict": 128}
    assert result.text == '{"ok": true}'
    assert (result.prompt_tokens, result.completion_tokens) == (20, 200)
    assert result.tokens_per_s == 50.0
    assert result.load_s == 1.0
    assert result.queue_s == 4.0  # 10 - 1 load - 1 prompt - 4 decode
    assert result.finish_reason == "stop"
    await p.aclose()


@respx.mock
async def test_ollama_schema_format_passes_through() -> None:
    route = respx.post(f"{OLLAMA}/api/chat").respond(
        json={"model": "m", "message": {"content": "{}"}, "done": True}
    )
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    await ollama().chat(ChatRequest(model="m", messages=MSGS, response_format=schema))
    assert body(route)["format"] == schema


@respx.mock
async def test_ollama_missing_model_is_model_not_found() -> None:
    respx.post(f"{OLLAMA}/api/chat").respond(404, json={"error": "model 'x' not found"})
    with pytest.raises(ModelNotFound, match="model 'x' not found"):
        await ollama().chat(ChatRequest(model="x", messages=MSGS))


@respx.mock
async def test_ollama_read_timeout_and_connect_error() -> None:
    route = respx.post(f"{OLLAMA}/api/chat")
    route.side_effect = httpx.ReadTimeout("slow")
    with pytest.raises(ProviderTimeout):
        await ollama().chat(ChatRequest(model="m", messages=MSGS))
    route.side_effect = httpx.ConnectError("refused")
    with pytest.raises(TransientError):
        await ollama().chat(ChatRequest(model="m", messages=MSGS))


@respx.mock
async def test_ollama_stream() -> None:
    lines = [
        {"model": "m", "message": {"content": "Hel"}, "done": False},
        {"model": "m", "message": {"content": "lo"}, "done": False},
        {
            "model": "m",
            "message": {"content": ""},
            "done": True,
            "eval_count": 2,
            "eval_duration": S // 2,
            "total_duration": S,
            "load_duration": 0,
            "prompt_eval_duration": S // 2,
        },
    ]
    respx.post(f"{OLLAMA}/api/chat").respond(
        content="\n".join(json.dumps(x) for x in lines).encode()
    )
    chunks = [c async for c in ollama().stream(ChatRequest(model="m", messages=MSGS))]
    assert [c.delta for c in chunks[:-1]] == ["Hel", "lo"]
    final = chunks[-1].result
    assert final is not None
    assert final.text == "Hello"
    assert final.tokens_per_s == 4.0
    assert final.ttft_s is not None


@respx.mock
async def test_ollama_stream_error_line() -> None:
    respx.post(f"{OLLAMA}/api/chat").respond(content=b'{"error": "out of memory"}\n')
    with pytest.raises(TransientError, match="out of memory"):
        async for _ in ollama().stream(ChatRequest(model="m", messages=MSGS)):
            pass


@respx.mock
async def test_ollama_health_reports_pulled_and_loaded() -> None:
    respx.get(f"{OLLAMA}/api/version").respond(json={"version": "0.40.2"})
    respx.get(f"{OLLAMA}/api/tags").respond(
        json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.1:8b"}]}
    )
    respx.get(f"{OLLAMA}/api/ps").respond(json={"models": [{"name": "qwen3:8b"}]})
    health = await ollama().health()
    assert health.ok
    assert health.models == ["qwen3:8b", "llama3.1:8b"]
    assert "0.40.2" in health.detail
    assert "loaded: qwen3:8b" in health.detail


@respx.mock
async def test_ollama_health_down() -> None:
    respx.get(f"{OLLAMA}/api/version").side_effect = httpx.ConnectError("refused")
    health = await ollama().health()
    assert not health.ok


# ---------------------------------------------------------------- OpenAI-compatible


def completion(text: str = "answer", **extra: object) -> dict[str, object]:
    return {
        "model": "served/model:free",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        **extra,
    }


@respx.mock
async def test_compat_chat_payload_and_result() -> None:
    route = respx.post(f"{OPENAI}/chat/completions").respond(json=completion())
    p = compat(server_fallback=True, max_server_fallback=3, headers={"X-Title": "Roundtable"})
    req = ChatRequest(
        model="a:free",
        messages=MSGS,
        response_format="json",
        fallback_models=["b:free", "c:free", "d:free"],
        max_tokens=64,
    )
    result = await p.chat(req)
    sent = body(route)
    headers = route.calls[0].request.headers
    assert headers["authorization"] == "Bearer sk-x"
    assert headers["x-title"] == "Roundtable"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["models"] == ["a:free", "b:free", "c:free"]  # capped at 3
    assert sent["max_tokens"] == 64
    assert result.text == "answer"
    assert result.model == "served/model:free"  # the model that actually answered
    assert (result.prompt_tokens, result.completion_tokens) == (12, 3)


@respx.mock
async def test_compat_json_schema_and_no_server_fallback_by_default() -> None:
    route = respx.post(f"{OPENAI}/chat/completions").respond(json=completion())
    schema = {"type": "object"}
    req = ChatRequest(model="m", messages=MSGS, response_format=schema, fallback_models=["n"])
    await compat().chat(req)
    sent = body(route)
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": schema},
    }
    assert "models" not in sent


@respx.mock
async def test_compat_429_with_retry_after() -> None:
    respx.post(f"{OPENAI}/chat/completions").respond(
        429, json={"error": {"message": "slow down"}}, headers={"Retry-After": "7"}
    )
    with pytest.raises(RateLimited) as info:
        await compat().chat(ChatRequest(model="m", messages=MSGS))
    assert info.value.retry_after_s == 7.0
    assert "slow down" in str(info.value)


@respx.mock
async def test_compat_error_inside_200_body() -> None:
    respx.post(f"{OPENAI}/chat/completions").respond(
        json={"error": {"code": 429, "message": "free-models-per-day exceeded"}}
    )
    with pytest.raises(RateLimited, match="free-models-per-day"):
        await compat().chat(ChatRequest(model="m", messages=MSGS))


@respx.mock
async def test_compat_server_error_is_transient_and_empty_is_not() -> None:
    route = respx.post(f"{OPENAI}/chat/completions")
    route.respond(503, text="upstream down")
    with pytest.raises(TransientError):
        await compat().chat(ChatRequest(model="m", messages=MSGS))
    route.respond(json=completion(text="  "))
    with pytest.raises(ProviderError, match="empty completion") as info:
        await compat().chat(ChatRequest(model="m", messages=MSGS))
    assert not info.value.retryable


@respx.mock
async def test_compat_401_is_not_retryable() -> None:
    respx.post(f"{OPENAI}/chat/completions").respond(401, json={"error": {"message": "bad key"}})
    with pytest.raises(ProviderError, match="bad key") as info:
        await compat().chat(ChatRequest(model="m", messages=MSGS))
    assert not info.value.retryable
    assert info.value.status == 401


@respx.mock
async def test_compat_stream_with_comments_usage_and_done() -> None:
    events = [
        ": OPENROUTER PROCESSING",
        "",
        'data: {"model": "m2", "choices": [{"delta": {"content": "Hi"}}]}',
        'data: {"choices": [{"delta": {"content": " there"}, "finish_reason": "stop"}]}',
        'data: {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}',
        "data: [DONE]",
    ]
    respx.post(f"{OPENAI}/chat/completions").respond(content="\n".join(events).encode())
    chunks = [c async for c in compat().stream(ChatRequest(model="m", messages=MSGS))]
    assert [c.delta for c in chunks[:-1]] == ["Hi", " there"]
    final = chunks[-1].result
    assert final is not None
    assert (final.text, final.model, final.finish_reason) == ("Hi there", "m2", "stop")
    assert final.completion_tokens == 2


@respx.mock
async def test_compat_stream_error_chunk() -> None:
    events = 'data: {"error": {"code": 502, "message": "provider died"}}\n'
    respx.post(f"{OPENAI}/chat/completions").respond(content=events.encode())
    with pytest.raises(TransientError, match="provider died"):
        async for _ in compat().stream(ChatRequest(model="m", messages=MSGS)):
            pass


@respx.mock
async def test_compat_list_models() -> None:
    respx.get(f"{OPENAI}/models").respond(json={"data": [{"id": "a:free"}, {"id": "b"}]})
    assert await compat().list_models() == ["a:free", "b"]


def test_parse_retry_after_variants() -> None:
    assert parse_retry_after(httpx.Headers({"retry-after": "3"})) == 3.0
    now = time.time()
    reset_ms = str(int((now + 42) * 1000))
    assert parse_retry_after(httpx.Headers({"x-ratelimit-reset": reset_ms}), now) == pytest.approx(
        42, abs=0.01
    )
    assert parse_retry_after(httpx.Headers({})) is None
