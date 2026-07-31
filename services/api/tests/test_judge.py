import json

import pytest

from deflect.evals.dataset import GoldenItem
from deflect.evals.judge import judge_answer
from deflect.llm.fake import FakeClient
from deflect.retrieval.search import Hit

ITEM = GoldenItem("q1", "How do I declare a dependency?", "Use Depends.", ["deps.md"], False)
HITS = [Hit(1, 1, "deps.md", "Dependencies", "Use Depends to declare a dependency.", 0.9)]


def scores(faithfulness=1.0, answer_relevance=1.0, context_relevance=1.0) -> str:
    return json.dumps(
        {
            "faithfulness": faithfulness,
            "answer_relevance": answer_relevance,
            "context_relevance": context_relevance,
            "rationale": "supported by the context",
        }
    )


async def test_judge_returns_the_three_ragas_scores():
    result = await judge_answer(FakeClient([scores()]), ITEM, "Use Depends.", HITS)

    assert result.faithfulness == 1.0
    assert result.answer_relevance == 1.0
    assert result.context_relevance == 1.0
    assert result.rationale


async def test_judge_prompt_contains_question_ideal_answer_context_and_answer():
    client = FakeClient([scores()])

    await judge_answer(client, ITEM, "Use Depends.", HITS)

    prompt = client.prompts[0]
    assert ITEM.question in prompt
    assert ITEM.ideal_answer in prompt
    assert "Use Depends to declare a dependency." in prompt


async def test_scores_above_the_unit_interval_are_rejected():
    with pytest.raises(ValueError, match="out-of-range"):
        await judge_answer(FakeClient([scores(faithfulness=1.7)]), ITEM, "x", HITS)


async def test_scores_below_the_unit_interval_are_rejected():
    with pytest.raises(ValueError, match="out-of-range"):
        await judge_answer(FakeClient([scores(context_relevance=-0.2)]), ITEM, "x", HITS)
