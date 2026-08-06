from deflect_common.logging import configure_logging
from deflect_common.observability import RequestIdMiddleware
from fastapi import APIRouter, FastAPI

from gateway.config import get_settings

# Interactive docs are an inventory of the attack surface. The rule is identical to every
# other service's -- the gateway does not get an exemption for being the front door.
_docs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if get_settings().env == "production"
    else {}
)
app = FastAPI(title="Deflect gateway", **_docs)
configure_logging()
app.add_middleware(RequestIdMiddleware)
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: this process is answering. Deliberately touches no dependency."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness: this process can route.

    It deliberately does not probe its upstreams. A gateway that reports unready because
    one service is sick turns a single outage into a total one, which is the opposite of
    what an edge is for -- the circuit breaker handles a sick upstream per-route.
    """
    return {"status": "ok"}


app.include_router(router)
