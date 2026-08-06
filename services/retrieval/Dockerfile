# Pinned by digest: the slim tag moves, so an unpinned base means two builds of the same
# commit can differ.
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b
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


# Runs as a non-root user: a container process that does not need root should not have it.
#
# HF_HOME is set explicitly and created before the switch. useradd without -m leaves no
# home directory, so huggingface_hub falls back to a path this user cannot write and the
# embedding model download dies with EACCES on the first /search -- a failure that no unit
# test sees, because the tests never run inside the container.
ENV HF_HOME=/app/.cache/huggingface
RUN useradd --system --uid 10001 --create-home --home-dir /home/deflect deflect \
    && mkdir -p /app/.cache/huggingface \
    && chown -R deflect /app /home/deflect
USER deflect
# Two workers because retrieval embeds and reranks in-process, which is CPU-bound and
# blocks the event loop -- async concurrency alone does not help there. Gunicorn also
# brings graceful shutdown, so in-flight requests finish on SIGTERM.
#
# Explicit, because the default trusts 127.0.0.1 and rewrites request.client from
# X-Forwarded-For before the application runs -- which silently defeats every
# trust_forwarded=False decision the code makes. Behind a proxy this makes
# request.client the proxy's address, so anonymous callers share one bucket: stricter
# rather than weaker, and it fails in the safe direction.
CMD ["gunicorn", "retrieval.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8001", \
     "--graceful-timeout", "30", "--timeout", "120", \
     "--forwarded-allow-ips", ""]

