import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from deflect_common.auth import bearer_guard
from deflect_common.jobs import INGEST_STREAM, JobQueue, RedisJobQueue
from deflect_common.logging import configure_logging
from deflect_common.observability import RequestIdMiddleware, metrics_response
from deflect_common.schemas import (
    IngestRequest,
    SearchRequest,
    SearchResponse,
)
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text

from retrieval.config import get_settings
from retrieval.db import SessionDep
from retrieval.models import Document, IngestJob
from retrieval.pipeline import RetrievalConfig, retrieve

# Interactive docs are an inventory of the attack surface, and nobody browses them on a
# deployed service. Disabled in production; the policy table records that they are public
# in development and absent otherwise.
_docs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if get_settings().env == "production"
    else {}
)
app = FastAPI(title="Deflect retrieval", **_docs)
configure_logging()
app.add_middleware(RequestIdMiddleware)

# Built at import, not in a lifespan: an unset token aborts this module and uvicorn
# exits before binding a port. Module-level names are also what dependency_overrides
# keys on when a test bypasses a guard.
_settings = get_settings()
require_service = bearer_guard(_settings.service_token, "service")
require_operator = bearer_guard(_settings.operator_token, "operator")


def build_queue() -> JobQueue:
    return RedisJobQueue(get_settings().redis_url)


QueueDep = Annotated[JobQueue, Depends(build_queue)]


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness: this process is answering. Deliberately touches no dependency -- a
    probe that queries the database restarts a healthy process whenever Postgres
    hiccups, which is the opposite of what liveness is for."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(session: SessionDep) -> dict[str, str]:
    """Readiness: this service can do useful work. Checks only its OWN database.

    It deliberately does not probe the services it calls. A readiness check that
    follows its dependencies turns one outage into all three reporting unready, so an
    orchestrator restarts healthy processes and the failure amplifies instead of
    staying contained.
    """
    await session.execute(text("select 1"))
    return {"status": "ok", "database": "connected"}


@app.get("/documents", dependencies=[Depends(require_service)])
async def documents(session: SessionDep) -> dict[str, list[str]]:
    """Every ingested source path.

    Exposed so the eval service can check that the golden dataset names documents
    that actually exist. A typo there would otherwise look like a permanent
    retrieval regression rather than a bad label.
    """
    paths = (await session.execute(select(Document.source_path))).scalars().all()
    return {"source_paths": sorted(paths)}


@app.post("/search", dependencies=[Depends(require_service)])
async def search(request: SearchRequest, session: SessionDep) -> SearchResponse:
    config = RetrievalConfig(
        use_dense=request.use_dense,
        use_lexical=request.use_lexical,
        use_rerank=request.use_rerank,
        candidate_limit=request.candidate_limit,
        final_limit=request.final_limit,
    )
    return SearchResponse(hits=await retrieve(session, request.query, config))


def resolve_corpus_path(root: str, corpus_root: Path) -> Path:
    """Resolve a requested ingest root, refusing anything outside `corpus_root`.

    Resolution happens before the check so a symlink pointing out of the corpus is
    caught, not only a literal `..`. is_relative_to compares path components rather
    than string prefixes, so a sibling named corpus-secrets does not pass as /corpus.
    """
    try:
        requested = Path(root).resolve()
    except (ValueError, OSError) as cause:
        # A path that cannot even be resolved is invalid input, not a server fault.
        # Same message as the containment rejection: a caller learns only that the
        # path was refused, never which of the two checks refused it.
        raise HTTPException(
            status_code=400, detail="ingest root is outside the corpus root"
        ) from cause
    allowed = corpus_root.resolve()

    if requested != allowed and not requested.is_relative_to(allowed):
        # The rejected path is deliberately absent from the message: echoing it back
        # would turn this endpoint into a way to map the container filesystem.
        raise HTTPException(status_code=400, detail="ingest root is outside the corpus root")

    return requested


@app.post("/ingest", status_code=202, dependencies=[Depends(require_operator)])
async def ingest(request: IngestRequest, session: SessionDep, queue: QueueDep) -> dict:
    """Accept the work and return. Embedding 2,370 chunks behind a held-open connection
    is what this endpoint used to do, and a client disconnect lost all of it.

    The path is validated before a row exists: rejecting afterwards would leave a queued
    job that can only ever fail.
    """
    root = resolve_corpus_path(request.root, get_settings().corpus_root)

    job = IngestJob(root=str(root), commit_sha=request.commit_sha)
    session.add(job)
    await session.flush()

    # Committed BEFORE enqueueing, not after. A worker blocked on XREADGROUP receives the
    # message the instant it is added, and if this transaction has not committed yet the
    # worker finds no row -- which it treats as a job whose commit failed, and
    # acknowledges. The job would then never run and never be retried.
    #
    # The cost of this order is the opposite failure: a committed job with no message,
    # which is visible as a queued row and can be re-enqueued. A dropped job cannot.
    await session.commit()
    await queue.enqueue(INGEST_STREAM, job.id)

    return {"job_id": job.id}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_operator)])
async def job_status(job_id: int, session: SessionDep) -> dict:
    job = await session.get(IngestJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return _job_payload(job)


@app.get("/jobs/{job_id}/events", dependencies=[Depends(require_operator)])
async def job_events(
    job_id: int, http: Request, session: SessionDep
) -> StreamingResponse:
    """Progress as SSE, closing once the job reaches a terminal state.

    Polled from the database rather than subscribed from Redis: the database is the
    source of truth, and a stream fed from the broker would disagree with /jobs/{id}
    the moment a message was redelivered.
    """
    # Resolved before streaming starts, so an unknown id is a 404 like everywhere else.
    # Reporting it as a 200 carrying an error frame would make the two job routes
    # disagree about what "missing" looks like.
    if await session.get(IngestJob, job_id) is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    async def stream() -> AsyncIterator[str]:
        while True:
            # A closed connection must end the loop. Without this an abandoned stream
            # holds a database session for the whole life of the job -- two hours, for
            # the runs this system exists to watch.
            if await http.is_disconnected():
                return

            job = await session.get(IngestJob, job_id)
            if job is None:
                return
            # The identity map would otherwise hand back the row as it looked on the
            # first pass, and the stream would report "queued" forever.
            await session.refresh(job)

            yield f"data: {json.dumps(_job_payload(job))}\n\n"
            if job.status in ("done", "failed"):
                return
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")


def _job_payload(job: IngestJob) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "attempts": job.attempts,
        "chunks": job.chunks,
        "error": job.error,
    }


@app.get("/metrics", dependencies=[Depends(require_service)])
async def metrics() -> Response:
    return metrics_response()
