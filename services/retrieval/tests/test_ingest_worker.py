from deflect_common.jobs import INGEST_STREAM, Delivery, FakeJobQueue

from retrieval.models import IngestJob
from retrieval.worker import MAX_ATTEMPTS, process_one


async def _queued(session, root: str = "/corpus") -> IngestJob:
    job = IngestJob(root=root, commit_sha="abc")
    session.add(job)
    await session.flush()
    return job


async def _claimed(queue: FakeJobQueue, job_id: int) -> Delivery:
    await queue.ensure_group(INGEST_STREAM)
    await queue.enqueue(INGEST_STREAM, job_id)
    return (await queue.claim(INGEST_STREAM, "w1", 10))[0]


async def test_a_successful_job_records_its_chunk_count(session):
    job = await _queued(session)
    queue = FakeJobQueue()

    async def ingest(db, root, sha) -> int:
        return 2370

    await process_one(session, queue, Delivery("1-0", job.id), ingest)

    assert job.status == "done"
    assert job.chunks == 2370


async def test_a_successful_job_is_acknowledged(session):
    job = await _queued(session)
    queue = FakeJobQueue()
    claimed = await _claimed(queue, job.id)

    async def ingest(db, root, sha) -> int:
        return 1

    await process_one(session, queue, claimed, ingest)

    assert await queue.pending_count(INGEST_STREAM) == 0


async def test_a_failing_job_is_left_pending_for_redelivery(session):
    """Not acknowledging is what makes the retry happen. Acknowledging a failure would
    silently drop the work."""
    job = await _queued(session)
    queue = FakeJobQueue()
    claimed = await _claimed(queue, job.id)

    async def ingest(db, root, sha) -> int:
        raise RuntimeError("disk fell over")

    await process_one(session, queue, claimed, ingest)

    assert job.status == "queued"
    assert job.attempts == 1
    assert await queue.pending_count(INGEST_STREAM) == 1


async def test_a_job_that_exhausts_its_attempts_fails_and_is_acknowledged(session):
    """A bounded retry is what stops a poisoned job redelivering forever."""
    job = await _queued(session)
    job.attempts = MAX_ATTEMPTS - 1
    queue = FakeJobQueue()
    claimed = await _claimed(queue, job.id)

    async def ingest(db, root, sha) -> int:
        raise RuntimeError("disk fell over")

    await process_one(session, queue, claimed, ingest)

    assert job.status == "failed"
    assert "disk fell over" in job.error
    assert await queue.pending_count(INGEST_STREAM) == 0


async def test_a_message_whose_job_row_is_missing_is_acknowledged(session):
    """Enqueue can succeed and its commit still fail. Without this, one unlucky crash
    leaves a message redelivering forever against a row that will never exist."""
    queue = FakeJobQueue()
    claimed = await _claimed(queue, 999999)

    async def ingest(db, root, sha) -> int:
        raise AssertionError("must not run")

    await process_one(session, queue, claimed, ingest)

    assert await queue.pending_count(INGEST_STREAM) == 0


async def test_an_already_finished_job_is_not_redone(session):
    """Redelivery after a successful run must not re-embed the corpus."""
    job = await _queued(session)
    job.status = "done"
    queue = FakeJobQueue()
    claimed = await _claimed(queue, job.id)

    async def ingest(db, root, sha) -> int:
        raise AssertionError("must not run")

    await process_one(session, queue, claimed, ingest)

    assert await queue.pending_count(INGEST_STREAM) == 0


async def test_the_job_is_marked_done_even_though_ingest_expunges_the_session(session):
    """ingest_directory calls expunge_all() after each document to bound memory, which
    detaches the job the worker is holding. Every other test here injects a fake that
    never touches the session, so none of them saw that writing through the detached
    object was silently discarded -- leaving the row at "running" forever while the
    message was acknowledged.
    """
    job = await _queued(session)
    queue = FakeJobQueue()

    async def expunging_ingest(db, root, sha) -> int:
        # Exactly what the real pipeline does between documents.
        await db.flush()
        db.expunge_all()
        return 2370

    await process_one(session, queue, Delivery("1-0", job.id), expunging_ingest)

    # Read it back rather than trusting the in-memory object, which is the whole point.
    from retrieval.models import IngestJob

    persisted = await session.get(IngestJob, job.id)
    assert persisted.status == "done"
    assert persisted.chunks == 2370
