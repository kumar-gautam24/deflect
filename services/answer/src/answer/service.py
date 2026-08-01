"""The single answer code path. Both /answer and /ask call this, and so does the
eval service by way of /answer over HTTP."""

import json
import time
from pathlib import Path

from deflect_common.llm.base import LLMClient
from deflect_common.schemas import (
    AnswerRequest,
    AnswerResponse,
    Citation,
    Hit,
    SearchRequest,
)
from sqlalchemy.ext.asyncio import AsyncSession

from answer.config import get_settings
from answer.gate import GateThresholds, evaluate_gate
from answer.models import Escalation, Trace
from answer.retrieval_client import RetrievalClient
from answer.telemetry import estimate_cost

PROMPT_VERSION = "answer_v1"
PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md").read_text()

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "cited_chunk_ids": {"type": "array", "items": {"type": "integer"}},
        "grounded": {"type": "boolean"},
    },
    "required": ["answer", "cited_chunk_ids", "grounded"],
}


def _format_context(hits: list[Hit]) -> str:
    return "\n\n".join(f"[id: {hit.chunk_id}] {hit.heading_path}\n{hit.text}" for hit in hits)


def _thresholds(request: AnswerRequest) -> GateThresholds:
    settings = get_settings()
    return GateThresholds(
        min_top_score=(
            request.min_top_score if request.min_top_score is not None else settings.min_top_score
        ),
        min_margin=(
            request.min_margin if request.min_margin is not None else settings.min_margin
        ),
    )


async def answer_question(
    session: AsyncSession,
    request: AnswerRequest,
    client: LLMClient,
    retrieval: RetrievalClient,
) -> AnswerResponse:
    started = time.monotonic()

    hits = (await retrieval.search(request.search or SearchRequest(query=request.question))).hits
    prompt = PROMPT_TEMPLATE.format(
        context=_format_context(hits), question=request.question
    )

    completion = await client.complete(prompt, schema=RESPONSE_SCHEMA)
    payload = json.loads(completion.text)

    decision = evaluate_gate(hits, grounded=payload["grounded"], thresholds=_thresholds(request))

    # Citations are resolved against retrieved chunks, so a hallucinated id cannot
    # produce a citation that links nowhere.
    by_id = {hit.chunk_id: hit for hit in hits}
    citations = (
        []
        if decision.escalate
        else [
            Citation(
                source_path=by_id[cid].source_path,
                heading_path=by_id[cid].heading_path,
                chunk_id=cid,
            )
            for cid in payload["cited_chunk_ids"]
            if cid in by_id
        ]
    )

    latency_ms = int((time.monotonic() - started) * 1000)
    cost = estimate_cost(completion.model, completion.input_tokens, completion.output_tokens)

    trace = Trace(
        question=request.question,
        answer=payload["answer"],
        escalated=decision.escalate,
        reason=decision.reason,
        top_score=decision.top_score,
        margin=decision.margin,
        retrieved=[
            {"chunk_id": h.chunk_id, "source_path": h.source_path, "score": h.score}
            for h in hits
        ],
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        cost_usd=cost,
        model=completion.model,
        prompt_version=PROMPT_VERSION,
        latency_ms=latency_ms,
    )
    session.add(trace)
    await session.flush()

    if decision.escalate:
        session.add(
            Escalation(trace_id=trace.id, question=request.question, reason=decision.reason)
        )
        await session.flush()

    return AnswerResponse(
        trace_id=trace.id,
        answer=payload["answer"],
        citations=citations,
        escalated=decision.escalate,
        reason=decision.reason,
        top_score=decision.top_score,
        margin=decision.margin,
        hits=hits,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        cost_usd=cost,
        model=completion.model,
        prompt_version=PROMPT_VERSION,
        latency_ms=latency_ms,
    )
