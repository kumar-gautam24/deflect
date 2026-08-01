"""Reads a documentation tree into the chunk store."""

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from retrieval.ingest.chunker import chunk_markdown
from retrieval.ingest.embedder import embed_texts
from retrieval.models import Chunk, Document


async def _upsert_document(
    session: AsyncSession, source_path: str, title: str, commit_sha: str
) -> Document:
    existing = (
        await session.execute(select(Document).where(Document.source_path == source_path))
    ).scalar_one_or_none()

    if existing is None:
        document = Document(source_path=source_path, title=title, commit_sha=commit_sha)
        session.add(document)
        await session.flush()
        return document

    existing.title = title
    existing.commit_sha = commit_sha
    await session.execute(delete(Chunk).where(Chunk.document_id == existing.id))
    return existing


async def ingest_directory(session: AsyncSession, root: Path, commit_sha: str) -> int:
    """Ingest every markdown file under root, replacing any previously stored chunks."""
    total = 0
    for path in sorted(root.rglob("*.md")):
        source = path.read_text(encoding="utf-8")
        chunks = chunk_markdown(source)
        relative = str(path.relative_to(root))

        # A file that no longer yields chunks contributes nothing to retrieval, so a
        # document previously ingested from it is dropped rather than left stale.
        if not chunks:
            await session.execute(delete(Document).where(Document.source_path == relative))
            continue

        document = await _upsert_document(session, relative, chunks[0].heading_path, commit_sha)

        embeddings = embed_texts([c.text for c in chunks])
        session.add_all(
            Chunk(
                document_id=document.id,
                heading_path=chunk.heading_path,
                text=chunk.text,
                embedding=embedding,
                position=chunk.position,
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        )
        total += len(chunks)

        # Flush and release per document. Holding every chunk of the corpus in the
        # identity map until the end grew the process past 5 GB and was OOM-killed in
        # a container; flushing here keeps the working set to one document.
        await session.flush()
        session.expunge_all()

    await session.flush()
    return total
