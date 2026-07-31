from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from deflect.db import SessionDep
from deflect.models import Trace

router = APIRouter(prefix="/traces")


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
        "created_at": trace.created_at.isoformat(),
    }


@router.get("")
async def list_traces(session: SessionDep) -> list[dict]:
    statement = select(Trace).order_by(Trace.id.desc()).limit(100)
    return [_serialize(trace) for trace in (await session.execute(statement)).scalars()]


@router.get("/{trace_id}")
async def get_trace(trace_id: int, session: SessionDep) -> dict:
    trace = await session.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
    return _serialize(trace)
