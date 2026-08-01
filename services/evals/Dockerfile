FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH"
COPY packages/common /packages/common
COPY services/evals/pyproject.toml services/evals/uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY services/evals .
RUN uv sync --no-dev --frozen

CMD ["uvicorn", "evals.main:app", "--host", "0.0.0.0", "--port", "8003"]
