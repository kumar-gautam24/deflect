import json

from deflect_common.llm.fake import FakeClient
from deflect_common.schemas import SearchRequest
from doubles import FakeAnswer, response

from evals.dataset import GoldenItem
from evals.runner import score_item


def item(item_id: str, escalate: bool = False, sources=("deps.md",)) -> GoldenItem:
    return GoldenItem(
        id=item_id,
        question=f"question {item_id}",
        ideal_answer="Use Depends.",
        expected_sources=[] if escalate else list(sources),
        should_escalate=escalate,
    )


def judged() -> str:
    return json.dumps(
        {
            "faithfulness": 1.0,
            "answer_relevance": 1.0,
            "context_relevance": 1.0,
            "rationale": "grounded",
        }
    )


async def test_a_scored_item_carries_its_retrieval_metrics():
    answer = FakeAnswer([response("Use Depends.", False)])

    result, _ = await score_item(item("q1"), answer, FakeClient([judged()]), None)

    assert result.item_id == "q1"
    assert result.escalated is False
    assert result.faithfulness == 1.0
    assert result.hit_at_5 in (0.0, 1.0)


async def test_an_escalated_item_is_not_judged():
    """Judging a refusal wastes tokens: there is no answer to score, and the escalation
    metrics already capture whether refusing was correct."""
    answer = FakeAnswer([response("", True)])
    judge = FakeClient([])  # would raise if asked for a completion

    result, _ = await score_item(item("q1", escalate=True), answer, judge, None)

    assert result.escalated is True
    assert result.faithfulness is None


async def test_the_item_question_is_what_gets_asked():
    answer = FakeAnswer([response("Use Depends.", False)])

    await score_item(item("q7"), answer, FakeClient([judged()]), None)

    assert answer.requests[0].question == "question q7"


async def test_a_search_variant_is_forwarded_with_the_item_question():
    """The variant decides retrieval; the question must still be the item's own, or an
    ablation would score every item against the same query."""
    answer = FakeAnswer([response("Use Depends.", False)])
    variant = SearchRequest(query="placeholder", use_rerank=False)

    await score_item(item("q3"), answer, FakeClient([judged()]), variant)

    sent = answer.requests[0].search
    assert sent.query == "question q3"
    assert sent.use_rerank is False


async def test_the_outcome_is_returned_alongside_the_row():
    """A run's provenance -- prompt version, model, the thresholds in force -- is only
    knowable once something has answered, and the run row is created before that."""
    answer = FakeAnswer([response("Use Depends.", False)])

    _, outcome = await score_item(item("q1"), answer, FakeClient([judged()]), None)

    assert outcome.prompt_version
    assert outcome.model
