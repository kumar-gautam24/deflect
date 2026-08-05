"""The single answer code path. Both /answer and /ask call this, and so does the
eval service by way of /answer over HTTP."""

import json
import time
from pathlib import Path

from deflect_common.gate import GateThresholds, evaluate_gate
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
from answer.models import Escalation, Trace
from answer.retrieval_client import RetrievalClient
from answer.telemetry import estimate_cost

PROMPT_VERSION = "answer_v1"
PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md").read_text()

def response_schema(chunk_ids: list[int]) -> dict:
    """The response shape, with citations constrained to the ids actually retrieved.

    Ids are strings, not integers, and that is the whole point. Asked for an integer
    array, gpt-oss concatenated the ids into a single number -- [4161, 2750, 4179] came
    back as [4161275041794180] -- and every answer shipped with no citations, because the
    resolver dropped the nonsense value. An enum of integers did not fix it either: the
    provider validates the enum after generation rather than constraining generation to
    it, so an invented number became a hard 400 instead. As strings the model copies a
    literal token it can see in the context instead of composing a number.

    The enum also makes citing a chunk that was never retrieved structurally impossible
    rather than merely filtered afterwards.
    """
    cited = {"type": "array", "items": {"type": "string"}}
    # An empty enum is not valid JSON Schema. With no hits the gate escalates anyway, so
    # the unconstrained shape is only ever used for an answer that will be discarded.
    if chunk_ids:
        cited = {"type": "array", "items": {"type": "string", "enum": [str(i) for i in chunk_ids]}}

    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "cited_chunk_ids": cited,
            "grounded": {"type": "boolean"},
        },
        "required": ["answer", "cited_chunk_ids", "grounded"],
    }


def _cited_ids(raw: list) -> list[int]:
    """Chunk ids as integers, whatever shape the provider returned them in.

    Groq is asked for strings; Gemini and Ollama may return numbers. Anything that is not
    a whole number is dropped rather than raising -- a malformed citation should cost one
    citation, not the entire answer.
    """
    ids = []
    for value in raw:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


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

    completion = await client.complete(prompt, schema=response_schema([h.chunk_id for h in hits]))
    payload = json.loads(completion.text)

    thresholds = _thresholds(request)
    decision = evaluate_gate(hits, grounded=payload["grounded"], thresholds=thresholds)

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
            for cid in _cited_ids(payload["cited_chunk_ids"])
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
        min_top_score=thresholds.min_top_score,
        min_margin=thresholds.min_margin,
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
        min_top_score=thresholds.min_top_score,
        min_margin=thresholds.min_margin,
    )
