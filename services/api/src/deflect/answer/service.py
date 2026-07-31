"""The single answer code path. The API route and the eval runner both call this."""

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from deflect.answer.gate import GateDecision, GateThresholds, evaluate_gate
from deflect.llm.base import LLMClient
from deflect.retrieval.pipeline import RetrievalConfig, retrieve
from deflect.retrieval.search import Hit

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


@dataclass(frozen=True)
class Citation:
    source_path: str
    heading_path: str
    chunk_id: int


@dataclass(frozen=True)
class AnswerResult:
    answer: str
    citations: list[Citation]
    decision: GateDecision
    hits: list[Hit]
    input_tokens: int
    output_tokens: int
    model: str
    prompt_version: str


def _format_context(hits: list[Hit]) -> str:
    return "\n\n".join(f"[id: {hit.chunk_id}] {hit.heading_path}\n{hit.text}" for hit in hits)


async def answer_question(
    session: AsyncSession,
    question: str,
    client: LLMClient,
    retrieval_config: RetrievalConfig,
    thresholds: GateThresholds,
) -> AnswerResult:
    hits = await retrieve(session, question, retrieval_config)
    prompt = PROMPT_TEMPLATE.format(context=_format_context(hits), question=question)

    completion = await client.complete(prompt, schema=RESPONSE_SCHEMA)
    payload = json.loads(completion.text)

    decision = evaluate_gate(hits, grounded=payload["grounded"], thresholds=thresholds)

    # Citations are resolved against retrieved chunks, so a hallucinated id cannot
    # produce a citation that links nowhere.
    by_id = {hit.chunk_id: hit for hit in hits}
    citations = (
        []
        if decision.escalate
        else [
            Citation(by_id[cid].source_path, by_id[cid].heading_path, cid)
            for cid in payload["cited_chunk_ids"]
            if cid in by_id
        ]
    )

    return AnswerResult(
        answer=payload["answer"],
        citations=citations,
        decision=decision,
        hits=hits,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        model=completion.model,
        prompt_version=PROMPT_VERSION,
    )
