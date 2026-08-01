import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from evals.db import engine


@pytest_asyncio.fixture
async def session():
    """Each test runs in a transaction that is rolled back, so tests never share state."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            yield db
        await transaction.rollback()
