from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deflect.db import SessionDep
from deflect.models import EvalResult, EvalRun

router = APIRouter(prefix="/eval-runs")


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


@router.get("")
async def list_runs(session: SessionDep) -> list[dict]:
    statement = select(EvalRun).order_by(EvalRun.id.desc()).limit(50)
    return [_run_summary(run) for run in (await session.execute(statement)).scalars()]


# Declared before /{run_id} so the literal path is not captured as a path parameter.
@router.get("/diff")
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


@router.get("/{run_id}")
async def get_run(run_id: int, session: SessionDep) -> dict:
    run = await _load_run(session, run_id)
    results = await _results_for(session, run_id)
    return _run_summary(run) | {"results": [_result_row(r) for r in results]}
