# Pinned by digest: the slim tag moves, so an unpinned base means two builds of the same
# commit can differ.
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# The image mirrors the repository layout rather than flattening it: this service's
# pyproject resolves deflect-common at ../../packages/common, and a flattened WORKDIR
# would make that path escape the filesystem root.
WORKDIR /app/services/auth
ENV PATH="/app/services/auth/.venv/bin:$PATH"

COPY packages/common /app/packages/common
COPY services/auth/pyproject.toml services/auth/uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY services/auth .
RUN uv sync --no-dev --frozen


# Runs as a non-root user: a container process that does not need root should not have it.
RUN useradd --system --uid 10001 --create-home --home-dir /home/deflect deflect \
    && chown -R deflect /app
USER deflect
CMD ["gunicorn", "auth.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8004", \
     "--graceful-timeout", "30", "--timeout", "120"]
