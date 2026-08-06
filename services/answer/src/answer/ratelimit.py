"""The answer service's own piece of abuse control.

The shared sliding-window limiter and the client-address helper moved to
deflect_common.ratelimit once login became a second caller. This function stays behind
because it queries this service's own Trace model, and packages/common importing a
service's model is exactly the dependency direction the architecture forbids.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from answer.models import Trace


async def questions_today(session: AsyncSession, now: datetime) -> int:
    """How many questions have been answered since UTC midnight.

    Counts trace rows rather than summing cost_usd. Summing would bound the bill more
    directly, but estimate_cost returns 0.0 for any model absent from PRICING, so
    pointing generation_model at an unpriced model would silently turn the cap into no
    cap at all -- a control that fails open on an ordinary configuration change. A row
    count cannot do that.

    No new table and no migration: the answer service already writes one row per
    question, so the day's counter already exists and survives a restart.
    """
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    statement = select(func.count()).select_from(Trace).where(Trace.created_at >= midnight)
    return (await session.execute(statement)).scalar_one()
