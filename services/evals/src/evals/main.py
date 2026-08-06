import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from deflect_common.auth import bearer_guard
from deflect_common.jobs import EVAL_ITEM_STREAM, JobQueue, RedisJobQueue
from deflect_common.llm.base import LLMClient, get_client
from deflect_common.logging import configure_logging
from deflect_common.observability import RequestIdMiddleware, metrics_response
from deflect_common.schemas import RunEvalsRequest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from evals.answer_client import AnswerClient
from evals.config import get_settings
from evals.dataset import GoldenItem, load_dataset
from evals.db import SessionDep
from evals.judge import JUDGE_VERSION
from evals.models import EvalItemJob, EvalResult, EvalRun


def _git_sha() -> str:
    """Identify the code under evaluation.

    Configured explicitly in a container, where there is neither a git binary nor a
    checkout. Falls back to asking git during local runs.
    """
    configured = get_settings().git_sha
    if configured:
        return configured
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def _smoke_subset(items: list[GoldenItem], limit: int | None) -> list[GoldenItem]:
    """Take a limited run that still exercises refusal.

    The unanswerable items sit at the end of the dataset, so a plain head-of-list
    slice would score only answerable questions and never test the behaviour the
    project exists for.
    """
    if limit is None or limit >= len(items):
        return items

    answerable = [i for i in items if not i.should_escalate]
    unanswerable = [i for i in items if i.should_escalate]
    refusals = max(1, limit // 4)
    return unanswerable[:refusals] + answerable[: limit - refusals]


def _make_judge() -> LLMClient:
    settings = get_settings()
    return get_client(
        provider=settings.llm_provider,
        model=settings.judge_model,
        api_key=settings.provider_api_key,
        base_url=settings.ollama_base_url,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the judge client once, at startup, so a missing credential stops the
    service from booting rather than failing the first run that uses it."""
    app.state.judge_client = _make_judge()
    yield


# Interactive docs are an inventory of the attack surface, and nobody browses them on a
# deployed service. Disabled in production; the policy table records that they are public
# in development and absent otherwise.
_docs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if get_settings().env == "production"
    else {}
)
app = FastAPI(title="Deflect evals", lifespan=lifespan, **_docs)
configure_logging()
app.add_middleware(RequestIdMiddleware)
router = APIRouter()

# Built at import so an unset token aborts this module rather than leaving the most
# expensive operation in the system reachable by anyone.
require_operator = bearer_guard(get_settings().operator_token, "operator")

# Guards /metrics, and its construction aborts the import when SERVICE_TOKEN is unset, so
# a deploy that forgot the credential refuses to start rather than failing partway through
# an eval run.
require_service = bearer_guard(get_settings().service_token, "service")


def build_judge(request: Request) -> LLMClient:
    return request.app.state.judge_client


def build_queue() -> JobQueue:
    return RedisJobQueue(get_settings().redis_url)


def build_answer_client() -> AnswerClient:
    settings = get_settings()
    return AnswerClient(settings.answer_url, settings.service_token)


JudgeDep = Annotated[LLMClient, Depends(build_judge)]
AnswerDep = Annotated[AnswerClient, Depends(build_answer_client)]
QueueDep = Annotated[JobQueue, Depends(build_queue)]


async def _progress(session: AsyncSession, run_id: int, items_total: int) -> dict:
    """How many of a run's items are finished.

    Failed items count as finished. Otherwise progress would stick below 100% on a run
    that is genuinely over, which reads as a stall.
    """
    finished = (
        await session.execute(
            select(func.count())
            .select_from(EvalItemJob)
            .where(EvalItemJob.run_id == run_id, EvalItemJob.status.in_(("done", "failed")))
        )
    ).scalar_one()
    return {"finished": finished, "total": items_total}


def _run_summary(run: EvalRun) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "items_total": run.items_total,
        "git_sha": run.git_sha,
        "prompt_version": run.prompt_version,
        "judge_version": run.judge_version,
        "model": run.model,
        "item_count": run.item_count,
        "metrics": run.metrics,
        "retrieval_config": run.retrieval_config,
        "thresholds": run.thresholds,
        "created_at": run.created_at.isoformat(),
    }


def _result_row(result: EvalResult) -> dict:
    return {
        "item_id": result.item_id,
        "question": result.question,
        "answer": result.answer,
        "escalated": result.escalated,
        "expected_escalate": result.expected_escalate,
        "retrieved_sources": result.retrieved_sources,
        "hit_at_5": result.hit_at_5,
        "mrr": result.mrr,
        "faithfulness": result.faithfulness,
        "rationale": result.rationale,
    }


async def _load_run(session: AsyncSession, run_id: int) -> EvalRun:
    run = await session.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"eval run {run_id} not found")
    return run


async def _results_for(session: AsyncSession, run_id: int) -> list[EvalResult]:
    statement = select(EvalResult).where(EvalResult.run_id == run_id).order_by(EvalResult.item_id)
    return list((await session.execute(statement)).scalars().all())


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


@router.post("/runs", status_code=202, dependencies=[Depends(require_operator)])
async def create_run(request: RunEvalsRequest, session: SessionDep, queue: QueueDep) -> dict:
    """Accept the run and return its id.

    This used to execute eighty items inline -- roughly two hours against a free-tier
    quota, held open by one HTTP request -- so a client timeout or a container restart
    destroyed the whole run.

    fail_under is no longer honoured here: nothing has been scored yet, so there is no
    faithfulness to compare. CI polls the run and applies its own threshold instead.
    """
    items = _smoke_subset(load_dataset(get_settings().dataset_path), request.limit)
    if not items:
        raise HTTPException(status_code=422, detail="cannot run evals over an empty dataset")

    # prompt_version, model and thresholds are filled at finalisation: they describe what
    # actually answered, and nothing has answered yet.
    run = EvalRun(
        git_sha=_git_sha(),
        prompt_version="",
        judge_version=JUDGE_VERSION,
        model=get_settings().judge_model,
        retrieval_config=(request.search.model_dump() if request.search else {}),
        thresholds={},
        item_count=0,
        metrics={},
        items_total=len(items),
        status="running",
    )
    session.add(run)
    await session.flush()

    jobs = [EvalItemJob(run_id=run.id, item_id=item.id) for item in items]
    session.add_all(jobs)
    await session.flush()

    # Rows first, then messages, all in one transaction: a failed enqueue rolls the run
    # back rather than leaving items no worker will ever be told about.
    for job in jobs:
        await queue.enqueue(EVAL_ITEM_STREAM, job.id)

    await session.commit()
    return {"run_id": run.id}


@router.get("/eval-runs")
async def list_runs(session: SessionDep) -> list[dict]:
    statement = select(EvalRun).order_by(EvalRun.id.desc()).limit(50)
    return [_run_summary(run) for run in (await session.execute(statement)).scalars()]


# Declared before /{run_id} so the literal path is not captured as a path parameter.
@router.get("/eval-runs/diff")
async def diff_runs(base: int, head: int, session: SessionDep) -> dict:
    base_run, head_run = await _load_run(session, base), await _load_run(session, head)
    base_by_item = {r.item_id: r for r in await _results_for(session, base)}

    regressed = []
    for result in await _results_for(session, head):
        previous = base_by_item.get(result.item_id)
        if previous is None or previous.faithfulness is None or result.faithfulness is None:
            continue
        if result.faithfulness < previous.faithfulness:
            regressed.append(
                {
                    "item_id": result.item_id,
                    "question": result.question,
                    "base_faithfulness": previous.faithfulness,
                    "head_faithfulness": result.faithfulness,
                }
            )

    return {
        "base": _run_summary(base_run),
        "head": _run_summary(head_run),
        "regressed": regressed,
    }


@router.get("/eval-runs/{run_id}")
async def get_run(run_id: int, session: SessionDep) -> dict:
    run = await _load_run(session, run_id)
    results = await _results_for(session, run_id)
    return _run_summary(run) | {
        "progress": await _progress(session, run_id, run.items_total),
        "results": [_result_row(r) for r in results],
    }


@router.get("/eval-runs/{run_id}/events")
async def run_events(run_id: int, http: Request, session: SessionDep) -> StreamingResponse:
    """Progress as SSE, closing when the run finalises.

    Public, like the run itself: a run's progress is the same class of information as its
    results, which the dashboard already shows to anyone. Watching an eval run is the most
    interesting thing this project does, and a credential would hide the demo.
    """
    # Resolved before streaming, so an unknown id is a 404 rather than a 200 carrying an
    # error frame -- the same contract GET /eval-runs/{run_id} already has.
    if await session.get(EvalRun, run_id) is None:
        raise HTTPException(status_code=404, detail=f"eval run {run_id} not found")

    async def stream() -> AsyncIterator[str]:
        while True:
            # An abandoned stream must not hold a database session for the whole life of
            # a run, which is about two hours.
            if await http.is_disconnected():
                return

            run = await session.get(EvalRun, run_id)
            if run is None:
                return
            # The identity map would otherwise hand back the row as it looked on the
            # first pass, and the stream would report "running" forever.
            await session.refresh(run)

            frame = {
                "status": run.status,
                "progress": await _progress(session, run_id, run.items_total),
            }
            yield f"data: {json.dumps(frame)}\n\n"
            if run.status != "running":
                return
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/metrics", dependencies=[Depends(require_service)])
async def metrics() -> Response:
    return metrics_response()


app.include_router(router)
