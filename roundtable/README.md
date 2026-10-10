# Roundtable

A self-hosted AI assistant where every question is answered by a **team of agents** instead
of one model. A Captain reads the question, decides how many agents it needs, gives each a
distinct brief, runs them in parallel, makes them cross-examine each other's claims, and
writes one answer that shows where the team disagreed.

It runs end to end on free resources: local models through Ollama, plus optional free API
tiers (OpenRouter `:free` models). Nothing paid is required.

## Status

| Milestone | Scope | State |
|---|---|---|
| M1 Skeleton | Compose stack, config, provider layer with fallback and rate limiting, health checks | **done** |
| M2 Core graph | Captain plan → parallel specialists → synthesis, CLI runner | next |
| M3 Dynamic team | Auto / Fixed / Classic / Heavy modes, role library, budget estimator, squads | |
| M4 Debate | Claims ledger, cross-exam rounds, Contrarian, Verifier, convergence | |
| M5 Tools | web_search, fetch_page, python_exec sandbox | |
| M6 API | SSE streaming, persistence, usage/cost tracking, OpenAI-compatible endpoint | |
| M7 Web UI | Agent lanes, claims board, run history, settings | |
| M8 Bots | Telegram and Discord summon | |
| M9 Evaluation | Single model vs Classic 4 vs Auto N report | |

## Architecture

Solid boxes exist today; dashed ones arrive in the milestone noted.

```mermaid
flowchart LR
    subgraph clients[Clients]
        web[Web UI<br/>Next.js]
        cli[CLI]
        oai[[OpenAI-compatible clients<br/>n8n, Open WebUI · M6]]
        bots[[Telegram / Discord · M8]]
    end

    subgraph api[API · FastAPI]
        health[/healthz · /readyz<br/>/v1/providers/]
        runs[[/v1/runs + SSE · M6]]
        graph[[LangGraph team graph<br/>Captain → fan-out → cross-exam → synthesis · M2–M4]]
        tools[[Tools: search, fetch, python · M5]]
        gw[LLM gateway<br/>fallback · retries · timeouts · usage log]
        rl[Rate limiter<br/>token bucket + daily cap + 429 cooldown]
    end

    subgraph providers[Model providers]
        ollama[(Ollama<br/>local GPU)]
        openrouter[(OpenRouter<br/>:free models)]
        custom[(Any OpenAI-compatible<br/>vLLM · ScaleServe)]
    end

    pg[(Postgres<br/>runs · claims · usage)]
    redis[(Redis<br/>shared quotas · queue)]

    web --> health
    web -.-> runs
    cli --> gw
    oai -.-> runs
    bots -.-> runs
    runs -.-> graph
    graph -.-> tools
    graph -.-> gw
    gw --> rl --> redis
    gw --> ollama
    gw --> openrouter
    gw --> custom
    health --> pg
    health --> redis
```

### The model layer

Every model call goes through one gateway (`api/src/roundtable/gateway/gateway.py`). For
each call it walks the calling role's preference list from `config/roundtable.yaml`:

| Situation | What happens |
|---|---|
| Model answers | Return the answer. Log a usage record with tokens, latency, tokens/sec, queue time and cost. |
| Local rate limit would need a long wait (more than `max_wait_s`) | Skip to the next model without sending anything. |
| 429 that includes `Retry-After` / `X-RateLimit-Reset` | Cool down the **whole provider** until reset, then fall back. |
| 429 with no reset time | Cool down **that model** only (`default_cooldown_s`), then fall back. |
| Connection error or 5xx | Retry the same model with jittered backoff (`max_retries`), then fall back. |
| Timeout, missing model, 4xx, empty answer | Fall back right away. |
| Streaming fails before the first token | Fall back. Failures after the first token are raised, so nothing is duplicated. |

Quotas live in Redis (a single atomic Lua script), so the API, workers and bots share one
budget. On OpenRouter, consecutive free models in a preference list collapse into **one**
request through OpenRouter's `models` fallback parameter. That matters when the free tier
allows ~50 requests a day.

**Routing policy.** Ollama comes first for most roles. OpenRouter's free quota is spent on
the Captain (plan and synthesis), where a stronger, different model helps most. The
Contrarian defaults to a different local model family from the other agents, to reduce
correlated mistakes.

