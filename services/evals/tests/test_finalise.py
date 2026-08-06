from evals.finalise import finalise_if_complete
from evals.models import EvalItemJob, EvalResult, EvalRun


async def _run(session, items_total: int = 2) -> EvalRun:
    run = EvalRun(
        git_sha="abc", prompt_version="", judge_version="v1", model="m",
        retrieval_config={}, thresholds={}, item_count=0, metrics={},
        items_total=items_total, status="running",
    )
    session.add(run)
    await session.flush()
    return run


async def _job(session, run_id: int, item_id: str, status: str) -> None:
    session.add(EvalItemJob(run_id=run_id, item_id=item_id, status=status))
    await session.flush()


async def _result(session, run_id: int, item_id: str) -> None:
    session.add(
        EvalResult(
            run_id=run_id, item_id=item_id, question="q", answer="a", escalated=False,
            expected_escalate=False, retrieved_sources=[], hit_at_5=1.0, mrr=1.0,
            faithfulness=1.0, answer_relevance=1.0, context_relevance=1.0,
        )
    )
    await session.flush()


async def test_an_incomplete_run_is_left_alone(session):
    run = await _run(session)
    await _job(session, run.id, "q1", "done")

    assert await finalise_if_complete(session, run.id) is False
    assert run.status == "running"


async def test_a_complete_run_gets_its_aggregates(session):
    run = await _run(session)
    for item in ("q1", "q2"):
        await _job(session, run.id, item, "done")
        await _result(session, run.id, item)

    assert await finalise_if_complete(session, run.id) is True
    assert run.status == "complete"
    assert run.item_count == 2
    assert run.metrics["faithfulness"] == 1.0


async def test_a_failed_item_still_completes_the_run(session):
    """Counting results instead of jobs would leave this run stalled at one of two
    forever, looking like work still in progress."""
    run = await _run(session)
    await _job(session, run.id, "q1", "done")
    await _result(session, run.id, "q1")
    await _job(session, run.id, "q2", "failed")

    assert await finalise_if_complete(session, run.id) is True
    assert run.status == "complete"
    # item_count reports what was scored, so the loss is visible.
    assert run.item_count == 1


async def test_finalising_twice_is_a_no_op(session):
    """Two workers can finish simultaneously. Exactly one may write aggregates."""
    run = await _run(session)
    for item in ("q1", "q2"):
        await _job(session, run.id, item, "done")
        await _result(session, run.id, item)

    assert await finalise_if_complete(session, run.id) is True
    assert await finalise_if_complete(session, run.id) is False


async def test_a_run_with_no_scores_completes_rather_than_hanging(session):
    run = await _run(session)
    for item in ("q1", "q2"):
        await _job(session, run.id, item, "failed")

    assert await finalise_if_complete(session, run.id) is True
    assert run.item_count == 0


async def test_a_run_that_does_not_exist_is_not_finalised(session):
    assert await finalise_if_complete(session, 999999) is False
