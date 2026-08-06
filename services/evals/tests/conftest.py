import os

os.environ["SERVICE_TOKEN"] = "test-service-token"
os.environ["OPERATOR_TOKEN"] = "test-operator-token"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from evals.db import engine  # noqa: E402


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

    from evals.db import get_session
    from evals.main import app, build_queue

    fake = FakeJobQueue()
    session.commit = session.flush
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[build_queue] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def dataset_of_three(tmp_path, monkeypatch):
    """A three-item dataset on disk, so /runs enqueues a known number of jobs."""
    path = tmp_path / "golden.yaml"
    path.write_text(
        "".join(
            f"- id: q{n}\n"
            f"  question: question {n}\n"
            f"  ideal_answer: answer {n}\n"
            f"  expected_sources: [deps.md]\n"
            f"  should_escalate: false\n"
            for n in (1, 2, 3)
        )
    )
    from evals.config import get_settings

    monkeypatch.setenv("DATASET_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()
