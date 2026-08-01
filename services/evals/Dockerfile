FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# The image mirrors the repository layout rather than flattening it: this service's
# pyproject resolves deflect-common at ../../packages/common, and a flattened WORKDIR
# would make that path escape the filesystem root.
WORKDIR /app/services/evals
ENV PATH="/app/services/evals/.venv/bin:$PATH"

COPY packages/common /app/packages/common
COPY services/evals/pyproject.toml services/evals/uv.lock ./
# Dev group included: CI runs the dataset validation inside this image.
RUN uv sync --frozen --no-install-project
COPY evals /app/evals
COPY services/evals .
RUN uv sync --frozen

CMD ["uvicorn", "evals.main:app", "--host", "0.0.0.0", "--port", "8003"]
