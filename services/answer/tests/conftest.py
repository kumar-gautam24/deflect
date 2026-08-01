import json

import pytest
import pytest_asyncio
from deflect_common.llm.fake import FakeClient
from deflect_common.schemas import Hit
from doubles import FakeRetrieval, hit
from sqlalchemy.ext.asyncio import AsyncSession

from answer.db import engine, get_session
from answer.main import app as fastapi_app
from answer.main import build_client, build_retrieval


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
