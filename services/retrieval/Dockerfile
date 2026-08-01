FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# The image mirrors the repository layout rather than flattening it: this service's
# pyproject resolves deflect-common at ../../packages/common, and a flattened WORKDIR
# would make that path escape the filesystem root.
WORKDIR /app/services/retrieval
ENV PATH="/app/services/retrieval/.venv/bin:$PATH"

COPY packages/common /app/packages/common
COPY services/retrieval/pyproject.toml services/retrieval/uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY services/retrieval .
RUN uv sync --no-dev --frozen

CMD ["uvicorn", "retrieval.main:app", "--host", "0.0.0.0", "--port", "8001"]
