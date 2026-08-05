import subprocess
from contextlib import asynccontextmanager
from typing import Annotated

from deflect_common.auth import bearer_guard
from deflect_common.llm.base import LLMClient, get_client
from deflect_common.schemas import RunEvalsRequest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from evals.answer_client import AnswerClient
from evals.config import get_settings
from evals.dataset import GoldenItem, load_dataset
from evals.db import SessionDep
from evals.models import EvalResult, EvalRun
from evals.runner import run_evals


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


app = FastAPI(title="Deflect evals", lifespan=lifespan)
router = APIRouter()

# Built at import so an unset token aborts this module rather than leaving the most
# expensive operation in the system reachable by anyone.
require_operator = bearer_guard(get_settings().operator_token, "operator")

# Built but never attached to a route: no evals route has the service principal. It
# exists so an unset SERVICE_TOKEN aborts this import, the same as it does in the other
# two services. This service presents that token outbound to the answer service, and a
# deploy that forgot it should refuse to start rather than fail partway through a run.
_require_service_at_startup = bearer_guard(get_settings().service_token, "service")


def build_judge(request: Request) -> LLMClient:
    return request.app.state.judge_client


def build_answer_client() -> AnswerClient:
    settings = get_settings()
    return AnswerClient(settings.answer_url, settings.service_token)


JudgeDep = Annotated[LLMClient, Depends(build_judge)]
AnswerDep = Annotated[AnswerClient, Depends(build_answer_client)]


def _run_summary(run: EvalRun) -> dict:
    return {
        "id": run.id,
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
async def health(session: SessionDep) -> dict[str, str]:
    await session.execute(text("select 1"))
    return {"status": "ok", "database": "connected"}


@router.post("/runs", dependencies=[Depends(require_operator)])
async def create_run(
    request: RunEvalsRequest,
    session: SessionDep,
    judge: JudgeDep,
    answer: AnswerDep,
) -> dict:
    items = _smoke_subset(load_dataset(get_settings().dataset_path), request.limit)
    git_sha = _git_sha()

    run = await run_evals(session, items, answer, judge, request.search, git_sha)
    await session.commit()

    if request.fail_under is not None and run.metrics["faithfulness"] < request.fail_under:
        raise HTTPException(
            status_code=422,
            detail=(
                f"faithfulness {run.metrics['faithfulness']:.3f} "
                f"below threshold {request.fail_under}"
            ),
        )
    return _run_summary(run)


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
    return _run_summary(run) | {"results": [_result_row(r) for r in results]}


app.include_router(router)
