"""Turning finished items into one scored run."""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from evals.models import EvalItemJob, EvalResult, EvalRun
from evals.runner import _aggregate

logger = logging.getLogger(__name__)

FINISHED = ("done", "failed")


async def finalise_if_complete(session: AsyncSession, run_id: int) -> bool:
    """Score the run if every item is accounted for. True if this call finalised it.

    The row lock is what makes "last one out" safe. Two workers finishing simultaneously
    would otherwise both see a complete count and both write aggregates; here one takes
    the lock and finalises, and the other blocks, then finds the run already complete.

    Completion is counted from job rows, not result rows. An item that fails permanently
    never writes a result, so counting results would leave the run stalled forever one
    short, looking like work still in progress.
    """
    run = (
        await session.execute(select(EvalRun).where(EvalRun.id == run_id).with_for_update())
    ).scalar_one_or_none()

    if run is None or run.status != "running":
        return False

    finished = (
        await session.execute(
            select(func.count())
            .select_from(EvalItemJob)
            .where(EvalItemJob.run_id == run_id, EvalItemJob.status.in_(FINISHED))
        )
    ).scalar_one()

    if finished < run.items_total:
        return False

    results = list(
        (await session.execute(select(EvalResult).where(EvalResult.run_id == run_id)))
        .scalars()
        .all()
    )

    run.metrics = _aggregate(results)
    # What was actually scored, not what was asked for: a run that lost items to provider
    # failures must not claim to have covered the whole dataset.
    run.item_count = len(results)
    run.status = "complete"

    logger.info("run %s complete: %s of %s items scored", run_id, len(results), run.items_total)
    return True
