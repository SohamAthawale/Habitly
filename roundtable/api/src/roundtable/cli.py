"""Command-line entry point.

M1 commands exercise the provider layer directly:

* ``roundtable providers`` - readiness of every dependency and provider;
* ``roundtable chat "question" [--role R] [--model provider:model ...] [--stream]`` - one
  call through the gateway, printing the usage records (fallbacks included);
* ``roundtable bench`` - Ollama concurrency benchmark (see ``roundtable.bench``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from roundtable.config import Settings
from roundtable.gateway import CallContext, FanoutUsageSink, LoggingUsageSink, MemoryUsageSink
from roundtable.gateway.gateway import LLMGateway
from roundtable.health import readiness
from roundtable.logging_setup import configure_logging
from roundtable.services import build_services


async def _providers() -> int:
    services = await build_services()
    try:
        report = await readiness(services)
    finally:
        await services.aclose()
    print(json.dumps(report.model_dump(), indent=2))
    return 0 if report.status != "fail" else 1


async def _chat(args: argparse.Namespace) -> int:
    services = await build_services()
    mem = MemoryUsageSink()
    gateway = LLMGateway(
        services.config,
        services.registry,
        services.limiter,
        FanoutUsageSink(mem, LoggingUsageSink()),
    )
    ctx = CallContext(role=args.role)
    messages = [{"role": "user", "content": args.prompt}]
    try:
        if args.stream:
            async for chunk in gateway.stream(messages, ctx=ctx, models=args.model or None):
                if chunk.delta:
                    print(chunk.delta, end="", flush=True)
            print()
        else:
            result = await gateway.complete(messages, ctx=ctx, models=args.model or None)
            print(result.text)
    finally:
        await services.aclose()
        print("\n--- usage ---", file=sys.stderr)
        for rec in mem.records:
            print(
                json.dumps(rec.model_dump(mode="json", exclude={"ts"}), default=str),
                file=sys.stderr,
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to a subcommand."""
    parser = argparse.ArgumentParser(prog="roundtable")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("providers", help="check dependencies and providers")
    chat = sub.add_parser("chat", help="send one prompt through the gateway")
    chat.add_argument("prompt")
    chat.add_argument("--role", default="default")
    chat.add_argument("--model", action="append", help="provider:model (repeatable, in order)")
    chat.add_argument("--stream", action="store_true")
    sub.add_parser("bench", help="Ollama concurrency benchmark", add_help=False)

    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["bench"]:
        from roundtable.bench import main as bench_main

        return bench_main(argv[1:])
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level if args.cmd != "chat" else "WARNING", json_output=False)
    if args.cmd == "providers":
        return asyncio.run(_providers())
    return asyncio.run(_chat(args))


if __name__ == "__main__":
    raise SystemExit(main())
