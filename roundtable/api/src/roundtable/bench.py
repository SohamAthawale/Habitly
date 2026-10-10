"""Ollama concurrency benchmark.

Sends batches of concurrent streaming requests at increasing concurrency levels and reports
throughput, per-request decode speed, time to first token, and how long requests waited
for a parallel slot (``queue_s``) or for scheduling/model load (``load_s``). Use it to pick
``OLLAMA_NUM_PARALLEL`` and the provider's ``max_concurrency`` for your GPU.

    roundtable bench --model qwen3:8b --concurrency 1,2,4,8
    roundtable bench --model qwen3:8b --model llama3.1:8b   # alternate two models
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from dataclasses import asdict, dataclass

from roundtable.config import ProviderConfig
from roundtable.providers.base import ChatMessage, ChatRequest, ChatResult, ProviderError
from roundtable.providers.ollama import OllamaProvider

DEFAULT_PROMPT = (
    "Explain in about 150 words why the sky is blue, including the role of Rayleigh scattering."
)


@dataclass
class LevelReport:
    """Aggregated numbers for one concurrency level."""

    concurrency: int
    requests: int
    ok: int
    wall_s: float
    aggregate_tok_s: float
    mean_req_tok_s: float | None
    p50_latency_s: float | None
    p95_latency_s: float | None
    p50_ttft_s: float | None
    p95_ttft_s: float | None
    mean_queue_s: float | None
    mean_load_s: float | None
    errors: list[str]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 3)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


async def _one(provider: OllamaProvider, req: ChatRequest) -> ChatResult:
    async for chunk in provider.stream(req):
        if chunk.result is not None:
            return chunk.result
    raise ProviderError("stream ended without result")


async def run_level(
    provider: OllamaProvider,
    models: list[str],
    concurrency: int,
    total: int,
    req_kw: dict[str, object],
) -> LevelReport:
    """Run ``total`` requests with at most ``concurrency`` in flight."""
    sem = asyncio.Semaphore(concurrency)
    results: list[ChatResult] = []
    errors: list[str] = []

    async def task(i: int) -> None:
        req = ChatRequest(model=models[i % len(models)], **req_kw)
        async with sem:
            try:
                results.append(await _one(provider, req))
            except ProviderError as exc:
                errors.append(str(exc))

    start = time.perf_counter()
    await asyncio.gather(*(task(i) for i in range(total)))
    wall = time.perf_counter() - start
    tokens = sum(r.completion_tokens or 0 for r in results)
    return LevelReport(
        concurrency=concurrency,
        requests=total,
        ok=len(results),
        wall_s=round(wall, 3),
        aggregate_tok_s=round(tokens / wall, 2) if wall else 0.0,
        mean_req_tok_s=_mean([r.tokens_per_s for r in results if r.tokens_per_s]),
        p50_latency_s=_pct([r.latency_s for r in results], 0.5),
        p95_latency_s=_pct([r.latency_s for r in results], 0.95),
        p50_ttft_s=_pct([r.ttft_s for r in results if r.ttft_s is not None], 0.5),
        p95_ttft_s=_pct([r.ttft_s for r in results if r.ttft_s is not None], 0.95),
        mean_queue_s=_mean([r.queue_s for r in results if r.queue_s is not None]),
        mean_load_s=_mean([r.load_s for r in results if r.load_s is not None]),
        errors=errors[:5],
    )


async def _run(args: argparse.Namespace) -> list[LevelReport]:
    provider = OllamaProvider(
        "ollama", ProviderConfig(kind="ollama", base_url=args.base_url, timeout_s=args.timeout)
    )
    req_kw: dict[str, object] = {
        "messages": [ChatMessage(role="user", content=args.prompt)],
        "max_tokens": args.max_tokens,
        "temperature": 0.7,
        "params": {"think": False} if args.no_think else {},
    }
    try:
        for model in args.model:  # warm-up so model load time doesn't skew level 1
            print(f"warming up {model} ...", flush=True)
            await _one(provider, ChatRequest(model=model, **{**req_kw, "max_tokens": 8}))
        reports = []
        for level in args.concurrency:
            total = args.requests or level * 2
            print(f"concurrency {level}: {total} requests ...", flush=True)
            reports.append(await run_level(provider, args.model, level, total, req_kw))
        return reports
    finally:
        await provider.aclose()


def _print_table(reports: list[LevelReport]) -> None:
    cols = [
        ("conc", "concurrency"),
        ("ok/n", None),
        ("wall", "wall_s"),
        ("agg tok/s", "aggregate_tok_s"),
        ("req tok/s", "mean_req_tok_s"),
        ("p50 lat", "p50_latency_s"),
        ("p95 lat", "p95_latency_s"),
        ("p50 ttft", "p50_ttft_s"),
        ("queue", "mean_queue_s"),
        ("load", "mean_load_s"),
    ]
    print("  ".join(f"{c:>9}" for c, _ in cols))
    for r in reports:
        row = []
        for _, key in cols:
            val = f"{r.ok}/{r.requests}" if key is None else getattr(r, key)
            row.append(f"{'-' if val is None else val:>9}")
        print("  ".join(row))
        for err in r.errors:
            print(f"    error: {err}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    p = argparse.ArgumentParser(prog="roundtable bench", description=__doc__.splitlines()[0])
    p.add_argument(
        "--base-url", default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    )
    p.add_argument("--model", action="append", required=True, help="repeat to alternate models")
    p.add_argument(
        "--concurrency",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[1, 2, 4, 8],
        help="comma-separated levels (default 1,2,4,8)",
    )
    p.add_argument("--requests", type=int, default=0, help="requests per level (default 2x level)")
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--no-think", action="store_true", help="disable thinking for reasoning models")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = p.parse_args(argv)
    reports = asyncio.run(_run(args))
    if args.json:
        print(json.dumps([asdict(r) for r in reports], indent=2))
    else:
        _print_table(reports)
    return 0 if all(r.ok for r in reports) else 1
