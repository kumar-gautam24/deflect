import pytest
from sqlalchemy.exc import IntegrityError

from evals.models import EvalItemJob, EvalResult, EvalRun


def _run() -> EvalRun:
    return EvalRun(
        git_sha="abc",
        prompt_version="v1",
        judge_version="v1",
        model="m",
        retrieval_config={},
        thresholds={},
        item_count=0,
        metrics={},
        items_total=2,
        status="running",
    )


def _result(run_id: int, item_id: str) -> EvalResult:
    return EvalResult(
        run_id=run_id,
        item_id=item_id,
        question="q",
        answer="a",
        escalated=False,
        expected_escalate=False,
        retrieved_sources=[],
        hit_at_5=1.0,
        mrr=1.0,
    )


async def test_a_run_starts_running_with_a_target(session):
    run = _run()
    session.add(run)
    await session.flush()

    assert run.status == "running"
    assert run.items_total == 2


async def test_the_same_item_cannot_be_scored_twice_for_one_run(session):
    """At-least-once delivery means a worker that wrote its result and died before
    acknowledging sees the item again. Without this the score is counted twice and the
    metrics are quietly wrong."""
    run = _run()
    session.add(run)
    await session.flush()
    session.add(_result(run.id, "q1"))
    await session.flush()

    # Inside a savepoint: the violation aborts whatever transaction it happens in, and
    # without this the fixture's outer rollback has nothing left to roll back and warns.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(_result(run.id, "q1"))


async def test_the_same_item_may_appear_in_different_runs(session):
    first, second = _run(), _run()
    session.add_all([first, second])
    await session.flush()

    session.add_all([_result(first.id, "q1"), _result(second.id, "q1")])
    await session.flush()  # must not raise


async def test_an_item_job_tracks_its_own_attempts(session):
    run = _run()
    session.add(run)
    await session.flush()

    job = EvalItemJob(run_id=run.id, item_id="q1")
    session.add(job)
    await session.flush()

    assert job.status == "queued"
    assert job.attempts == 0
