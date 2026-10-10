"""Ollama adapter using the native ``/api/chat`` endpoint.

The native API (rather than Ollama's OpenAI shim) returns exact token counts and
nanosecond timings, which is how we measure tokens/sec, model load time and the time a
request waited for a free parallel slot (``OLLAMA_NUM_PARALLEL``).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from roundtable.providers.base import (
    BaseProvider,
    ChatRequest,
    ChatResult,
    ProviderError,
    ProviderHealth,
    StreamChunk,
    error_from_status,
)

_NS = 1_000_000_000


class OllamaProvider(BaseProvider):
    """Talks to a local or remote Ollama server."""

    def _payload(self, req: ChatRequest, *, stream: bool) -> dict[str, Any]:
        params = dict(req.params)
        options: dict[str, Any] = dict(params.pop("options", {}) or {})
        if req.temperature is not None:
            options["temperature"] = req.temperature
        if req.max_tokens is not None:
            options["num_predict"] = req.max_tokens
        body: dict[str, Any] = {
            "model": req.model,
            "messages": [m.model_dump() for m in req.messages],
            "stream": stream,
        }
        if req.response_format is not None and self.config.json_mode:
            body["format"] = req.response_format
        if self.config.keep_alive:
            body["keep_alive"] = self.config.keep_alive
        body.update(params)  # e.g. {"think": false}
        if options:
            body["options"] = options
        return body

    def _result(
        self, req: ChatRequest, final: dict[str, Any], text: str, latency: float, ttft: float | None
    ) -> ChatResult:
        total = final.get("total_duration")
        load = final.get("load_duration")
        prompt_eval = final.get("prompt_eval_duration")
        eval_ns = final.get("eval_duration")
        eval_count = final.get("eval_count")
        tps = eval_count / (eval_ns / _NS) if eval_count and eval_ns else None
        queue = None
        if total is not None and load is not None and prompt_eval is not None and eval_ns:
            # Time inside Ollama after scheduling that was neither prompt eval nor decode:
            # almost entirely waiting for a free slot when NUM_PARALLEL is saturated.
            queue = max(0.0, (total - load - prompt_eval - eval_ns) / _NS)
        return ChatResult(
            text=text,
            provider=self.name,
            model=final.get("model") or req.model,
            prompt_tokens=final.get("prompt_eval_count"),
            completion_tokens=eval_count,
            latency_s=round(latency, 4),
            ttft_s=None if ttft is None else round(ttft, 4),
            tokens_per_s=None if tps is None else round(tps, 2),
            load_s=None if load is None else round(load / _NS, 4),
            queue_s=None if queue is None else round(queue, 4),
            finish_reason=final.get("done_reason"),
        )

    def _raise_for(self, model: str, status: int, body: str) -> ProviderError:
        try:
            message = json.loads(body).get("error", body)
        except (json.JSONDecodeError, AttributeError):
            message = body
        return error_from_status(self.name, model, status, str(message))

    async def chat(self, req: ChatRequest) -> ChatResult:
        """Run one completion and return the text with Ollama's timings."""
        start = time.perf_counter()
        try:
            resp = await self._client.post("/api/chat", json=self._payload(req, stream=False))
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, req.model) from exc
        if resp.status_code != 200:
            raise self._raise_for(req.model, resp.status_code, resp.text)
        data = resp.json()
        if "error" in data:
            raise self._raise_for(req.model, 500, json.dumps(data))
        text = (data.get("message") or {}).get("content", "")
        return self._result(req, data, text, time.perf_counter() - start, None)

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream a completion as NDJSON deltas; the last chunk carries the totals."""
        start = time.perf_counter()
        ttft: float | None = None
        parts: list[str] = []
        try:
            async with self._client.stream(
                "POST", "/api/chat", json=self._payload(req, stream=True)
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise self._raise_for(req.model, resp.status_code, body)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise self._raise_for(
                            req.model, 502, f"malformed chunk: {line[:200]}"
                        ) from exc
                    if "error" in obj:
                        raise self._raise_for(req.model, 500, line)
                    delta = (obj.get("message") or {}).get("content", "")
                    if delta:
                        if ttft is None:
                            ttft = time.perf_counter() - start
                        parts.append(delta)
                        yield StreamChunk(delta=delta)
                    if obj.get("done"):
                        text = "".join(parts)
                        latency = time.perf_counter() - start
                        yield StreamChunk(result=self._result(req, obj, text, latency, ttft))
                        return
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, req.model) from exc
        raise self._raise_for(req.model, 502, "stream ended without a done message")

    async def list_models(self) -> list[str]:
        """Models pulled into this Ollama server."""
        return [m["name"] for m in (await self._get_json("/api/tags")).get("models", [])]

    async def loaded_models(self) -> list[str]:
        """Models currently resident in memory (``/api/ps``)."""
        return [m["name"] for m in (await self._get_json("/api/ps")).get("models", [])]

    async def health(self) -> ProviderHealth:
        """Report version, pulled models and which are loaded in VRAM right now."""
        start = time.perf_counter()
        try:
            version = (await self._get_json("/api/version")).get("version", "?")
            models = await self.list_models()
            loaded = await self.loaded_models()
        except ProviderError as exc:
            return ProviderHealth(ok=False, detail=str(exc))
        return ProviderHealth(
            ok=True,
            detail=f"ollama {version}; {len(models)} pulled; loaded: {', '.join(loaded) or 'none'}",
            models=models,
            latency_s=round(time.perf_counter() - start, 4),
        )

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            resp = await self._client.get(path, timeout=self.config.connect_timeout_s * 2)
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, "-") from exc
        if resp.status_code != 200:
            raise self._raise_for("-", resp.status_code, resp.text)
        data: dict[str, Any] = resp.json()
        return data
