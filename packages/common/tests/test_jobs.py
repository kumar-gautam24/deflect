import pytest

from deflect_common.jobs import EVAL_ITEM_STREAM, Delivery, FakeJobQueue


async def test_a_claimed_job_carries_its_id():
    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)
    await queue.enqueue(EVAL_ITEM_STREAM, 42)

    claimed = await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert [d.job_id for d in claimed] == [42]


async def test_an_unacknowledged_job_stays_pending():
    """Acknowledging after the work rather than on receipt is the whole reason for
    choosing streams: a worker that dies mid-job must not lose it."""
    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert await queue.pending_count(EVAL_ITEM_STREAM) == 1


async def test_acknowledging_clears_the_pending_entry():
    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    claimed = await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    await queue.acknowledge(EVAL_ITEM_STREAM, claimed[0].message_id)

    assert await queue.pending_count(EVAL_ITEM_STREAM) == 0


async def test_a_claimed_job_is_not_handed_to_a_second_consumer():
    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert await queue.claim(EVAL_ITEM_STREAM, consumer="w2", count=10) == []


async def test_a_stale_job_can_be_reclaimed_by_another_consumer():
    """A worker that dies leaves its message pending; reclaiming is what turns a crash
    into a retry rather than a lost job."""
    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    reclaimed = await queue.reclaim_stale(EVAL_ITEM_STREAM, consumer="w2", min_idle_ms=0)

    assert [d.job_id for d in reclaimed] == [42]


async def test_reclaiming_leaves_a_fresh_job_alone():
    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert await queue.reclaim_stale(EVAL_ITEM_STREAM, consumer="w2", min_idle_ms=60_000) == []


async def test_claiming_an_empty_stream_returns_nothing():
    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)

    assert await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10) == []


async def test_jobs_are_delivered_in_order():
    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)
    for job_id in (1, 2, 3):
        await queue.enqueue(EVAL_ITEM_STREAM, job_id)

    claimed = await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert [d.job_id for d in claimed] == [1, 2, 3]


async def test_streams_are_independent():
    from deflect_common.jobs import INGEST_STREAM

    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)
    await queue.enqueue(INGEST_STREAM, 1)

    assert await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10) == []


def test_a_delivery_is_hashable_and_comparable():
    """Workers deduplicate deliveries in a set when a reclaim overlaps a read."""
    assert Delivery("1-0", 7) == Delivery("1-0", 7)
    assert len({Delivery("1-0", 7), Delivery("1-0", 7)}) == 1


async def test_enqueue_rejects_a_non_integer_job_id():
    """The envelope carries only an id; anything else means payload leaked into the
    message, where it could disagree with the row it refers to."""
    with pytest.raises((TypeError, ValueError)):
        await FakeJobQueue().enqueue(EVAL_ITEM_STREAM, "not-an-id")  # type: ignore[arg-type]


async def test_claiming_without_a_group_fails_as_redis_would():
    """A fake that needs no setup would let a worker pass every test and then die on
    NOGROUP against a real broker."""
    with pytest.raises(RuntimeError, match="ensure_group"):
        await FakeJobQueue().claim(EVAL_ITEM_STREAM, consumer="w1", count=10)


async def test_ensure_group_is_idempotent():
    """Workers call it on every start, and a second call must not fail."""
    queue = FakeJobQueue()
    await queue.ensure_group(EVAL_ITEM_STREAM)
    await queue.ensure_group(EVAL_ITEM_STREAM)

    await queue.enqueue(EVAL_ITEM_STREAM, 1)
    assert len(await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)) == 1
