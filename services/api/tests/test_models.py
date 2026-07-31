from sqlalchemy import select

from deflect.models import Chunk, Document


async def test_chunk_stores_embedding_and_links_to_document(session):
    document = Document(source_path="docs/tutorial/index.md", title="Tutorial", commit_sha="abc123")
    session.add(document)
    await session.flush()

    session.add(
        Chunk(
            document_id=document.id,
            heading_path="Tutorial > First Steps",
            text="Create a file main.py",
            embedding=[0.1] * 384,
            position=0,
        )
    )
    await session.flush()

    stored = (await session.execute(select(Chunk))).scalar_one()
    assert stored.document_id == document.id
    assert len(stored.embedding) == 384
