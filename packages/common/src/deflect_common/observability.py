"""Request-scoped observability shared by all three services."""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from deflect_common.logging import request_id

HEADER = "X-Request-ID"

logger = logging.getLogger("deflect.request")

# Orchestrators poll these every few seconds and scrapers hit /metrics on a schedule.
# At INFO they would drown every line that describes real traffic, so they drop to DEBUG.
_POLLED = frozenset({"/health", "/ready", "/metrics"})

# Labelled by route template rather than raw path: /traces/17 and /traces/18 are the same
# operation, and one series per id would be an unbounded cardinality leak.
REQUESTS = Counter(
    "deflect_requests_total", "Requests handled", ["method", "route", "status"]
)
LATENCY = Histogram("deflect_request_seconds", "Request latency", ["method", "route"])


def _route(request: Request) -> str:
    """The route template, or "unmatched" for a path no route claims.

    Using request.url.path would mint a fresh time series per trace id, so a handful of
    real users would blow the metric up. Falling back to a constant rather than the raw
    path keeps a 404 scan from doing the same.
    """
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Adopt the caller's request id, or mint one.

    Adopting rather than always generating is the whole point: a question entering at the
    web app and reaching retrieval through the answer service carries one id the entire
    way, so three services' logs reassemble into a single story.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        current = request.headers.get(HEADER) or uuid.uuid4().hex
        token = request_id.set(current)
        started = time.perf_counter()

        # The reset is the outermost finally deliberately. The formatter reads the
        # contextvar when it formats, so anything logged after a reset loses the id --
        # which would silently strip it from the one line per request this emits, the
        # very thing the id exists for.
        try:
            try:
                response = await call_next(request)
            except Exception:
                # Counted and logged before re-raising: an endpoint that only ever fails
                # would otherwise be invisible in both, which is when you most want it.
                REQUESTS.labels(request.method, _route(request), "500").inc()
                logger.exception("%s %s raised", request.method, _route(request))
                raise
            finally:
                elapsed = time.perf_counter() - started
                LATENCY.labels(request.method, _route(request)).observe(elapsed)

            route = _route(request)
            REQUESTS.labels(request.method, route, str(response.status_code)).inc()
            # One line per request, in every service. The request id is attached by the
            # formatter, so a question's path across three services shares one identifier
            # and the logs reassemble into a single story rather than three of them.
            logger.log(
                logging.DEBUG if route in _POLLED else logging.INFO,
                "%s %s -> %s",
                request.method,
                route,
                response.status_code,
                extra={
                    "route": route,
                    "status": response.status_code,
                    "duration_ms": int(elapsed * 1000),
                },
            )

            # Echoed so a caller can quote it in a bug report.
            response.headers[HEADER] = current
            return response
        finally:
            request_id.reset(token)


def metrics_response() -> Response:
    """Prometheus exposition for the default registry.

    Guarded by the service principal at every call site: request volumes and latencies
    are operational intelligence, and every route in this system has a principal.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
