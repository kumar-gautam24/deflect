import os

# Set before importing anything under retrieval.*: config is read at module import, and
# bearer_guard refuses to build on an empty token, so main.py would fail to import.
# Assigned rather than setdefault: a developer with SERVICE_TOKEN already exported would
# otherwise build guards from their shell's value while the tests send these constants.
os.environ["SERVICE_TOKEN"] = "test-service-token"
os.environ["OPERATOR_TOKEN"] = "test-operator-token"

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from retrieval.db import engine  # noqa: E402


@pytest_asyncio.fixture
async def session():
    """Each test runs in a transaction that is rolled back, so tests never share state."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            yield db
        await transaction.rollback()
