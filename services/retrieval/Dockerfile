FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH"
COPY packages/common /packages/common
COPY services/retrieval/pyproject.toml services/retrieval/uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY services/retrieval .
RUN uv sync --no-dev --frozen

CMD ["uvicorn", "retrieval.main:app", "--host", "0.0.0.0", "--port", "8001"]
