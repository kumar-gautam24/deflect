import pytest_asyncio

from deflect.ingest.embedder import embed_texts
from deflect.models import Chunk, Document
from deflect.retrieval.search import dense_search, lexical_search


@pytest_asyncio.fixture
async def corpus(session):
    document = Document(source_path="deps.md", title="Dependencies", commit_sha="sha")
    session.add(document)
    await session.flush()

    texts = [
        "Use Depends to declare a dependency in a path operation function.",
        "Return a 422 status code when request validation fails.",
        "Deploy the application behind a reverse proxy such as nginx.",
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


async def test_dense_search_matches_on_meaning_not_wording(session, corpus):
    hits = await dense_search(session, "how do I inject a shared resource", limit=3)

    assert hits[0].text.startswith("Use Depends")
    assert hits[0].source_path == "deps.md"


async def test_lexical_search_matches_exact_tokens_dense_search_can_miss(session, corpus):
    hits = await lexical_search(session, "422", limit=3)

    assert len(hits) == 1
    assert "422" in hits[0].text


async def test_lexical_search_returns_empty_for_absent_tokens(session, corpus):
    assert await lexical_search(session, "kubernetes", limit=3) == []


async def test_both_searches_respect_the_limit(session, corpus):
    assert len(await dense_search(session, "fastapi", limit=2)) == 2
