import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from deflect.answer.gate import GateThresholds
from deflect.answer.service import answer_question
from deflect.db import SessionDep
from deflect.llm.base import ClientDep
from deflect.retrieval.pipeline import RetrievalConfig
from deflect.telemetry import record_trace

router = APIRouter()

# Cross-encoder logits, not similarities. Chosen in Task 20 from the swept curve;
# measured separation is roughly +6 for answerable questions against -0.1 or lower
# for questions the corpus does not cover.
THRESHOLDS = GateThresholds(min_top_score=2.0, min_margin=0.0)


class AskRequest(BaseModel):
    question: str


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/ask")
async def ask(
    request: AskRequest,
    session: SessionDep,
    client: ClientDep,
) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        started = time.monotonic()
        result = await answer_question(
            session, request.question, client, RetrievalConfig(), THRESHOLDS
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        trace = await record_trace(session, request.question, result, latency_ms)
        await session.commit()

        # The provider returns structured JSON in one call, so streaming is done by
        # chunking the finished answer. This keeps one code path for app and evals.
        for word in result.answer.split(" "):
            yield _event({"type": "token", "text": word + " "})
            await asyncio.sleep(0)

        yield _event(
            {
                "type": "done",
                "trace_id": trace.id,
                "escalated": result.decision.escalate,
                "reason": result.decision.reason,
                "citations": [
                    {
                        "source_path": c.source_path,
                        "heading_path": c.heading_path,
                        "chunk_id": c.chunk_id,
                    }
                    for c in result.citations
                ],
                "latency_ms": latency_ms,
            }
        )

    return StreamingResponse(stream(), media_type="text/event-stream")
