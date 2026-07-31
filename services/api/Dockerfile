FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app

# Dependencies are synced from the lockfile in their own layer, before the source
# is copied, so a source edit does not invalidate the dependency cache. Putting the
# venv on PATH lets CMD run uvicorn directly: `uv run` would re-resolve at every
# container start and undo the pinning.
ENV PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY . .
RUN uv sync --no-dev --frozen

CMD ["uvicorn", "deflect.main:app", "--host", "0.0.0.0", "--port", "8000"]
