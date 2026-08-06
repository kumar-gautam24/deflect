"""Eval item worker.

Runs from the evals image as a different command: it needs this service's database and
judge client, so a shared generic worker would have to carry every service's dependencies.
"""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable

from deflect_common.jobs import EVAL_ITEM_STREAM, Delivery, JobQueue, RedisJobQueue
from deflect_common.llm.base import get_client
from deflect_common.schemas import AnswerResponse
from sqlalchemy.ext.asyncio import AsyncSession

from evals.answer_client import AnswerClient
from evals.config import get_settings
from evals.dataset import load_dataset
from evals.db import SessionFactory
from evals.finalise import finalise_if_complete
from evals.models import EvalItemJob, EvalResult, EvalRun
from evals.runner import score_item

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# Comfortably longer than one item, which is roughly ninety seconds against a free tier.
STALE_AFTER_MS = 15 * 60 * 1000

ScoreFn = Callable[[str], Awaitable[tuple[EvalResult, AnswerResponse]]]


async def _record_provenance(session: AsyncSession, run_id: int, outcome: AnswerResponse) -> None:
    """Fill in what actually answered, once anything has.

    The run row is created before a single item has run, so the prompt version, the model
    and the gate thresholds in force are unknown at that point. The first item to succeed
    supplies them; later ones find them already set and leave them alone.
    """
    run = await session.get(EvalRun, run_id)
    if run is None or run.prompt_version:
        return

    run.prompt_version = outcome.prompt_version
    run.model = outcome.model
    run.thresholds = {
        "min_top_score": outcome.min_top_score,
        "min_margin": outcome.min_margin,
    }


async def process_one(
    session: AsyncSession, queue: JobQueue, delivery: Delivery, score: ScoreFn
) -> None:
    """Score one item, persist it, and finalise the run if this was the last one."""
    job = await session.get(EvalItemJob, delivery.job_id)

    if job is None:
        # Enqueue can succeed and its commit still fail. Without acknowledging here, one
        # unlucky crash leaves a message redelivering forever against a row that will
        # never exist.
        logger.warning("eval item job %s has no row; acknowledging", delivery.job_id)
        await queue.acknowledge(EVAL_ITEM_STREAM, delivery.message_id)
        return

    if job.status in ("done", "failed"):
        # Redelivery after a finished item must not score it twice.
        await queue.acknowledge(EVAL_ITEM_STREAM, delivery.message_id)
        return

    job.status = "running"
    job.attempts += 1
    await session.flush()

    try:
        result, outcome = await score(job.item_id)
    except Exception as cause:  # noqa: BLE001 - the failure is recorded, not swallowed
        job.error = str(cause)[:1000]
        if job.attempts >= MAX_ATTEMPTS:
            job.status = "failed"
            # A permanently failed item must still let the run finish, or it stalls one
            # short forever looking like work in progress.
            await finalise_if_complete(session, job.run_id)
            await session.commit()
            await queue.acknowledge(EVAL_ITEM_STREAM, delivery.message_id)
            logger.error("eval item %s failed permanently: %s", job.item_id, cause)
            return

        # Left unacknowledged on purpose: that is what makes the retry happen.
        job.status = "queued"
        await session.commit()
        logger.warning("eval item %s failed, attempt %s: %s", job.item_id, job.attempts, cause)
        return

    result.run_id = job.run_id
    session.add(result)
    job.status = "done"
    job.error = None

    await _record_provenance(session, job.run_id, outcome)
    await finalise_if_complete(session, job.run_id)
    await session.commit()
    await queue.acknowledge(EVAL_ITEM_STREAM, delivery.message_id)


async def run_worker() -> None:
    settings = get_settings()
    queue = RedisJobQueue(settings.redis_url)
    # Before any claim: real Redis answers NOGROUP if the group was never created, and a
    # worker starting before the first producer is the normal order on a cold deploy.
    await queue.ensure_group(EVAL_ITEM_STREAM)
    consumer = socket.gethostname()

    answer_client = AnswerClient(settings.answer_url, settings.service_token)
    judge_client = get_client(
        provider=settings.llm_provider,
        model=settings.judge_model,
        api_key=settings.provider_api_key,
        base_url=settings.ollama_base_url,
    )

    async def score(item_id: str) -> tuple[EvalResult, AnswerResponse]:
        # Looked up by id rather than read off the job row: the dataset on disk is the
        # single source of truth for what an item says, and copying the question into the
        # row would let the two drift apart silently.
        items = {i.id: i for i in load_dataset(settings.dataset_path)}
        return await score_item(items[item_id], answer_client, judge_client, None)

    logger.info("eval worker %s consuming %s", consumer, EVAL_ITEM_STREAM)
    while True:
        # Reclaim before reading: an item abandoned by a dead worker should be retried
        # before new work starts, or it waits behind the whole backlog.
        deliveries = await queue.reclaim_stale(EVAL_ITEM_STREAM, consumer, STALE_AFTER_MS)
        deliveries += await queue.claim(EVAL_ITEM_STREAM, consumer, count=1)

        for delivery in deliveries:
            async with SessionFactory() as session:
                await process_one(session, queue, delivery, score)


def main() -> None:
    from deflect_common.logging import configure_logging

    configure_logging()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
