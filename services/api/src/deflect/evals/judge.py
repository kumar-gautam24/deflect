"""LLM-as-judge scoring of generated answers."""

import json
from dataclasses import dataclass
from pathlib import Path

from deflect.evals.dataset import GoldenItem
from deflect.llm.base import LLMClient
from deflect.retrieval.search import Hit

JUDGE_VERSION = "judge_v1"
PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / f"{JUDGE_VERSION}.md").read_text()

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "faithfulness": {"type": "number"},
        "answer_relevance": {"type": "number"},
        "context_relevance": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["faithfulness", "answer_relevance", "context_relevance", "rationale"],
}


@dataclass(frozen=True)
class JudgeScores:
    faithfulness: float
    answer_relevance: float
    context_relevance: float
    rationale: str


async def judge_answer(
    client: LLMClient, item: GoldenItem, answer: str, hits: list[Hit]
) -> JudgeScores:
    context = "\n\n".join(f"{h.heading_path}\n{h.text}" for h in hits)
    prompt = PROMPT_TEMPLATE.format(
        question=item.question,
        ideal_answer=item.ideal_answer,
        context=context,
        answer=answer,
    )

    completion = await client.complete(prompt, schema=SCORE_SCHEMA)
    payload = json.loads(completion.text)

    scores = JudgeScores(
        faithfulness=payload["faithfulness"],
        answer_relevance=payload["answer_relevance"],
        context_relevance=payload["context_relevance"],
        rationale=payload["rationale"],
    )
    # An out-of-range score means the judge misread its instructions; averaging it
    # into a run would silently corrupt the whole run's metrics.
    for value in (scores.faithfulness, scores.answer_relevance, scores.context_relevance):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"judge returned an out-of-range score: {value}")

    return scores
