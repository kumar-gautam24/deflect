import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deflect.db import engine, get_session
from deflect.ingest.embedder import embed_texts
from deflect.llm.base import get_client
from deflect.llm.fake import FakeClient
from deflect.main import app as fastapi_app
from deflect.models import Chunk, Document


@pytest_asyncio.fixture
async def session():
    """Each test runs in a transaction that is rolled back, so tests never share state."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            yield db
        await transaction.rollback()


@pytest_asyncio.fixture
async def corpus(session):
    document = Document(source_path="deps.md", title="Dependencies", commit_sha="sha")
    session.add(document)
    await session.flush()

    texts = [
        "Use Depends to declare a dependency in a path operation function.",
        "Deploy behind a reverse proxy such as nginx.",
    ]
    session.add_all(
        Chunk(
            document_id=document.id,
            heading_path=f"Dependencies > {i}",
            text=text,
            embedding=embedding,
            position=i,
        )
        for i, (text, embedding) in enumerate(zip(texts, embed_texts(texts), strict=True))
    )
    await session.flush()
    return document


def _answer_payload(text: str, cited: list[int], grounded: bool) -> str:
    return json.dumps({"answer": text, "cited_chunk_ids": cited, "grounded": grounded})


@pytest.fixture
def answer_payload():
    """Builds the JSON body the answer service expects back from the model."""
    return _answer_payload


def _app_with(session, client):
    """Bind the app to the test transaction and a scripted client.

    The commit inside the ask route would end the test transaction, so it is
    neutralized here; the outer fixture rollback is what cleans up.
    """
    session.commit = session.flush
    fastapi_app.dependency_overrides[get_session] = lambda: session
    fastapi_app.dependency_overrides[get_client] = lambda: client
    return fastapi_app


@pytest.fixture
def app_with_session(session):
    yield _app_with(session, FakeClient([]))
    fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def fake_client_app(session, corpus):
    chunk_id = (
        await session.execute(select(Chunk.id).where(Chunk.document_id == corpus.id))
    ).scalars().first()
    yield _app_with(session, FakeClient([_answer_payload("Use Depends.", [chunk_id], True)]))
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def escalating_app(session, corpus):
    yield _app_with(session, FakeClient([_answer_payload("Invented.", [], False)]))
    fastapi_app.dependency_overrides.clear()
