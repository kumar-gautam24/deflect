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
    await session.flush()

    try:
        job.chunks = await ingest(session, Path(job.root), job.commit_sha)
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
            async with SessionFactory() as session:
                await process_one(session, queue, delivery)


def main() -> None:
    from deflect_common.logging import configure_logging

    configure_logging()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
