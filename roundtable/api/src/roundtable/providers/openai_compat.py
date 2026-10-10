"""Adapter for any OpenAI-compatible ``/chat/completions`` endpoint.

Used for OpenRouter and for arbitrary base URLs (vLLM, LM Studio, ScaleServe, ...).
OpenRouter specifics that are handled here, behind config flags:

* errors can arrive inside an HTTP 200 body (``{"error": {"code": 429, ...}}``) and as SSE
  chunks mid-stream, so both paths check for them;
* ``server_fallback`` sends a ``models`` list so one request (one unit of the scarce free
  quota) can try several free models server-side.
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
    StreamChunk,
    TransientError,
    error_from_status,
)


class OpenAICompatProvider(BaseProvider):
    """Talks to an OpenAI-style chat completions API."""

    def _payload(self, req: ChatRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": [m.model_dump() for m in req.messages],
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.response_format is not None and self.config.json_mode:
            if req.response_format == "json":
                body["response_format"] = {"type": "json_object"}
            else:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "schema": req.response_format},
                }
        if self.config.server_fallback and req.fallback_models:
            models = [req.model, *req.fallback_models]
            body["models"] = models[: self.config.max_server_fallback]
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        body.update(req.params)
        return body

    def _body_error(self, model: str, err: Any, headers: httpx.Headers | None) -> ProviderError:
        if isinstance(err, dict):
            code = err.get("code")
            status = code if isinstance(code, int) else 502
            message = str(err.get("message", err))
        else:
            status, message = 502, str(err)
        return error_from_status(self.name, model, status, message, headers)

    def _status_error(self, model: str, resp: httpx.Response, body: str) -> ProviderError:
        try:
            err = json.loads(body).get("error")
        except (json.JSONDecodeError, AttributeError):
            err = None
        message = err.get("message", body) if isinstance(err, dict) else body
        return error_from_status(self.name, model, resp.status_code, str(message), resp.headers)

    def _result(
        self,
        req: ChatRequest,
        data: dict[str, Any],
        text: str,
        finish: str | None,
        latency: float,
        ttft: float | None,
    ) -> ChatResult:
        usage = data.get("usage") or {}
        completion = usage.get("completion_tokens")
        decode_time = latency - (ttft or 0.0)
        tps = completion / decode_time if completion and decode_time > 0 else None
        cost = usage.get("cost")
        return ChatResult(
            text=text,
            provider=self.name,
            model=data.get("model") or req.model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=completion,
            latency_s=round(latency, 4),
            ttft_s=None if ttft is None else round(ttft, 4),
            tokens_per_s=None if tps is None else round(tps, 2),
            finish_reason=finish,
            cost_usd=float(cost) if isinstance(cost, int | float) else 0.0,
        )

    async def chat(self, req: ChatRequest) -> ChatResult:
        """Run one completion."""
        start = time.perf_counter()
        try:
            resp = await self._client.post(
                "/chat/completions", json=self._payload(req, stream=False)
            )
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, req.model) from exc
        if resp.status_code != 200:
            raise self._status_error(req.model, resp, resp.text)
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise TransientError(
                f"{self.name}/{req.model}: non-JSON response", provider=self.name, model=req.model
            ) from exc
        if data.get("error"):
            raise self._body_error(req.model, data["error"], resp.headers)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(
                f"{self.name}/{req.model}: response had no choices",
                provider=self.name,
                model=req.model,
            )
        choice = choices[0]
        if choice.get("error"):
            raise self._body_error(req.model, choice["error"], resp.headers)
        text = (choice.get("message") or {}).get("content") or ""
        if not text.strip():
            raise ProviderError(
                f"{self.name}/{req.model}: empty completion", provider=self.name, model=req.model
            )
        latency = time.perf_counter() - start
        return self._result(req, data, text, choice.get("finish_reason"), latency, None)

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Stream a completion over SSE; the final chunk carries totals and usage."""
        start = time.perf_counter()
        ttft: float | None = None
        parts: list[str] = []
        finish: str | None = None
        last: dict[str, Any] = {}
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=self._payload(req, stream=True)
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise self._status_error(req.model, resp, body)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # blank lines and ": keep-alive" comments
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    obj = json.loads(payload)
                    if obj.get("error"):
                        raise self._body_error(req.model, obj["error"], resp.headers)
                    if obj.get("model"):
                        last["model"] = obj["model"]
                    if obj.get("usage"):
                        last["usage"] = obj["usage"]
                    for choice in obj.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content") or ""
                        finish = choice.get("finish_reason") or finish
                        if delta:
                            if ttft is None:
                                ttft = time.perf_counter() - start
                            parts.append(delta)
                            yield StreamChunk(delta=delta)
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, req.model) from exc
        except json.JSONDecodeError as exc:
            raise TransientError(
                f"{self.name}/{req.model}: malformed stream chunk",
                provider=self.name,
                model=req.model,
            ) from exc
        if not parts:
            raise ProviderError(
                f"{self.name}/{req.model}: empty completion", provider=self.name, model=req.model
            )
        latency = time.perf_counter() - start
        yield StreamChunk(result=self._result(req, last, "".join(parts), finish, latency, ttft))

    async def list_models(self) -> list[str]:
        """Model ids from ``GET /models``."""
        try:
            resp = await self._client.get("/models", timeout=self.config.connect_timeout_s * 2)
        except httpx.HTTPError as exc:
            raise self._map_transport_error(exc, "-") from exc
        if resp.status_code != 200:
            raise self._status_error("-", resp, resp.text)
        return [m["id"] for m in resp.json().get("data", []) if "id" in m]
