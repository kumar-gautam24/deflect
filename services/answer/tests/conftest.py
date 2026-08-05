import os

# Set before importing anything under answer.*: config is read at import and
# bearer_guard refuses to build on an empty token. Assigned rather than setdefault so an
# exported token in the developer's shell cannot diverge from what the tests send.
os.environ["SERVICE_TOKEN"] = "test-service-token"
os.environ["OPERATOR_TOKEN"] = "test-operator-token"

import json  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from deflect_common.llm.fake import FakeClient  # noqa: E402
from deflect_common.schemas import Hit  # noqa: E402
from doubles import FakeRetrieval, hit  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from answer.db import engine, get_session  # noqa: E402
from answer.main import app as fastapi_app  # noqa: E402
from answer.main import build_client, build_retrieval  # noqa: E402


@pytest_asyncio.fixture
async def session():
    """Each test runs in a transaction that is rolled back, so tests never share state."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            yield db
        await transaction.rollback()


@pytest.fixture
def hits() -> list[Hit]:
    return [
        hit(1, "Use Depends to declare a dependency in a path operation function.", 6.0),
        hit(2, "Deploy behind a reverse proxy such as nginx.", 1.0),
    ]


def _payload(text: str, cited: list[int], grounded: bool) -> str:
    return json.dumps({"answer": text, "cited_chunk_ids": cited, "grounded": grounded})


@pytest.fixture
def answer_payload():
    """Builds the JSON body the answer service expects back from the model."""
    return _payload


@pytest.fixture
def make_app(session):
    """Binds the app to the test transaction, a scripted model and a fake retrieval."""

    def build(responses: list[str], retrieval: FakeRetrieval):
        session.commit = session.flush
        fastapi_app.dependency_overrides[get_session] = lambda: session
        fastapi_app.dependency_overrides[build_client] = lambda: FakeClient(responses)
        fastapi_app.dependency_overrides[build_retrieval] = lambda: retrieval
        return fastapi_app

    yield build
    fastapi_app.dependency_overrides.clear()
