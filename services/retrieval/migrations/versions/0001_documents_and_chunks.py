import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_path", sa.String(512), nullable=False, unique=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("heading_path", sa.String(1024), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(384), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])

    # HNSW over cosine distance: the retrieval path only ever ranks by cosine.
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    # Generated tsvector keeps the lexical index in sync without application writes.
    op.execute(
        "ALTER TABLE chunks ADD COLUMN text_search tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED"
    )
    op.execute("CREATE INDEX ix_chunks_text_search ON chunks USING gin (text_search)")


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("documents")
