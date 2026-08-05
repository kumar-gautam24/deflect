import json

import httpx
import pytest
from deflect_common.llm.fake import FakeClient
from deflect_common.schemas import SearchRequest
from doubles import FakeAnswer, response
from sqlalchemy import select

from evals.dataset import GoldenItem
from evals.models import EvalResult
from evals.runner import run_evals


def item(item_id: str, escalate: bool = False, sources=("deps.md",)) -> GoldenItem:
    return GoldenItem(
        id=item_id,
        question="how do I declare a dependency",
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
            "rationale": "ok",
        }
    )


async def test_run_persists_a_result_per_item_and_aggregate_metrics(session):
    run = await run_evals(
        session,
        [item("q1"), item("q2")],
        FakeAnswer([response("Use Depends.", False), response("Use Depends.", False)]),
        FakeClient([judged()] * 2),
        None,
        git_sha="abc123",
    )

    results = (
        (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(results) == 2
    assert run.item_count == 2
    assert run.metrics["faithfulness"] == 1.0
    assert run.git_sha == "abc123"


async def test_run_records_what_the_answer_service_reported(session):
    run = await run_evals(
        session,
        [item("q1")],
        FakeAnswer([response("x", False)]),
        FakeClient([judged()]),
        None,
        git_sha="sha",
    )

    # Reproducibility comes from what the answer service reported, not from a local
    # guess: this service no longer owns the gate configuration.
    assert run.prompt_version == "answer_v1"
    assert run.judge_version == "judge_v1"
    assert run.thresholds == {"min_top_score": 2.0, "min_margin": 0.0}


async def test_escalated_item_is_not_judged(session):
    judge = FakeClient([])

    run = await run_evals(
        session,
        [item("u1", escalate=True)],
        FakeAnswer([response("Not covered.", True)]),
        judge,
        None,
        git_sha="sha",
    )

    assert judge.prompts == []
    assert run.metrics["escalation_recall"] == 1.0
    assert run.metrics["escalation_precision"] == 1.0


async def test_escalation_precision_penalizes_refusing_an_answerable_question(session):
    run = await run_evals(
        session,
        [item("q1")],
        FakeAnswer([response("x", True)]),
        FakeClient([]),
        None,
        git_sha="sha",
    )

    assert run.metrics["escalation_precision"] == 0.0
    assert run.metrics["answered_rate"] == 0.0


async def test_retrieval_metrics_ignore_items_that_should_escalate(session):
    run = await run_evals(
        session,
        [item("q1"), item("u1", escalate=True)],
        FakeAnswer([response("Use Depends.", False), response("No.", True)]),
        FakeClient([judged()]),
        None,
        git_sha="sha",
    )

    # Only the answerable item contributes; an unanswerable item has no expected
    # source, so averaging it in would drag hit_at_5 toward zero for no reason.
    assert run.metrics["hit_at_5"] == 1.0


async def test_each_item_is_sent_as_its_own_question(session):
    answer = FakeAnswer([response("x", False), response("y", False)])

    await run_evals(
        session,
        [item("q1"), item("q2")],
        answer,
        FakeClient([judged()] * 2),
        None,
        git_sha="sha",
    )

    assert [r.question for r in answer.requests] == [
        "how do I declare a dependency",
        "how do I declare a dependency",
    ]


async def test_a_search_variant_is_forwarded_with_the_item_question(session):
    answer = FakeAnswer([response("x", False)])
    variant = SearchRequest(query="ignored", use_rerank=False, final_limit=3)

    run = await run_evals(
        session, [item("q1")], answer, FakeClient([judged()]), variant, git_sha="sha"
    )

    # The query is replaced per item; the rest of the variant is what the run is
    # measuring, so it must survive to the answer service unchanged.
    forwarded = answer.requests[0].search
    assert forwarded.query == "how do I declare a dependency"
    assert forwarded.use_rerank is False
    assert forwarded.final_limit == 3
    assert run.retrieval_config["use_rerank"] is False


async def test_empty_dataset_is_rejected(session):
    with pytest.raises(ValueError, match="empty dataset"):
        await run_evals(session, [], FakeAnswer([]), FakeClient([]), None, "sha")


async def test_one_failing_item_does_not_discard_the_whole_run(session):
    """A full pass takes roughly 110 minutes against a free-tier quota, so aborting at
    item 47 would throw away 45 minutes and every score already computed."""

    class FlakyAnswer(FakeAnswer):
        """Fails exactly once, on the second item."""

        calls = 0

        async def answer(self, request):
            type(self).calls += 1
            if type(self).calls == 2:
                raise httpx.ReadTimeout("provider took too long")
            return await super().answer(request)

    run = await run_evals(
        session,
        [item("q1"), item("q2"), item("q3")],
        FlakyAnswer([response("Use Depends.", False)] * 3),
        FakeClient([judged()] * 3),
        None,
        git_sha="abc123",
    )

    # Two of three scored: the run survives, and its lower item_count is what makes the
    # loss visible rather than silent.
    assert run.item_count == 2
