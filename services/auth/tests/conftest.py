import os

os.environ["SERVICE_TOKEN"] = "test-service-token"
os.environ["OPERATOR_TOKEN"] = "test-operator-token"

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from auth.db import engine  # noqa: E402


@pytest_asyncio.fixture
async def session():
    """Each test runs in a transaction that is rolled back, so tests never share state."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            yield db
        await transaction.rollback()
