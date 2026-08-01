"""The two retrieval strategies whose ranked lists the fusion stage merges."""

from deflect_common.schemas import Hit
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from retrieval.ingest.embedder import embed_query
from retrieval.models import Chunk, Document


def _to_hits(rows) -> list[Hit]:
    return [
        Hit(
            chunk_id=row.id,
            document_id=row.document_id,
            source_path=row.source_path,
            heading_path=row.heading_path,
            text=row.text,
            score=float(row.score),
        )
        for row in rows
    ]


async def dense_search(session: AsyncSession, query: str, limit: int) -> list[Hit]:
    distance = Chunk.embedding.cosine_distance(embed_query(query))
    statement = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.heading_path,
            Chunk.text,
            Document.source_path,
            (1 - distance).label("score"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .order_by(distance)
        .limit(limit)
    )
    return _to_hits((await session.execute(statement)).all())


async def lexical_search(session: AsyncSession, query: str, limit: int) -> list[Hit]:
    tsquery = func.plainto_tsquery("english", query)
    rank = func.ts_rank(Chunk.text_search, tsquery)
    statement = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.heading_path,
            Chunk.text,
            Document.source_path,
            rank.label("score"),
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.text_search.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    )
    return _to_hits((await session.execute(statement)).all())
