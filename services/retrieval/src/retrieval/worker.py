"""Ingest worker.

Runs from the same image as the service, as a different command. It needs this service's
database and embedder, so a shared generic worker would have to carry every service's
dependencies -- the coupling database-per-service exists to prevent.
"""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

from deflect_common.jobs import INGEST_STREAM, Delivery, JobQueue, RedisJobQueue
from sqlalchemy.ext.asyncio import AsyncSession

from retrieval.config import get_settings
from retrieval.db import SessionFactory
from retrieval.ingest.pipeline import ingest_directory
from retrieval.models import IngestJob

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# Long enough that a slow ingest is not reclaimed from a worker still doing it. Embedding
# the FastAPI corpus takes minutes, not seconds.
STALE_AFTER_MS = 30 * 60 * 1000

IngestFn = Callable[[AsyncSession, Path, str], Awaitable[int]]


async def process_one(
    session: AsyncSession,
    queue: JobQueue,
    delivery: Delivery,
    ingest: IngestFn = ingest_directory,
) -> None:
    """Run one job, then decide whether to acknowledge it.

    Acknowledging is the decision that matters. A success or a permanent failure is
    acknowledged; a retryable failure deliberately is not, so the visibility timeout
    redelivers it.
    """
    job = await session.get(IngestJob, delivery.job_id)

    if job is None:
        # Enqueue can succeed and its commit still fail. Without acknowledging here, one
        # unlucky crash leaves a message redelivering forever against a row that will
        # never exist.
        logger.warning("ingest job %s has no row; acknowledging", delivery.job_id)
        await queue.acknowledge(INGEST_STREAM, delivery.message_id)
        return

    if job.status in ("done", "failed"):
        # Redelivery after a finished run must not re-embed the corpus.
        await queue.acknowledge(INGEST_STREAM, delivery.message_id)
        return

    job.status = "running"
    job.attempts += 1
    # Committed, not just flushed. Uncommitted, no other connection can see it, so a job
    # that is actively running reads as "queued" to /jobs and to the dashboard for its
    # whole duration -- which for an ingest is minutes and for a run is hours.
    #
    # It also makes the attempt count durable: rolled back by a crash, a worker that
    # died mid-job would retry forever without ever reaching MAX_ATTEMPTS.
    await session.commit()

    try:
        chunks = await ingest(session, Path(job.root), job.commit_sha)
    except Exception as cause:  # noqa: BLE001 - the failure is recorded, not swallowed
        job.error = str(cause)[:1000]
        if job.attempts >= MAX_ATTEMPTS:
            job.status = "failed"
            await session.commit()
            await queue.acknowledge(INGEST_STREAM, delivery.message_id)
            logger.error("ingest job %s failed permanently: %s", job.id, cause)
            return

        # Left unacknowledged on purpose: that is what makes the retry happen.
        job.status = "queued"
        await session.commit()
        logger.warning("ingest job %s failed, attempt %s: %s", job.id, job.attempts, cause)
        return

    # ingest_directory calls expunge_all() after each document to bound memory, which
    # detaches this job along with everything else. Writing through the detached object
    # would be silently discarded at commit, leaving the row at "running" forever while
    # the message was acknowledged -- a job that can never finish and never retry.
    job = await session.get(IngestJob, delivery.job_id)
    job.chunks = chunks
    job.status = "done"
    job.error = None
    await session.commit()
    await queue.acknowledge(INGEST_STREAM, delivery.message_id)


async def run_worker() -> None:
    settings = get_settings()
    queue = RedisJobQueue(settings.redis_url)
    # Before any claim: real Redis answers NOGROUP if the group was never created, and a
    # worker starting before the first producer is the normal order on a cold deploy.
    await queue.ensure_group(INGEST_STREAM)
    consumer = socket.gethostname()

    logger.info("ingest worker %s consuming %s", consumer, INGEST_STREAM)
    while True:
        # Reclaim before reading: a job abandoned by a dead worker should be retried
        # before new work is started, or it waits behind the whole backlog.
        deliveries = await queue.reclaim_stale(INGEST_STREAM, consumer, STALE_AFTER_MS)
        deliveries += await queue.claim(INGEST_STREAM, consumer, count=1)

        for delivery in deliveries:
            try:
                async with SessionFactory() as session:
                    await process_one(session, queue, delivery)
            except Exception:  # noqa: BLE001 - a transient fault must not end the worker
                # Unhandled, anything here -- a Redis blip, a deadlock, a poisoned
                # session -- exits asyncio.run and the container stays dead with the
                # queue still full. The message is left unacknowledged, so the work
                # returns after the visibility timeout.
                logger.exception("ingest job %s raised; leaving it unacknowledged", delivery.job_id)


def main() -> None:
    from deflect_common.logging import configure_logging

    configure_logging()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
