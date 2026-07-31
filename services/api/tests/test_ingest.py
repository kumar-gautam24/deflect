from pathlib import Path

from sqlalchemy import func, select

from deflect.ingest.embedder import embed_query, embed_texts
from deflect.ingest.pipeline import ingest_directory
from deflect.models import Chunk, Document


def test_embeddings_have_configured_dimension_and_are_normalized():
    vectors = embed_texts(["dependency injection", "path parameters"])

    assert len(vectors) == 2
    assert all(len(v) == 384 for v in vectors)


def test_query_embedding_matches_document_embedding_dimension():
    assert len(embed_query("how do I use Depends")) == len(embed_texts(["Depends"])[0])


async def test_ingest_persists_documents_and_chunks(session, tmp_path: Path):
    (tmp_path / "tutorial").mkdir()
    (tmp_path / "tutorial" / "first.md").write_text("# First Steps\n\nCreate main.py\n")
    (tmp_path / "index.md").write_text("# Index\n\nWelcome\n")

    count = await ingest_directory(session, tmp_path, commit_sha="abc123")

    # Every assertion is scoped to rows this test produced. The suite runs against a
    # database already holding the ingested corpus, so unscoped queries would see it.
    documents = (
        await session.execute(select(Document).where(Document.commit_sha == "abc123"))
    ).scalars().all()
    assert {d.source_path for d in documents} == {"index.md", "tutorial/first.md"}

    chunk_count = (
        await session.execute(
            select(func.count(Chunk.id)).where(
                Chunk.document_id.in_([d.id for d in documents])
            )
        )
    ).scalar_one()
    assert count == chunk_count


async def test_reingest_replaces_previous_chunks_for_a_document(session, tmp_path: Path):
    path = tmp_path / "a.md"
    path.write_text("# A\n\noriginal\n")
    await ingest_directory(session, tmp_path, commit_sha="sha1")

    path.write_text("# A\n\nrevised\n")
    await ingest_directory(session, tmp_path, commit_sha="sha2")

    document = (
        await session.execute(select(Document).where(Document.source_path == "a.md"))
    ).scalar_one()
    texts = (
        await session.execute(select(Chunk.text).where(Chunk.document_id == document.id))
    ).scalars().all()
    assert len(texts) == 1
    assert "revised" in texts[0]


async def test_reingest_drops_a_document_whose_content_no_longer_chunks(
    session, tmp_path: Path
):
    path = tmp_path / "b.md"
    path.write_text("# B\n\noriginal\n")
    await ingest_directory(session, tmp_path, commit_sha="sha1")

    path.write_text("# B\n")
    await ingest_directory(session, tmp_path, commit_sha="sha2")

    document = (
        await session.execute(select(Document).where(Document.source_path == "b.md"))
    ).scalar_one_or_none()
    assert document is None
