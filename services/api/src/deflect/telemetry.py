"""Token cost accounting and trace persistence."""

from sqlalchemy.ext.asyncio import AsyncSession

from deflect.answer.service import AnswerResult
from deflect.models import Escalation, Trace

# USD per million tokens, input and output. Unpriced models cost nothing to record.
PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-pro": (1.25, 5.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in PRICING:
        return 0.0
    input_price, output_price = PRICING[model]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


async def record_trace(
    session: AsyncSession, question: str, result: AnswerResult, latency_ms: int
) -> Trace:
    trace = Trace(
        question=question,
        answer=result.answer,
        escalated=result.decision.escalate,
        reason=result.decision.reason,
        top_score=result.decision.top_score,
        margin=result.decision.margin,
        retrieved=[
            {"chunk_id": h.chunk_id, "source_path": h.source_path, "score": h.score}
            for h in result.hits
        ],
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=estimate_cost(result.model, result.input_tokens, result.output_tokens),
        model=result.model,
        prompt_version=result.prompt_version,
        latency_ms=latency_ms,
    )
    session.add(trace)
    await session.flush()

    if result.decision.escalate:
        session.add(
            Escalation(trace_id=trace.id, question=question, reason=result.decision.reason)
        )
        await session.flush()

    return trace
