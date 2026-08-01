from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from retrieval.config import get_settings


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_path: Mapped[str] = mapped_column(String(512), unique=True)
    title: Mapped[str] = mapped_column(String(512))
    commit_sha: Mapped[str] = mapped_column(String(64))


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    heading_path: Mapped[str] = mapped_column(String(1024))
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(get_settings().embedding_dim))
    position: Mapped[int] = mapped_column(Integer)
    # Maintained by Postgres. Mapped here so lexical search can query the column the
    # GIN index is built on: an equivalent to_tsvector() expression in the WHERE
    # clause does not match that index and falls back to a sequential scan.
    text_search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
    )

    __table_args__ = (Index("ix_chunks_document_id", "document_id"),)
