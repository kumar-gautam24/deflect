import logging
import time
from datetime import UTC, datetime
from typing import Annotated

from deflect_common.auth import bearer_guard, hash_token
from deflect_common.logging import configure_logging
from deflect_common.observability import RequestIdMiddleware, metrics_response
from deflect_common.ratelimit import SlidingWindowLimiter, client_address
from deflect_common.schemas import LoginRequest
from deflect_common.sessions import RedisSessionStore, SessionStore
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from auth.config import get_settings
from auth.db import SessionDep
from auth.models import AdminUser
from auth.models import Session as SessionRow
from auth.policy import Policy
from auth.service import AccountLocked, LoginFailed, login, logout, logout_all

logger = logging.getLogger(__name__)

# Interactive docs are an inventory of the attack surface, and nobody browses them on a
# deployed service. Disabled in production; the policy table records that they are public
# in development and absent otherwise.
_docs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if get_settings().env == "production"
    else {}
)
app = FastAPI(title="Deflect auth", **_docs)
configure_logging()
app.add_middleware(RequestIdMiddleware)
router = APIRouter()


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Say what was wrong with the request without quoting the request back.

    Pydantic attaches the offending value to every validation error, and when the failure
    is a missing field that value is the whole body -- so misspelling "email" returns the
    plaintext password to the caller, and leaves it in whatever proxy log or error tracker
    the response passes through. The constraint is that a password exists nowhere but its
    hash, so the input is dropped rather than truncated or masked.
    """
    without_input = [
        {key: value for key, value in error.items() if key != "input"}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(without_input)})

# Built at import, not per request: an unset token or url aborts this module and uvicorn
# exits before binding a port, the same refuse-to-boot behaviour every other service has.
_sessions = RedisSessionStore(get_settings().redis_url)
require_service = bearer_guard(get_settings().service_token, "service")
_login_limiter = SlidingWindowLimiter(
    limit=Policy.LOGIN_ATTEMPTS_PER_HOUR, window_seconds=3600
)


def build_sessions() -> SessionStore:
    return _sessions


SessionsDep = Annotated[SessionStore, Depends(build_sessions)]


async def current_session(
    session: SessionDep, authorization: Annotated[str | None, Header()] = None
) -> SessionRow:
    """The Session row behind the presented token, or 401.

    Resolved from the database rather than the cache: logout has to stamp revoked_at on a
    real row, and /auth/me should report the authoritative expiry rather than a copy.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "a session is required", headers={"WWW-Authenticate": "Bearer"})

    row = (
        await session.execute(
            select(SessionRow).where(SessionRow.token_hash == hash_token(token))
        )
    ).scalar_one_or_none()

    if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
        raise HTTPException(401, "a session is required", headers={"WWW-Authenticate": "Bearer"})
    return row


CurrentSessionDep = Annotated[SessionRow, Depends(current_session)]


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: this process is answering. Deliberately touches no dependency -- a
    probe that queries the database restarts a healthy process whenever Postgres
    hiccups, which is the opposite of what liveness is for."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: SessionDep) -> dict[str, str]:
    """Readiness: this service can do useful work. Checks only its OWN database.

    It deliberately does not probe the services it calls. A readiness check that
    follows its dependencies turns one outage into all three reporting unready, so an
    orchestrator restarts healthy processes and the failure amplifies instead of
    staying contained.
    """
    await session.execute(text("select 1"))
    return {"status": "ok", "database": "connected"}


@router.post("/auth/login")
async def login_route(
    request: LoginRequest, http: Request, session: SessionDep, sessions: SessionsDep
) -> dict:
    address = client_address(http, trust_forwarded=False)
    if not _login_limiter.check(address, time.monotonic()):
        raise HTTPException(429, "too many login attempts", headers={"Retry-After": "3600"})

    try:
        token, row = await login(
            session,
            sessions,
            request.email,
            request.password,
            now=datetime.now(UTC),
            ip=address,
            user_agent=http.headers.get("user-agent"),
        )
    except AccountLocked as locked:
        await session.commit()
        # Deliberately the same status, body and headers as a wrong password. AccountLocked
        # is raised only for an account that exists, so a 423 -- or a Retry-After -- would
        # answer "is this address an admin?" in five requests, which is the question the
        # single 401 below exists to refuse. The cost is that a locked-out admin is not
        # told why; this log line is where an operator finds out.
        logger.warning(
            "login refused: account %s locked for %s more seconds",
            locked.user_id,
            locked.seconds_remaining,
        )
        raise HTTPException(401, "invalid email or password") from locked
    except LoginFailed as failed:
        # Committed so the failure count survives, then one message for both a wrong
        # password and an unknown address.
        await session.commit()
        raise HTTPException(401, "invalid email or password") from failed

    await session.commit()
    return {"token": token, "role": row.role, "expires_at": row.expires_at.isoformat()}


@router.post("/auth/logout")
async def logout_route(
    session: SessionDep, sessions: SessionsDep, current: CurrentSessionDep
) -> dict:
    await logout(session, sessions, current.token_hash, now=datetime.now(UTC))
    await session.commit()
    return {"status": "ok"}


@router.post("/auth/logout-all")
async def logout_all_route(
    session: SessionDep, sessions: SessionsDep, current: CurrentSessionDep
) -> dict:
    await logout_all(session, sessions, current.user_id, now=datetime.now(UTC))
    await session.commit()
    return {"status": "ok"}


@router.get("/auth/me")
async def me_route(session: SessionDep, current: CurrentSessionDep) -> dict:
    user = await session.get(AdminUser, current.user_id)
    return {
        "email": user.email,
        "role": current.role,
        "expires_at": current.expires_at.isoformat(),
    }


@router.get("/metrics", dependencies=[Depends(require_service)])
async def metrics() -> Response:
    return metrics_response()


app.include_router(router)
