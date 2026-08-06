import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from deflect_common.auth import bearer_guard, token_matches
from deflect_common.llm.base import LLMClient, get_client
from deflect_common.logging import configure_logging
from deflect_common.observability import RequestIdMiddleware, metrics_response
from deflect_common.ratelimit import (
    SlidingWindowLimiter,
    client_address,
    seconds_until_utc_midnight,
)
from deflect_common.schemas import AnswerRequest, AnswerResponse
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text

from answer.config import get_settings
from answer.db import SessionDep
from answer.models import Trace
from answer.ratelimit import questions_today
from answer.retrieval_client import RetrievalClient
from answer.service import answer_question


def _make_client() -> LLMClient:
    settings = get_settings()
    return get_client(
        provider=settings.llm_provider,
        model=settings.generation_model,
        api_key=settings.provider_api_key,
        base_url=settings.ollama_base_url,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the provider client once, at startup.

    Constructing it per request rebuilt an HTTP client on every call, and a missing
    credential surfaced as a 500 on the first real request rather than a service that
    refuses to boot. Failing here means a misconfigured deploy never takes traffic.
    """
    app.state.llm_client = _make_client()
    yield


# Interactive docs are an inventory of the attack surface, and nobody browses them on a
# deployed service. Disabled in production; the policy table records that they are public
# in development and absent otherwise.
_docs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if get_settings().env == "production"
    else {}
)
app = FastAPI(title="Deflect answer", lifespan=lifespan, **_docs)
configure_logging()
app.add_middleware(RequestIdMiddleware)
router = APIRouter()

# Built at import, not in the lifespan: an unset token aborts this module and uvicorn
# exits before binding a port. Module-level names are what dependency_overrides keys on.
require_service = bearer_guard(get_settings().service_token, "service")
require_operator = bearer_guard(get_settings().operator_token, "operator")

# One limiter for the process. Per-process state means each instance counts separately
# and a redeploy grants a fresh allowance; see ratelimit.py for why that is accepted.
_ask_limiter = SlidingWindowLimiter(
    limit=get_settings().ask_rate_limit_per_hour, window_seconds=3600
)


async def enforce_ask_limits(
    http: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Reject an abusive question before it reaches a model.

    Ordered cheapest first: the per-address window is a dict lookup, the daily cap is a
    database query.
    """
    settings = get_settings()
    trusted = token_matches(settings.service_token, authorization)
    address = client_address(http, trust_forwarded=trusted)

    if not _ask_limiter.check(address, time.monotonic()):
        raise HTTPException(
            status_code=429,
            detail="too many questions from this address",
            headers={"Retry-After": "3600"},
        )

    now = datetime.now(UTC)
    if await questions_today(session, now) >= settings.ask_daily_limit:
        raise HTTPException(
            status_code=429,
            detail="this demo's daily question budget is spent; it resets at UTC midnight",
            headers={"Retry-After": str(seconds_until_utc_midnight(now))},
        )


app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().web_origin.split(",")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    # Without this a browser cannot read the header at all, so the id the
    # correlation feature exists to hand users never reaches them.
    expose_headers=["X-Request-ID"],
)


def build_client(request: Request) -> LLMClient:
    return request.app.state.llm_client


def build_retrieval() -> RetrievalClient:
    settings = get_settings()
    return RetrievalClient(settings.retrieval_url, settings.service_token)


ClientDep = Annotated[LLMClient, Depends(build_client)]
RetrievalDep = Annotated[RetrievalClient, Depends(build_retrieval)]


def _serialize(trace: Trace) -> dict:
    return {
        "id": trace.id,
        "question": trace.question,
        "answer": trace.answer,
        "escalated": trace.escalated,
        "reason": trace.reason,
        "top_score": trace.top_score,
        "margin": trace.margin,
        "retrieved": trace.retrieved,
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "cost_usd": trace.cost_usd,
        "model": trace.model,
        "prompt_version": trace.prompt_version,
        "latency_ms": trace.latency_ms,
        "min_top_score": trace.min_top_score,
        "min_margin": trace.min_margin,
        "created_at": trace.created_at.isoformat(),
    }


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


@router.post("/answer", dependencies=[Depends(require_service)])
async def answer(
    request: AnswerRequest,
    session: SessionDep,
    client: ClientDep,
    retrieval: RetrievalDep,
) -> AnswerResponse:
    result = await answer_question(session, request, client, retrieval)
    await session.commit()
    return result


@router.post("/ask", dependencies=[Depends(enforce_ask_limits)])
async def ask(
    request: AnswerRequest,
    session: SessionDep,
    client: ClientDep,
    retrieval: RetrievalDep,
) -> StreamingResponse:
    result = await answer_question(session, request, client, retrieval)
    await session.commit()

    async def stream() -> AsyncIterator[str]:
        # The provider returns structured JSON in one call, so streaming is done by
        # chunking the finished answer. /ask and /answer therefore cannot diverge.
        for word in result.answer.split(" "):
            yield f"data: {json.dumps({'type': 'token', 'text': word + ' '})}\n\n"
            await asyncio.sleep(0)

        done = result.model_dump() | {"type": "done"}
        yield f"data: {json.dumps(done)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/traces", dependencies=[Depends(require_operator)])
async def list_traces(session: SessionDep) -> list[dict]:
    statement = select(Trace).order_by(Trace.id.desc()).limit(100)
    return [_serialize(trace) for trace in (await session.execute(statement)).scalars()]


@router.get("/traces/{trace_id}", dependencies=[Depends(require_operator)])
async def get_trace(trace_id: int, session: SessionDep) -> dict:
    trace = await session.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
    return _serialize(trace)


@router.get("/metrics", dependencies=[Depends(require_service)])
async def metrics() -> Response:
    return metrics_response()


app.include_router(router)
