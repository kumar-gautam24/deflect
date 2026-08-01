import pytest
import pytest_asyncio
from deflect_common.schemas import Hit

from retrieval.ingest.embedder import embed_texts
from retrieval.models import Chunk, Document
from retrieval.pipeline import RetrievalConfig, retrieve
from retrieval.rerank import rerank


@pytest_asyncio.fixture
async def corpus(session):
    document = Document(source_path="a.md", title="A", commit_sha="sha")
    session.add(document)
    await session.flush()

    texts = [
        "Use Depends to declare a dependency in a path operation function.",
        "Return a 422 status code when request validation fails.",
        "Deploy behind a reverse proxy such as nginx.",
        "Background tasks run after the response is sent.",
    ]
    session.add_all(
        Chunk(
            document_id=document.id,
            heading_path=f"A > {i}",
            text=text,
            embedding=embedding,
            position=i,
        )
        for i, (text, embedding) in enumerate(zip(texts, embed_texts(texts), strict=True))
    )
    await session.flush()


def test_rerank_orders_by_query_relevance_and_truncates():
    def hit(chunk_id: int, text: str, score: float) -> Hit:
        return Hit(
            chunk_id=chunk_id,
            document_id=1,
            source_path="a.md",
            heading_path="A",
            text=text,
            score=score,
        )

    hits = [
        hit(1, "Deploy behind a reverse proxy such as nginx.", 0.5),
        hit(2, "Use Depends to declare a dependency.", 0.4),
    ]

    reranked = rerank("how does dependency injection work", hits, limit=1)

    assert len(reranked) == 1
    assert reranked[0].chunk_id == 2


async def test_pipeline_returns_final_limit_results(session, corpus):
    hits = await retrieve(session, "dependency injection", RetrievalConfig(final_limit=2))

    assert len(hits) == 2


async def test_disabling_lexical_still_returns_results(session, corpus):
    config = RetrievalConfig(use_lexical=False, use_rerank=False, final_limit=3)

    hits = await retrieve(session, "dependency injection", config)

    assert len(hits) == 3


async def test_lexical_adds_rank_mass_that_dense_alone_does_not(session, corpus):
    dense_only = RetrievalConfig(use_lexical=False, use_rerank=False, final_limit=1)
    hybrid = RetrievalConfig(use_rerank=False, final_limit=1)

    dense_hits = await retrieve(session, "422", dense_only)
    hybrid_hits = await retrieve(session, "422", hybrid)

    assert "422" in hybrid_hits[0].text
    # On a fixture this small dense search already ranks the exact-token chunk first,
    # so the observable effect of enabling lexical search is the fused score, not the
    # ordering. Whether it changes ordering is what the corpus-wide ablation measures.
    assert hybrid_hits[0].score > dense_hits[0].score


async def test_disabling_every_strategy_is_rejected(session, corpus):
    with pytest.raises(ValueError):
        await retrieve(session, "x", RetrievalConfig(use_dense=False, use_lexical=False))
