# Pinned by digest: the slim tag moves, so an unpinned base means two builds of the same
# commit can differ.
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# The image mirrors the repository layout rather than flattening it: this service's
# pyproject resolves deflect-common at ../../packages/common, and a flattened WORKDIR
# would make that path escape the filesystem root.
WORKDIR /app/services/gateway
ENV PATH="/app/services/gateway/.venv/bin:$PATH"

COPY packages/common /app/packages/common
COPY services/gateway/pyproject.toml services/gateway/uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY services/gateway .
RUN uv sync --no-dev --frozen

# Runs as a non-root user: a container process that does not need root should not have it.
RUN useradd --system --uid 10001 --create-home --home-dir /home/deflect deflect \
    && chown -R deflect /app
USER deflect

# One worker: the rate limiter is an in-process schedule, so N workers would mean N
# independent buckets and the limit would silently become N times what it says.
# RedisLeakyBucket exists and is tested for the day that stops being acceptable.
#
# --forwarded-allow-ips is explicit and EMPTY even though this service IS the edge. The
# default would have uvicorn rewrite request.client from X-Forwarded-For before the
# application runs, taking the LEFTMOST entry -- which a caller controls. The gateway
# reads the header itself and takes the rightmost, so uvicorn must be told to leave
# request.client alone rather than helpfully corrupting it first.
CMD ["gunicorn", "gateway.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "1", "--bind", "0.0.0.0:8000", \
     "--graceful-timeout", "30", "--timeout", "120", \
     "--forwarded-allow-ips", ""]
