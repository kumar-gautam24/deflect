"""Request-scoped observability shared by all three services."""

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from deflect_common.logging import request_id

HEADER = "X-Request-ID"


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
        try:
            response = await call_next(request)
        finally:
            request_id.reset(token)

        # Echoed so a caller can quote it in a bug report.
        response.headers[HEADER] = current
        return response
