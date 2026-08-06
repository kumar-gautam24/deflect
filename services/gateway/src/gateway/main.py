from typing import Annotated

import httpx
from deflect_common.auth import bearer_guard
from deflect_common.logging import configure_logging
from deflect_common.observability import RequestIdMiddleware, metrics_response
from deflect_common.sessions import RedisSessionStore, SessionStore
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response

from gateway.config import get_settings
from gateway.principal import allowed
from gateway.proxy import forward
from gateway.routes import ROUTES, Route

_docs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if get_settings().env == "production"
    else {}
)
app = FastAPI(title="Deflect gateway", **_docs)
configure_logging()
app.add_middleware(RequestIdMiddleware)

# Built at import: an unset token or url aborts this module and the process exits before
# binding a port, the same refuse-to-boot behaviour every other service has.
_settings = get_settings()
_sessions = RedisSessionStore(_settings.redis_url)
_client = httpx.AsyncClient()
require_service = bearer_guard(_settings.service_token, "service")

_UPSTREAMS = {
    "retrieval": _settings.retrieval_url,
    "answer": _settings.answer_url,
    "evals": _settings.evals_url,
    "auth": _settings.auth_url,
}


def build_sessions() -> SessionStore:
    return _sessions


def build_client() -> httpx.AsyncClient:
    return _client


SessionsDep = Annotated[SessionStore, Depends(build_sessions)]
ClientDep = Annotated[httpx.AsyncClient, Depends(build_client)]


def _handler_for(route: Route):
    """One handler per table entry, closed over its own Route.

    Registered through add_api_route rather than matched by hand, so FastAPI does the
    path parsing -- {job_id} and friends -- and produces the right 404 and 405 without
    the gateway reimplementing either.
    """

    async def handler(
        request: Request,
        sessions: SessionsDep,
        client: ClientDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        if not await allowed(
            route, authorization, sessions, _settings.service_token, _settings.operator_token
        ):
            raise HTTPException(
                status_code=401,
                detail=f"a {route.principal} credential is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await forward(route, request, _UPSTREAMS[route.upstream], client)

    return handler


for _route in ROUTES:
    app.add_api_route(
        _route.path,
        _handler_for(_route),
        methods=[_route.method],
        # The proxied body is whatever the upstream returned; describing it as a model
        # would be a second, drifting copy of the upstream's contract.
        response_model=None,
    )

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: this process is answering. Deliberately touches no dependency."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness: this process can route.

    It deliberately does not probe its upstreams. A gateway that reports unready because
    one service is sick turns one outage into a total one, which is the opposite of what
    an edge is for. A sick upstream is the circuit breaker's job, per route.
    """
    return {"status": "ok"}


@router.get("/metrics", dependencies=[Depends(require_service)])
async def metrics() -> Response:
    """The gateway's OWN metrics. No upstream's /metrics is routable."""
    return metrics_response()


app.include_router(router)
