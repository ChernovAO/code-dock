FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY apps/core/pyproject.toml apps/core/README.md apps/core/
COPY apps/core/src apps/core/src
RUN uv sync --frozen --no-dev \
    && useradd --system --no-create-home app

USER app

EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]