### Sizing Ollama for a 16 GB GPU

Ollama 0.40 serves **one request at a time per model** unless `OLLAMA_NUM_PARALLEL` is set,
and it allocates KV cache for `OLLAMA_CONTEXT_LENGTH × OLLAMA_NUM_PARALLEL` tokens per
loaded model. The defaults:

| | Weights (Q4) | KV cache, 4 slots × 8k, q8_0 | Total |
|---|---|---|---|
| qwen3:8b | ~5.2 GB | ~2.4 GB | ~7.6 GB |
| llama3.1:8b | ~4.9 GB | ~2.1 GB | ~7.0 GB |
| **Both loaded** | | | **~14.6 GB** |

These are estimates. Measure on your machine:

```bash
docker compose exec api roundtable bench --model qwen3:8b --concurrency 1,2,4,8 --no-think
```

The benchmark reports aggregate and per-request tokens/sec, time to first token, model-load
time, and **queue time** (time spent waiting for a free parallel slot inside Ollama). If
queue time grows while aggregate tokens/sec flattens, you have passed the useful
`OLLAMA_NUM_PARALLEL`. If models keep reloading (high `load`), lower `OLLAMA_CONTEXT_LENGTH`
or use one model.

## Quickstart (5 minutes)

Requirements: Docker with Compose v2.24+. For GPU inference you also need the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).

```bash
cd roundtable
cp .env.example .env               # optional: add OPENROUTER_API_KEY; no GPU? set COMPOSE_FILE
docker compose up -d --build
docker compose logs -f ollama-init # first run pulls ~10 GB of models; wait for it to exit
```

Then:

* **Status page:** http://localhost:3000. It shows every dependency, the models still
  missing, and today's OpenRouter usage.
* **Readiness:** `curl -s localhost:8000/readyz | jq`. `ok` means all good, `degraded`
  means an optional provider is down or a model isn't pulled yet, `fail` (HTTP 503) means a
  required dependency is down.
* **Ask through the gateway:**

  ```bash
  docker compose exec api roundtable chat "Why is the sky blue?" --stream
  docker compose exec api roundtable chat "Plan a 3-agent team for X" --role captain
  ```

  Usage records, including each fallback attempt, print to stderr after the answer.

No GPU: uncomment `COMPOSE_FILE=compose.yaml:compose.cpu.yaml` in `.env`. It works, slowly.
Prefer smaller models there (for example `OLLAMA_MODEL_PRIMARY=qwen3:1.7b`).

## Development

```bash
cd api
uv sync                                   # Python 3.12 + deps
uv run pytest                             # unit tests (Redis tests use a local redis-server if installed)
uv run ruff check src tests && uv run mypy
uv run --env-file ../.env uvicorn roundtable.main:app_from_env --factory --reload

cd ../web && npm install && npm run dev   # API_INTERNAL_URL defaults to http://localhost:8000
```

## Configuration

* `.env` holds secrets, URLs, ports and Ollama tuning. `.env.example` documents every variable.
* `config/roundtable.yaml` holds behaviour: limits, team sizing, providers and their rate
  limits, per-model params and pricing, and per-role model preference lists. It may
  reference env vars as `${VAR}` or `${VAR:-default}`. Compose mounts it read-only, so edits
  need a container restart, not a rebuild.
* `prompts/` holds the runtime prompts (Captain plan, specialist, Contrarian, Verifier,
  synthesis), loaded with variables from M2 on.

## Layout

```
roundtable/
├── compose.yaml, compose.cpu.yaml, .env.example
├── config/roundtable.yaml      behaviour config
├── prompts/                    runtime prompts
├── api/                        Python service (FastAPI, gateway, later the LangGraph team)
│   ├── src/roundtable/
│   │   ├── config.py           env + YAML config, validated
│   │   ├── providers/          ollama, openai_compat (OpenRouter, custom), registry
│   │   ├── gateway/            fallback gateway, rate limiter, usage records
│   │   ├── health.py, routes/  /healthz, /readyz, /v1/providers
│   │   ├── cli.py, bench.py    roundtable providers | chat | bench
│   │   └── main.py             app factory
│   └── tests/
└── web/                        Next.js status page (full UI in M7)
```
