# Pinned by digest: the slim tag moves, so an unpinned base means two builds of the same
# commit can differ.
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# The image mirrors the repository layout rather than flattening it: this service's
# pyproject resolves deflect-common at ../../packages/common, and a flattened WORKDIR
# would make that path escape the filesystem root.
WORKDIR /app/services/answer
ENV PATH="/app/services/answer/.venv/bin:$PATH"

COPY packages/common /app/packages/common
COPY services/answer/pyproject.toml services/answer/uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY services/answer .
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
# EXACTLY ONE WORKER, deliberately. /ask's backstop limiter (config.py's
# ask_backstop_per_hour) is an in-process dict, so N workers would mean N independent
# limiters and the backstop would silently become N times as generous -- undermining the
# one thing it exists to bound. This service awaits a model provider rather than burning
# CPU, so async concurrency already carries its load and extra workers would buy almost
# nothing anyway.
#
# When the job queue arrives and brings Redis, the limiter can move there and this can
# scale out. Until then, raising this number weakens the backstop.
#
# Explicit, because the default trusts 127.0.0.1 and rewrites request.client from
# X-Forwarded-For before the application runs -- which silently defeats every
# trust_forwarded=False decision the code makes. Behind a proxy this makes
# request.client the proxy's address, so anonymous callers share one bucket: stricter
# rather than weaker, and it fails in the safe direction.
CMD ["gunicorn", "answer.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "1", "--bind", "0.0.0.0:8002", \
     "--graceful-timeout", "30", "--timeout", "300", \
     "--forwarded-allow-ips", ""]

