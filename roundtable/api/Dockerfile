# syntax=docker/dockerfile:1.7
# Build context is the repository root (roundtable/) so config/ and prompts/ can be baked in.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.11.32
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first so code changes don't invalidate the dependency layer.
COPY api/pyproject.toml api/uv.lock api/.python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY api/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# Defaults; compose mounts the live directories over these so edits need no rebuild.
COPY config ./config
COPY prompts ./prompts

ENV PATH="/app/.venv/bin:$PATH" \
    ROUNDTABLE_CONFIG=/app/config/roundtable.yaml
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "roundtable.main:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
