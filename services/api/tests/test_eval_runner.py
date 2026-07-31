import json

import pytest
from sqlalchemy import select

from deflect.answer.gate import GateThresholds
from deflect.evals.dataset import GoldenItem
from deflect.evals.runner import run_evals
from deflect.llm.fake import FakeClient
from deflect.models import EvalResult
from deflect.retrieval.pipeline import RetrievalConfig

# Cross-encoder scores are unbounded logits, not 0-1 similarities: real queries
# range from about -12 to +8. A large finite floor is effectively permissive, and
# unlike -inf it survives being persisted as JSON with the eval run.
PERMISSIVE = GateThresholds(min_top_score=-1e9, min_margin=-1e9)
ANSWERABLE = "how do I declare a dependency"


def answer(text: str, grounded: bool = True) -> str:
    return json.dumps({"answer": text, "cited_chunk_ids": [], "grounded": grounded})


def judged() -> str:
    return json.dumps(
        {
            "faithfulness": 1.0,
            "answer_relevance": 1.0,
            "context_relevance": 1.0,
            "rationale": "ok",
        }
    )


def item(item_id: str, escalate: bool = False, sources=("deps.md",)) -> GoldenItem:
    return GoldenItem(
        id=item_id,
        question=ANSWERABLE,
        ideal_answer="Use Depends.",
        expected_sources=[] if escalate else list(sources),
        should_escalate=escalate,
    )


async def test_run_persists_a_result_per_item_and_aggregate_metrics(session, corpus):
    run = await run_evals(
        session,
        [item("q1"), item("q2")],
        FakeClient([answer("Use Depends.")] * 2),
        FakeClient([judged()] * 2),
        RetrievalConfig(),
        PERMISSIVE,
        git_sha="abc123",
    )

    results = (
        await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))
    ).scalars().all()
    assert len(results) == 2
    assert run.item_count == 2
    assert run.metrics["faithfulness"] == 1.0
    assert run.git_sha == "abc123"


async def test_run_records_configuration_needed_to_reproduce_it(session, corpus):
    run = await run_evals(
        session,
        [item("q1")],
        FakeClient([answer("x")]),
        FakeClient([judged()]),
        RetrievalConfig(use_rerank=False),
        PERMISSIVE,
        git_sha="sha",
    )

    assert run.retrieval_config["use_rerank"] is False
    assert run.prompt_version == "answer_v1"
    assert run.judge_version == "judge_v1"


async def test_unanswerable_item_that_escalates_is_not_judged(session, corpus):
    judge = FakeClient([])

    run = await run_evals(
        session,
        [item("u1", escalate=True)],
        FakeClient([answer("Not covered.", grounded=False)]),
        judge,
        RetrievalConfig(),
        PERMISSIVE,
        git_sha="sha",
    )

    assert judge.prompts == []
    assert run.metrics["escalation_recall"] == 1.0
    assert run.metrics["escalation_precision"] == 1.0


async def test_escalation_precision_penalizes_refusing_an_answerable_question(session, corpus):
    run = await run_evals(
        session,
        [item("q1")],
        FakeClient([answer("x", grounded=False)]),
        FakeClient([]),
        RetrievalConfig(),
        PERMISSIVE,
        git_sha="sha",
    )

    assert run.metrics["escalation_precision"] == 0.0
    assert run.metrics["answered_rate"] == 0.0


async def test_retrieval_metrics_ignore_items_that_should_escalate(session, corpus):
    run = await run_evals(
        session,
        [item("q1"), item("u1", escalate=True)],
        FakeClient([answer("Use Depends."), answer("No.", grounded=False)]),
        FakeClient([judged()]),
        RetrievalConfig(),
        PERMISSIVE,
        git_sha="sha",
    )

    # Only the answerable item contributes; an unanswerable item has no expected
    # source, so averaging it in would drag hit_at_5 toward zero for no reason.
    assert run.metrics["hit_at_5"] == 1.0


async def test_empty_dataset_is_rejected(session, corpus):
    with pytest.raises(ValueError, match="empty dataset"):
        await run_evals(
            session, [], FakeClient([]), FakeClient([]), RetrievalConfig(), PERMISSIVE, "sha"
        )
