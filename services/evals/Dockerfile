# Pinned by digest: the slim tag moves, so an unpinned base means two builds of the same
# commit can differ.
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b
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


# Runs as a non-root user: a container process that does not need root should not have it,
# and this costs nothing to arrange.
RUN useradd --system --uid 10001 deflect && chown -R deflect /app
USER deflect
CMD ["gunicorn", "evals.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8003", \
     "--graceful-timeout", "30", "--timeout", "120"]

