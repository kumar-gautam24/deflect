from deflect_common.jobs import EVAL_ITEM_STREAM, Delivery, FakeJobQueue
from deflect_common.schemas import AnswerResponse
from sqlalchemy import func, select

from evals.models import EvalItemJob, EvalResult, EvalRun
from evals.worker import MAX_ATTEMPTS, process_one


async def _run_with_one_item(session) -> tuple[EvalRun, EvalItemJob]:
    run = EvalRun(
        git_sha="abc", prompt_version="", judge_version="v1", model="m",
        retrieval_config={}, thresholds={}, item_count=0, metrics={},
        items_total=1, status="running",
    )
    session.add(run)
    await session.flush()
    job = EvalItemJob(run_id=run.id, item_id="q1")
    session.add(job)
    await session.flush()
    return run, job


def _row(item_id: str = "q1") -> EvalResult:
    return EvalResult(
        item_id=item_id, question="q", answer="a", escalated=False,
        expected_escalate=False, retrieved_sources=[], hit_at_5=1.0, mrr=1.0,
        faithfulness=1.0, answer_relevance=1.0, context_relevance=1.0,
    )


def _outcome() -> AnswerResponse:
    return AnswerResponse(
        trace_id=1, answer="a", citations=[], escalated=False, reason=None,
        top_score=5.0, margin=1.0, hits=[], input_tokens=1, output_tokens=1,
        cost_usd=0.0, model="gpt-oss-20b", prompt_version="answer_v1",
        latency_ms=1, min_top_score=2.0, min_margin=0.0,
    )


async def _claimed(queue: FakeJobQueue, job_id: int) -> Delivery:
    await queue.ensure_group(EVAL_ITEM_STREAM)
    await queue.enqueue(EVAL_ITEM_STREAM, job_id)
    return (await queue.claim(EVAL_ITEM_STREAM, "w1", 10))[0]


async def _score(item_id: str):
    return _row(item_id), _outcome()


async def test_a_scored_item_is_persisted_and_acknowledged(session):
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()
    claimed = await _claimed(queue, job.id)

    await process_one(session, queue, claimed, _score)

    assert job.status == "done"
    assert await queue.pending_count(EVAL_ITEM_STREAM) == 0


async def test_the_last_item_finalises_its_run(session):
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()

    await process_one(session, queue, Delivery("1-0", job.id), _score)

    assert run.status == "complete"
    assert run.item_count == 1


async def test_the_first_success_records_the_run_provenance(session):
    """The run row is created before anything has answered, so the prompt version, model
    and thresholds in force are unknown until an item succeeds."""
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()

    await process_one(session, queue, Delivery("1-0", job.id), _score)

    assert run.prompt_version == "answer_v1"
    assert run.model == "gpt-oss-20b"
    assert run.thresholds == {"min_top_score": 2.0, "min_margin": 0.0}


async def test_a_redelivered_item_does_not_score_twice(session):
    """The job status guard plus the unique constraint make at-least-once delivery
    harmless. Without them the score is counted twice and the metrics are quietly wrong."""
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()

    await process_one(session, queue, Delivery("1-0", job.id), _score)
    await process_one(session, queue, Delivery("1-0", job.id), _score)

    count = (
        await session.execute(
            select(func.count()).select_from(EvalResult).where(EvalResult.run_id == run.id)
        )
    ).scalar_one()
    assert count == 1


async def test_a_failing_item_is_retried_before_it_is_failed(session):
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()
    claimed = await _claimed(queue, job.id)

    async def score(item_id: str):
        raise RuntimeError("provider timed out")

    await process_one(session, queue, claimed, score)

    assert job.status == "queued"
    assert job.attempts == 1
    assert await queue.pending_count(EVAL_ITEM_STREAM) == 1
    assert run.status == "running"


async def test_an_exhausted_item_fails_and_lets_the_run_finish(session):
    run, job = await _run_with_one_item(session)
    job.attempts = MAX_ATTEMPTS - 1
    queue = FakeJobQueue()
    claimed = await _claimed(queue, job.id)

    async def score(item_id: str):
        raise RuntimeError("provider timed out")

    await process_one(session, queue, claimed, score)

    assert job.status == "failed"
    assert run.status == "complete"
    assert run.item_count == 0
    assert await queue.pending_count(EVAL_ITEM_STREAM) == 0


async def test_a_message_whose_job_row_is_missing_is_acknowledged(session):
    queue = FakeJobQueue()
    claimed = await _claimed(queue, 999999)

    async def score(item_id: str):
        raise AssertionError("must not run")

    await process_one(session, queue, claimed, score)

    assert await queue.pending_count(EVAL_ITEM_STREAM) == 0


async def test_a_duplicate_score_does_not_kill_the_worker(session):
    """Reclaiming cannot tell a dead worker from a slow one, so a long item gets handed
    to a second worker while the first still holds it. Both insert, the unique constraint
    stops the double count -- and unhandled, that IntegrityError would propagate out of
    the run loop and kill the container with a queue still full."""
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()
    claimed = await _claimed(queue, job.id)

    # A row already exists for this item, as the other worker would have written.
    duplicate = _row("q1")
    duplicate.run_id = run.id
    session.add(duplicate)
    await session.flush()
    job.status = "queued"

    await process_one(session, queue, claimed, _score)

    assert await queue.pending_count(EVAL_ITEM_STREAM) == 0
