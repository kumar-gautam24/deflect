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


@pytest_asyncio.fixture
async def queue(session):
    """Binds the app to the test transaction and an in-memory queue, so no test needs
    Redis and none leaves a row behind."""
    from deflect_common.jobs import FakeJobQueue

    from retrieval.db import get_session
    from retrieval.main import app, build_queue

    fake = FakeJobQueue()
    session.commit = session.flush
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[build_queue] = lambda: fake
    yield fake
    app.dependency_overrides.clear()
