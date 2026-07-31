# Deflect Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a support-ticket deflection system over the FastAPI documentation that answers with citations, refuses and escalates when its confidence signals say it should not guess, and measures both behaviors with an eval harness that gates CI.

**Architecture:** A Next.js app talks to a FastAPI service over HTTP and proxies its SSE stream; the web app never calls an LLM. Retrieval is hybrid (pgvector dense + Postgres full-text), merged with Reciprocal Rank Fusion and reranked by a local cross-encoder. The eval harness invokes the same answer pipeline the live app uses, so evals and production cannot drift apart.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async, asyncpg), Alembic, Pydantic v2, pytest; Postgres 16 with pgvector; fastembed (local embeddings + cross-encoder); Google Gemini via `google-genai`; Next.js 15 App Router, TypeScript, Tailwind, shadcn/ui, Vitest.

## Global Constraints

Copied verbatim from the spec. Every task's requirements implicitly include this section.

- Comments explain why, never what.
- No emoji in code, commits, or documentation.
- No exception handling that swallows errors. Fail loudly where failing is correct.
- No abstraction without a second caller.
- No placeholder functions, no deferred TODOs, no unused configuration flags.
- Docstrings on public module boundaries only.
- Tests assert behavior, never tautologies.
- Commits are incremental and describe intent. No generated co-author trailers.
- README is factual: architecture, ablation table, tradeoff curve, run instructions.
- Target size for Phase 1 is 2,500-3,500 lines. Exceeding it triggers a cut, not an exception.
- The web app never calls an LLM. All model access is server-side in the FastAPI service.
- The eval harness and the live app must execute the same answer code path.
- Embedding dimension is 384 (`BAAI/bge-small-en-v1.5`). This value appears in the migration and in config; they must agree.
- The corpus is pinned to a FastAPI docs commit SHA. Eval runs record it.
- No Kubernetes. Docker Compose locally, Vercel + Render + Neon deployed.

## File Structure

```
deflect/
├── docker-compose.yml               Postgres+pgvector and the API for local dev
├── .env.example                     Documented env vars, no secrets
├── README.md                        Architecture, ablation table, tradeoff curve, setup
├── .github/workflows/ci.yml         Lint, tests, eval smoke gate, nightly full run
├── evals/golden.yaml                Hand-labeled dataset, version controlled
├── services/api/
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── alembic.ini
│   ├── migrations/versions/         Alembic revisions
│   └── src/deflect/
│       ├── config.py                Pydantic settings, single source of tunables
│       ├── db.py                    Async engine and session factory
│       ├── models.py                SQLAlchemy ORM models
│       ├── main.py                  FastAPI app assembly
│       ├── telemetry.py             Span helpers, token and cost accounting
│       ├── llm/
│       │   ├── base.py              LLMClient protocol, request/response types
│       │   ├── gemini.py            Gemini implementation
│       │   ├── ollama.py            Local implementation
│       │   └── fake.py              Scripted test double
│       ├── ingest/
│       │   ├── chunker.py           Heading-aware markdown chunking (pure)
│       │   ├── embedder.py          fastembed wrapper
│       │   └── pipeline.py          Fetch, chunk, embed, persist
│       ├── retrieval/
│       │   ├── search.py            Dense and lexical queries
│       │   ├── fusion.py            Reciprocal Rank Fusion (pure)
│       │   ├── rerank.py            Cross-encoder wrapper
│       │   └── pipeline.py          Stage orchestration, config-driven ablation
│       ├── answer/
│       │   ├── prompts/answer_v1.md Versioned prompt, not an inline string
│       │   ├── gate.py              Confidence gate (pure)
│       │   └── service.py           Assemble, generate, cite, gate, persist trace
│       ├── evals/
│       │   ├── dataset.py           YAML loading and validation
│       │   ├── metrics.py           Deterministic retrieval metrics (pure)
│       │   ├── judge.py             LLM-as-judge scoring
│       │   └── runner.py            Run execution and persistence
│       └── routes/
│           ├── ask.py               SSE answer endpoint
│           ├── evals.py             Run listing, detail, diff
│           └── traces.py            Trace listing and detail
└── apps/web/
    ├── app/page.tsx                 Ask surface
    ├── app/evals/page.tsx           Run history and diff
    ├── app/traces/page.tsx          Request timeline
    ├── app/api/ask/route.ts         SSE proxy to FastAPI
    ├── components/                  Presentational components
    └── lib/api.ts                   Typed fetch helpers
```

Rationale for the boundaries: pure logic (`chunker`, `fusion`, `gate`, `metrics`) is separated from I/O so it can be tested without a database or a model. Each of those four files is a function or two with a table-driven test suite, and they are where the interesting decisions live.

---

## Task 1: Repository scaffold and health check

**Files:**
- Create: `docker-compose.yml`, `.env.example`, `.gitignore`
- Create: `services/api/pyproject.toml`, `services/api/Dockerfile`
- Create: `services/api/src/deflect/config.py`, `services/api/src/deflect/db.py`, `services/api/src/deflect/main.py`
- Test: `services/api/tests/test_health.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Settings` (pydantic-settings, attributes `database_url: str`, `gemini_api_key: str`, `embedding_model: str`, `embedding_dim: int`, `rerank_model: str`, `llm_provider: str`); `get_session()` async generator yielding `AsyncSession`; `app` FastAPI instance

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_health.py
import pytest
from httpx import ASGITransport, AsyncClient

from deflect.main import app


@pytest.mark.asyncio
async def test_health_reports_database_connectivity():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && uv run pytest tests/test_health.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect'`

- [ ] **Step 3: Write pyproject.toml**

```toml
# services/api/pyproject.toml
[project]
name = "deflect"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30",
    "pgvector>=0.3.6",
    "alembic>=1.14",
    "pydantic-settings>=2.6",
    "google-genai>=0.3",
    "fastembed>=0.4",
    "pyyaml>=6.0",
    "httpx>=0.28",
]

[dependency-groups]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "ruff>=0.8"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/deflect"]
```

- [ ] **Step 4: Write config.py**

```python
# services/api/src/deflect/config.py
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://deflect:deflect@localhost:5432/deflect"
    gemini_api_key: str = ""
    llm_provider: str = "gemini"
    generation_model: str = "gemini-2.0-flash"
    judge_model: str = "gemini-2.0-pro"
    ollama_base_url: str = "http://localhost:11434"

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: Write db.py**

```python
# services/api/src/deflect/db.py
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from deflect.config import get_settings

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionFactory() as session:
        yield session
```

- [ ] **Step 6: Write main.py**

```python
# services/api/src/deflect/main.py
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from deflect.db import get_session

app = FastAPI(title="Deflect")


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(text("select 1"))
    return {"status": "ok", "database": "connected"}
```

- [ ] **Step 7: Write docker-compose.yml**

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: deflect
      POSTGRES_PASSWORD: deflect
      POSTGRES_DB: deflect
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U deflect"]
      interval: 5s
      retries: 10
    volumes:
      - pgdata:/var/lib/postgresql/data

  api:
    build: ./services/api
    environment:
      DATABASE_URL: postgresql+asyncpg://deflect:deflect@db:5432/deflect
      GEMINI_API_KEY: ${GEMINI_API_KEY}
    ports: ["8000:8000"]
    depends_on:
      db:
        condition: service_healthy

volumes:
  pgdata:
```

- [ ] **Step 8: Write Dockerfile and .env.example**

```dockerfile
# services/api/Dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml ./
RUN uv sync --no-dev
COPY . .
CMD ["uv", "run", "uvicorn", "deflect.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
# .env.example
DATABASE_URL=postgresql+asyncpg://deflect:deflect@localhost:5432/deflect
GEMINI_API_KEY=
LLM_PROVIDER=gemini
```

- [ ] **Step 9: Start the database and run the test**

Run: `docker compose up -d db && cd services/api && uv sync && uv run pytest tests/test_health.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "scaffold API service with database-backed health check"
```

---

## Task 2: Document and chunk schema

**Files:**
- Create: `services/api/src/deflect/models.py`, `services/api/alembic.ini`, `services/api/migrations/env.py`, `services/api/migrations/versions/0001_documents_and_chunks.py`
- Test: `services/api/tests/test_models.py`, `services/api/tests/conftest.py`

**Interfaces:**
- Consumes: `get_settings()`, `engine` from Task 1
- Produces: `Base`; `Document(id: int, source_path: str, title: str, commit_sha: str)`; `Chunk(id: int, document_id: int, heading_path: str, text: str, embedding: Vector(384), position: int)`; pytest fixture `session` yielding a rolled-back `AsyncSession`

- [ ] **Step 1: Write conftest.py and the failing test**

```python
# services/api/tests/conftest.py
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from deflect.db import engine


@pytest_asyncio.fixture
async def session():
    """Each test runs in a transaction that is rolled back, so tests never share state."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            yield db
        await transaction.rollback()
```

```python
# services/api/tests/test_models.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.models'`

- [ ] **Step 3: Write models.py**

```python
# services/api/src/deflect/models.py
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from deflect.config import get_settings


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

    __table_args__ = (Index("ix_chunks_document_id", "document_id"),)
```

- [ ] **Step 4: Initialize Alembic and write the migration**

Run: `uv run alembic init -t async migrations`

Then set `sqlalchemy.url` from settings in `migrations/env.py` and point `target_metadata` at `Base.metadata`:

```python
# services/api/migrations/env.py -- replace the config/metadata section
from deflect.config import get_settings
from deflect.models import Base

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata
```

```python
# services/api/migrations/versions/0001_documents_and_chunks.py
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
```

- [ ] **Step 5: Apply the migration and run the test**

Run: `uv run alembic upgrade head && uv run pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "add document and chunk schema with hnsw and full-text indexes"
```

---

## Task 3: Heading-aware markdown chunker

**Files:**
- Create: `services/api/src/deflect/ingest/chunker.py`
- Test: `services/api/tests/test_chunker.py`

**Interfaces:**
- Consumes: nothing (pure module, no I/O)
- Produces: `@dataclass TextChunk(heading_path: str, text: str, position: int)`; `chunk_markdown(source: str, max_chars: int = 1200, overlap_chars: int = 150) -> list[TextChunk]`

This is the first of the four pure modules where the interesting decisions live. It has no database and no model dependency, so it is tested exhaustively and cheaply.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_chunker.py
from deflect.ingest.chunker import chunk_markdown


def test_heading_path_accumulates_nested_headings():
    source = "# Tutorial\n\nIntro text.\n\n## Dependencies\n\nDep text.\n\n### Sub\n\nSub text.\n"

    chunks = chunk_markdown(source)

    assert [c.heading_path for c in chunks] == [
        "Tutorial",
        "Tutorial > Dependencies",
        "Tutorial > Dependencies > Sub",
    ]


def test_sibling_heading_replaces_rather_than_nests():
    source = "# A\n\ntext\n\n## B\n\ntext\n\n## C\n\ntext\n"

    chunks = chunk_markdown(source)

    assert [c.heading_path for c in chunks] == ["A", "A > B", "A > C"]


def test_oversized_section_splits_with_overlap():
    source = "# Long\n\n" + ("word " * 600)

    chunks = chunk_markdown(source, max_chars=400, overlap_chars=50)

    assert len(chunks) > 1
    assert all(c.heading_path == "Long" for c in chunks)
    assert all(len(c.text) <= 400 for c in chunks)
    # The tail of one chunk reappears at the head of the next so a sentence split
    # across the boundary is still retrievable from at least one chunk.
    assert chunks[0].text[-20:] in chunks[1].text


def test_sections_without_body_are_dropped():
    source = "# A\n\n## B\n\n## C\n\nreal content\n"

    chunks = chunk_markdown(source)

    assert [c.heading_path for c in chunks] == ["A > C"]


def test_positions_are_sequential():
    source = "# A\n\ntext\n\n## B\n\ntext\n"

    chunks = chunk_markdown(source)

    assert [c.position for c in chunks] == [0, 1]


def test_fenced_code_containing_hashes_is_not_read_as_a_heading():
    source = "# A\n\n```python\n# not a heading\nx = 1\n```\n"

    chunks = chunk_markdown(source)

    assert [c.heading_path for c in chunks] == ["A"]
    assert "x = 1" in chunks[0].text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chunker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.ingest'`

- [ ] **Step 3: Write chunker.py**

```python
# services/api/src/deflect/ingest/chunker.py
"""Splits markdown into retrievable chunks that carry their heading context."""

import re
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE = re.compile(r"^\s*```")


@dataclass(frozen=True)
class TextChunk:
    heading_path: str
    text: str
    position: int


def _split_sections(source: str) -> list[tuple[str, str]]:
    sections: list[tuple[list[str], list[str]]] = []
    stack: list[str] = []
    body: list[str] = []
    in_fence = False

    def flush() -> None:
        if body:
            sections.append((list(stack), list(body)))
        body.clear()

    for line in source.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence

        match = None if in_fence else HEADING.match(line)
        if match is None:
            body.append(line)
            continue

        flush()
        depth = len(match.group(1))
        del stack[depth - 1 :]
        stack.append(match.group(2).strip())

    flush()
    return [(" > ".join(path), "\n".join(lines).strip()) for path, lines in sections]


def _split_oversized(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    start = 0
    while start < len(text):
        parts.append(text[start : start + max_chars])
        start += max_chars - overlap_chars
    return parts


def chunk_markdown(
    source: str, max_chars: int = 1200, overlap_chars: int = 150
) -> list[TextChunk]:
    """Chunk on heading boundaries, splitting only sections that exceed max_chars.

    Heading boundaries are used rather than a fixed window because a documentation
    section is the unit an answer cites, and the heading path is what makes a
    citation readable.
    """
    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be smaller than max_chars")

    chunks: list[TextChunk] = []
    for heading_path, body in _split_sections(source):
        if not body:
            continue
        for part in _split_oversized(body, max_chars, overlap_chars):
            chunks.append(TextChunk(heading_path, part, len(chunks)))
    return chunks
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chunker.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "add heading-aware markdown chunker"
```

---

## Task 4: Embedder and ingest pipeline

**Files:**
- Create: `services/api/src/deflect/ingest/embedder.py`, `services/api/src/deflect/ingest/pipeline.py`
- Test: `services/api/tests/test_ingest.py`

**Interfaces:**
- Consumes: `TextChunk`, `chunk_markdown` (Task 3); `Document`, `Chunk` (Task 2)
- Produces: `embed_texts(texts: list[str]) -> list[list[float]]`; `embed_query(text: str) -> list[float]`; `async ingest_directory(session, root: Path, commit_sha: str) -> int` returning the chunk count

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_ingest.py
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

    documents = (await session.execute(select(Document))).scalars().all()
    assert {d.source_path for d in documents} == {"index.md", "tutorial/first.md"}
    assert all(d.commit_sha == "abc123" for d in documents)
    assert count == (await session.execute(select(func.count(Chunk.id)))).scalar_one()


async def test_reingest_replaces_previous_chunks_for_a_document(session, tmp_path: Path):
    path = tmp_path / "a.md"
    path.write_text("# A\n\noriginal\n")
    await ingest_directory(session, tmp_path, commit_sha="sha1")

    path.write_text("# A\n\nrevised\n")
    await ingest_directory(session, tmp_path, commit_sha="sha2")

    texts = (await session.execute(select(Chunk.text))).scalars().all()
    assert len(texts) == 1
    assert "revised" in texts[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.ingest.embedder'`

- [ ] **Step 3: Write embedder.py**

```python
# services/api/src/deflect/ingest/embedder.py
"""Local embedding model. Kept local so re-ingesting during ablation costs nothing."""

from functools import lru_cache

from fastembed import TextEmbedding

from deflect.config import get_settings


@lru_cache
def _model() -> TextEmbedding:
    return TextEmbedding(model_name=get_settings().embedding_model)


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [vector.tolist() for vector in _model().embed(texts)]


def embed_query(text: str) -> list[float]:
    # bge models are trained with an asymmetric query prefix; omitting it measurably
    # degrades retrieval, so the query path must not reuse embed_texts directly.
    return embed_texts([f"Represent this sentence for searching relevant passages: {text}"])[0]
```

- [ ] **Step 4: Write pipeline.py**

```python
# services/api/src/deflect/ingest/pipeline.py
"""Reads a documentation tree into the chunk store."""

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from deflect.ingest.chunker import chunk_markdown
from deflect.ingest.embedder import embed_texts
from deflect.models import Chunk, Document


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
        if not chunks:
            continue

        relative = str(path.relative_to(root))
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

    await session.flush()
    return total
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ingest.py -v`
Expected: PASS (4 tests). First run downloads the embedding model.

- [ ] **Step 6: Ingest the real corpus**

```bash
git clone --depth 1 https://github.com/fastapi/fastapi /tmp/fastapi-src
cd /tmp/fastapi-src && git rev-parse HEAD  # record this SHA in the README
```

Add a `scripts/ingest.py` entry point that opens a session, calls `ingest_directory` against `/tmp/fastapi-src/docs/en/docs`, and commits.

```python
# services/api/scripts/ingest.py
import asyncio
import sys
from pathlib import Path

from deflect.db import SessionFactory
from deflect.ingest.pipeline import ingest_directory


async def main(root: str, commit_sha: str) -> None:
    async with SessionFactory() as session:
        count = await ingest_directory(session, Path(root), commit_sha)
        await session.commit()
    print(f"ingested {count} chunks")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
```

Run: `uv run python scripts/ingest.py /tmp/fastapi-src/docs/en/docs <SHA>`
Expected: prints a chunk count in the low thousands

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "add local embedder and documentation ingest pipeline"
```

---

## Task 5: Dense and lexical search

**Files:**
- Create: `services/api/src/deflect/retrieval/search.py`
- Test: `services/api/tests/test_search.py`

**Interfaces:**
- Consumes: `Chunk`, `Document` (Task 2); `embed_query` (Task 4)
- Produces: `@dataclass Hit(chunk_id: int, document_id: int, source_path: str, heading_path: str, text: str, score: float)`; `async dense_search(session, query: str, limit: int) -> list[Hit]`; `async lexical_search(session, query: str, limit: int) -> list[Hit]`

Both return the same `Hit` type so the fusion stage does not care which produced them.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_search.py
import pytest

from deflect.ingest.embedder import embed_texts
from deflect.models import Chunk, Document
from deflect.retrieval.search import dense_search, lexical_search


@pytest.fixture
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_search.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.retrieval'`

- [ ] **Step 3: Write search.py**

```python
# services/api/src/deflect/retrieval/search.py
"""The two retrieval strategies whose ranked lists the fusion stage merges."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deflect.ingest.embedder import embed_query
from deflect.models import Chunk, Document


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    document_id: int
    source_path: str
    heading_path: str
    text: str
    score: float


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
    rank = func.ts_rank(func.to_tsvector("english", Chunk.text), tsquery)
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
        .where(func.to_tsvector("english", Chunk.text).op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    )
    return _to_hits((await session.execute(statement)).all())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_search.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "add dense and lexical chunk search"
```

---

## Task 6: Reciprocal Rank Fusion

**Files:**
- Create: `services/api/src/deflect/retrieval/fusion.py`
- Test: `services/api/tests/test_fusion.py`

**Interfaces:**
- Consumes: `Hit` (Task 5)
- Produces: `reciprocal_rank_fusion(rankings: list[list[Hit]], k: int = 60) -> list[Hit]`

Pure function, no I/O. The returned `Hit.score` is the fused score, not either input score.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_fusion.py
from deflect.retrieval.fusion import reciprocal_rank_fusion
from deflect.retrieval.search import Hit


def hit(chunk_id: int, score: float = 0.0) -> Hit:
    return Hit(chunk_id, 1, "a.md", "A", f"text {chunk_id}", score)


def test_chunk_ranked_highly_by_both_strategies_wins():
    dense = [hit(1), hit(2), hit(3)]
    lexical = [hit(3), hit(1), hit(4)]

    fused = reciprocal_rank_fusion([dense, lexical])

    assert fused[0].chunk_id == 1


def test_scores_are_fused_ranks_not_input_scores():
    fused = reciprocal_rank_fusion([[hit(1, score=0.99)]], k=60)

    assert fused[0].score == 1 / 61


def test_result_is_deduplicated_by_chunk_id():
    fused = reciprocal_rank_fusion([[hit(1), hit(2)], [hit(1), hit(2)]])

    assert [h.chunk_id for h in fused] == [1, 2]


def test_output_is_sorted_by_descending_score():
    fused = reciprocal_rank_fusion([[hit(1), hit(2), hit(3)]])

    assert [h.score for h in fused] == sorted([h.score for h in fused], reverse=True)


def test_empty_ranking_lists_are_ignored():
    fused = reciprocal_rank_fusion([[], [hit(1)]])

    assert [h.chunk_id for h in fused] == [1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fusion.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.retrieval.fusion'`

- [ ] **Step 3: Write fusion.py**

```python
# services/api/src/deflect/retrieval/fusion.py
"""Merges ranked lists from independent retrieval strategies."""

from dataclasses import replace

from deflect.retrieval.search import Hit


def reciprocal_rank_fusion(rankings: list[list[Hit]], k: int = 60) -> list[Hit]:
    """Combine ranked lists by rank position rather than by score.

    Dense cosine similarity and ts_rank are not on a comparable scale, so blending
    their scores would require per-corpus tuning. Rank position needs none.
    """
    scores: dict[int, float] = {}
    hits: dict[int, Hit] = {}

    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item.chunk_id] = scores.get(item.chunk_id, 0.0) + 1 / (k + rank + 1)
            hits.setdefault(item.chunk_id, item)

    ordered = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return [replace(hits[chunk_id], score=score) for chunk_id, score in ordered]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fusion.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "add reciprocal rank fusion"
```

---

## Task 7: Reranker and retrieval pipeline

**Files:**
- Create: `services/api/src/deflect/retrieval/rerank.py`, `services/api/src/deflect/retrieval/pipeline.py`
- Test: `services/api/tests/test_retrieval_pipeline.py`

**Interfaces:**
- Consumes: `Hit`, `dense_search`, `lexical_search` (Task 5); `reciprocal_rank_fusion` (Task 6)
- Produces: `rerank(query: str, hits: list[Hit], limit: int) -> list[Hit]`; `@dataclass RetrievalConfig(use_dense: bool = True, use_lexical: bool = True, use_rerank: bool = True, candidate_limit: int = 20, final_limit: int = 5)`; `async retrieve(session, query: str, config: RetrievalConfig) -> list[Hit]`

`RetrievalConfig` is what makes the ablation table reproducible: the same code path produces vector-only, hybrid, and hybrid+rerank results.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_retrieval_pipeline.py
import pytest

from deflect.ingest.embedder import embed_texts
from deflect.models import Chunk, Document
from deflect.retrieval.pipeline import RetrievalConfig, retrieve
from deflect.retrieval.rerank import rerank
from deflect.retrieval.search import Hit


@pytest.fixture
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
    hits = [
        Hit(1, 1, "a.md", "A", "Deploy behind a reverse proxy such as nginx.", 0.5),
        Hit(2, 1, "a.md", "A", "Use Depends to declare a dependency.", 0.4),
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


async def test_hybrid_finds_exact_token_that_dense_alone_ranks_poorly(session, corpus):
    dense_only = RetrievalConfig(use_lexical=False, use_rerank=False, final_limit=1)
    hybrid = RetrievalConfig(use_rerank=False, final_limit=1)

    dense_hits = await retrieve(session, "422", dense_only)
    hybrid_hits = await retrieve(session, "422", hybrid)

    assert "422" in hybrid_hits[0].text
    assert dense_hits[0].chunk_id != hybrid_hits[0].chunk_id or "422" in dense_hits[0].text


async def test_disabling_every_strategy_is_rejected(session, corpus):
    with pytest.raises(ValueError):
        await retrieve(session, "x", RetrievalConfig(use_dense=False, use_lexical=False))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_retrieval_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.retrieval.rerank'`

- [ ] **Step 3: Write rerank.py**

```python
# services/api/src/deflect/retrieval/rerank.py
"""Cross-encoder reranking of fused candidates."""

from dataclasses import replace
from functools import lru_cache

from fastembed.rerank.cross_encoder import TextCrossEncoder

from deflect.config import get_settings
from deflect.retrieval.search import Hit


@lru_cache
def _model() -> TextCrossEncoder:
    return TextCrossEncoder(model_name=get_settings().rerank_model)


def rerank(query: str, hits: list[Hit], limit: int) -> list[Hit]:
    """Rescore candidates by joint query-document attention, then keep the top `limit`.

    Bi-encoder retrieval scores query and chunk independently; a cross-encoder sees
    both at once and is materially better at ordering, but is too slow to run over
    the whole corpus. Hence: cheap retrieval wide, expensive reranking narrow.
    """
    if not hits:
        return []

    scores = list(_model().rerank(query, [h.text for h in hits]))
    rescored = [replace(hit, score=float(score)) for hit, score in zip(hits, scores, strict=True)]
    return sorted(rescored, key=lambda h: h.score, reverse=True)[:limit]
```

- [ ] **Step 4: Write pipeline.py**

```python
# services/api/src/deflect/retrieval/pipeline.py
"""Retrieval stage orchestration. Stages are toggleable so the ablation is reproducible."""

import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from deflect.retrieval.fusion import reciprocal_rank_fusion
from deflect.retrieval.rerank import rerank
from deflect.retrieval.search import Hit, dense_search, lexical_search


@dataclass(frozen=True)
class RetrievalConfig:
    use_dense: bool = True
    use_lexical: bool = True
    use_rerank: bool = True
    candidate_limit: int = 20
    final_limit: int = 5


async def retrieve(session: AsyncSession, query: str, config: RetrievalConfig) -> list[Hit]:
    if not (config.use_dense or config.use_lexical):
        raise ValueError("at least one of use_dense or use_lexical must be enabled")

    searches = []
    if config.use_dense:
        searches.append(dense_search(session, query, config.candidate_limit))
    if config.use_lexical:
        searches.append(lexical_search(session, query, config.candidate_limit))

    rankings: list[list[Hit]] = await asyncio.gather(*searches)
    fused = reciprocal_rank_fusion(rankings)

    if config.use_rerank:
        return rerank(query, fused[: config.candidate_limit], config.final_limit)
    return fused[: config.final_limit]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_retrieval_pipeline.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "add cross-encoder reranking and configurable retrieval pipeline"
```

---

## Task 8: LLM client interface with Gemini, Ollama, and a test double

**Files:**
- Create: `services/api/src/deflect/llm/base.py`, `services/api/src/deflect/llm/gemini.py`, `services/api/src/deflect/llm/ollama.py`, `services/api/src/deflect/llm/fake.py`
- Test: `services/api/tests/test_llm.py`

**Interfaces:**
- Consumes: `get_settings()` (Task 1)
- Produces: `@dataclass Completion(text: str, input_tokens: int, output_tokens: int, model: str)`; `class LLMClient(Protocol)` with `async complete(prompt: str, schema: dict | None = None) -> Completion`; `GeminiClient`, `OllamaClient`, `FakeClient(responses: list[str])`; `get_client(provider: str | None = None) -> LLMClient`

The abstraction exists because it has two real implementations plus a test double, and because the eval dashboard compares providers. Nothing else in this codebase gets an interface for a single implementation.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_llm.py
import json

import pytest

from deflect.llm.base import Completion
from deflect.llm.fake import FakeClient


async def test_fake_client_returns_scripted_responses_in_order():
    client = FakeClient(["first", "second"])

    assert (await client.complete("a")).text == "first"
    assert (await client.complete("b")).text == "second"


async def test_fake_client_records_prompts_for_assertions():
    client = FakeClient(["ok"])

    await client.complete("what is Depends")

    assert client.prompts == ["what is Depends"]


async def test_fake_client_raises_when_the_script_runs_out():
    client = FakeClient(["only"])
    await client.complete("a")

    with pytest.raises(AssertionError):
        await client.complete("b")


async def test_completion_reports_token_counts():
    completion = await FakeClient([json.dumps({"answer": "x"})]).complete("q")

    assert isinstance(completion, Completion)
    assert completion.output_tokens > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.llm'`

- [ ] **Step 3: Write base.py**

```python
# services/api/src/deflect/llm/base.py
"""Provider-agnostic completion interface."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


class LLMClient(Protocol):
    async def complete(self, prompt: str, schema: dict | None = None) -> Completion:
        """Return a completion, constrained to `schema` when the provider supports it."""
        ...
```

- [ ] **Step 4: Write fake.py**

```python
# services/api/src/deflect/llm/fake.py
"""Scripted client so tests exercise the real pipeline without spending tokens."""

from deflect.llm.base import Completion


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def complete(self, prompt: str, schema: dict | None = None) -> Completion:
        self.prompts.append(prompt)
        assert self._responses, "FakeClient exhausted: more calls than scripted responses"
        text = self._responses.pop(0)
        return Completion(
            text=text,
            input_tokens=len(prompt.split()),
            output_tokens=max(len(text.split()), 1),
            model="fake",
        )
```

- [ ] **Step 5: Write gemini.py and ollama.py**

```python
# services/api/src/deflect/llm/gemini.py
from google import genai
from google.genai import types

from deflect.config import get_settings
from deflect.llm.base import Completion


class GeminiClient:
    def __init__(self, model: str) -> None:
        self._client = genai.Client(api_key=get_settings().gemini_api_key)
        self._model = model

    async def complete(self, prompt: str, schema: dict | None = None) -> Completion:
        config = types.GenerateContentConfig(temperature=0.0)
        if schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = schema

        response = await self._client.aio.models.generate_content(
            model=self._model, contents=prompt, config=config
        )
        usage = response.usage_metadata
        return Completion(
            text=response.text,
            input_tokens=usage.prompt_token_count,
            output_tokens=usage.candidates_token_count,
            model=self._model,
        )
```

```python
# services/api/src/deflect/llm/ollama.py
import httpx

from deflect.config import get_settings
from deflect.llm.base import Completion


class OllamaClient:
    def __init__(self, model: str) -> None:
        self._base_url = get_settings().ollama_base_url
        self._model = model

    async def complete(self, prompt: str, schema: dict | None = None) -> Completion:
        payload: dict = {"model": self._model, "prompt": prompt, "stream": False}
        if schema is not None:
            payload["format"] = schema

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self._base_url}/api/generate", json=payload)
            response.raise_for_status()
            body = response.json()

        return Completion(
            text=body["response"],
            input_tokens=body["prompt_eval_count"],
            output_tokens=body["eval_count"],
            model=self._model,
        )
```

Add the factory to `base.py`:

```python
# append to services/api/src/deflect/llm/base.py
def get_client(provider: str | None = None, model: str | None = None) -> LLMClient:
    from deflect.config import get_settings
    from deflect.llm.gemini import GeminiClient
    from deflect.llm.ollama import OllamaClient

    settings = get_settings()
    provider = provider or settings.llm_provider
    model = model or settings.generation_model

    if provider == "gemini":
        return GeminiClient(model)
    if provider == "ollama":
        return OllamaClient(model)
    raise ValueError(f"unknown provider: {provider}")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "add provider-agnostic llm client with gemini and ollama backends"
```

---

## Task 9: Confidence gate

**Files:**
- Create: `services/api/src/deflect/answer/gate.py`
- Test: `services/api/tests/test_gate.py`

**Interfaces:**
- Consumes: `Hit` (Task 5)
- Produces: `@dataclass GateThresholds(min_top_score: float = 0.0, min_margin: float = 0.0, require_grounded: bool = True)`; `@dataclass GateDecision(escalate: bool, reason: str | None, top_score: float, margin: float)`; `evaluate_gate(hits: list[Hit], grounded: bool, thresholds: GateThresholds) -> GateDecision`

Pure function. Threshold *values* are chosen in Task 20 from the swept curve; this task only implements the decision rule.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_gate.py
from deflect.answer.gate import GateThresholds, evaluate_gate
from deflect.retrieval.search import Hit


def hits(*scores: float) -> list[Hit]:
    return [Hit(i, 1, "a.md", "A", "text", score) for i, score in enumerate(scores)]


THRESHOLDS = GateThresholds(min_top_score=0.5, min_margin=0.1)


def test_confident_grounded_answer_is_not_escalated():
    decision = evaluate_gate(hits(0.9, 0.4), grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is False
    assert decision.reason is None


def test_weak_top_score_escalates():
    decision = evaluate_gate(hits(0.2, 0.1), grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is True
    assert decision.reason == "low_retrieval_score"


def test_ambiguous_results_escalate_even_when_the_top_score_is_high():
    decision = evaluate_gate(hits(0.9, 0.88), grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is True
    assert decision.reason == "ambiguous_retrieval"


def test_ungrounded_answer_escalates_despite_strong_retrieval():
    decision = evaluate_gate(hits(0.95, 0.3), grounded=False, thresholds=THRESHOLDS)

    assert decision.escalate is True
    assert decision.reason == "ungrounded_answer"


def test_no_retrieved_chunks_escalates():
    decision = evaluate_gate([], grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is True
    assert decision.reason == "no_results"


def test_single_hit_uses_its_own_score_as_the_margin():
    decision = evaluate_gate(hits(0.9), grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is False
    assert decision.margin == 0.9


def test_decision_reports_the_signals_it_used():
    decision = evaluate_gate(hits(0.9, 0.4), grounded=True, thresholds=THRESHOLDS)

    assert decision.top_score == 0.9
    assert decision.margin == pytest.approx(0.5)
```

Add `import pytest` at the top of the test file for `pytest.approx`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.answer'`

- [ ] **Step 3: Write gate.py**

```python
# services/api/src/deflect/answer/gate.py
"""Decides whether an answer is trustworthy enough to show, or must be escalated."""

from dataclasses import dataclass

from deflect.retrieval.search import Hit


@dataclass(frozen=True)
class GateThresholds:
    min_top_score: float = 0.0
    min_margin: float = 0.0
    require_grounded: bool = True


@dataclass(frozen=True)
class GateDecision:
    escalate: bool
    reason: str | None
    top_score: float
    margin: float


def evaluate_gate(
    hits: list[Hit], grounded: bool, thresholds: GateThresholds
) -> GateDecision:
    """Escalate unless retrieval was strong, unambiguous, and the answer stayed grounded.

    Ordering matters: retrieval failures are reported ahead of grounding failures
    because they are the actionable ones. An ungrounded answer over good context is a
    prompt problem; an ungrounded answer over bad context is a retrieval problem.
    """
    if not hits:
        return GateDecision(True, "no_results", 0.0, 0.0)

    top_score = hits[0].score
    # A lone hit has nothing to be ambiguous against, so its own score is the margin.
    margin = top_score - hits[1].score if len(hits) > 1 else top_score

    if top_score < thresholds.min_top_score:
        return GateDecision(True, "low_retrieval_score", top_score, margin)
    if margin < thresholds.min_margin:
        return GateDecision(True, "ambiguous_retrieval", top_score, margin)
    if thresholds.require_grounded and not grounded:
        return GateDecision(True, "ungrounded_answer", top_score, margin)

    return GateDecision(False, None, top_score, margin)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gate.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "add confidence gate"
```

---

## Task 10: Answer service

**Files:**
- Create: `services/api/src/deflect/answer/prompts/answer_v1.md`, `services/api/src/deflect/answer/service.py`
- Test: `services/api/tests/test_answer_service.py`

**Interfaces:**
- Consumes: `retrieve`, `RetrievalConfig` (Task 7); `LLMClient`, `FakeClient` (Task 8); `evaluate_gate`, `GateThresholds`, `GateDecision` (Task 9)
- Produces: `@dataclass Citation(source_path: str, heading_path: str, chunk_id: int)`; `@dataclass AnswerResult(answer: str, citations: list[Citation], decision: GateDecision, hits: list[Hit], input_tokens: int, output_tokens: int, model: str, prompt_version: str)`; `async answer_question(session, question: str, client: LLMClient, retrieval_config: RetrievalConfig, thresholds: GateThresholds) -> AnswerResult`

This is the single code path both the API route and the eval runner call. Nothing else may reimplement it.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_answer_service.py
import json

import pytest

from deflect.answer.gate import GateThresholds
from deflect.answer.service import answer_question
from deflect.ingest.embedder import embed_texts
from deflect.llm.fake import FakeClient
from deflect.models import Chunk, Document
from deflect.retrieval.pipeline import RetrievalConfig


@pytest.fixture
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


def response(answer: str, cited: list[int], grounded: bool = True) -> str:
    return json.dumps({"answer": answer, "cited_chunk_ids": cited, "grounded": grounded})


PERMISSIVE = GateThresholds(min_top_score=-1.0, min_margin=-1.0)


async def test_answer_includes_citations_for_the_chunks_the_model_used(session, corpus):
    from sqlalchemy import select

    chunk_id = (await session.execute(select(Chunk.id))).scalars().first()
    client = FakeClient([response("Use Depends.", [chunk_id])])

    result = await answer_question(
        session, "how do I inject a dependency", client, RetrievalConfig(), PERMISSIVE
    )

    assert result.answer == "Use Depends."
    assert [c.chunk_id for c in result.citations] == [chunk_id]
    assert result.citations[0].source_path == "deps.md"


async def test_retrieved_chunks_are_present_in_the_prompt(session, corpus):
    client = FakeClient([response("x", [])])

    await answer_question(session, "dependency injection", client, RetrievalConfig(), PERMISSIVE)

    assert "Use Depends" in client.prompts[0]
    assert "dependency injection" in client.prompts[0]


async def test_ungrounded_model_response_escalates(session, corpus):
    client = FakeClient([response("Invented answer.", [], grounded=False)])

    result = await answer_question(
        session, "dependency injection", client, RetrievalConfig(), PERMISSIVE
    )

    assert result.decision.escalate is True
    assert result.decision.reason == "ungrounded_answer"


async def test_weak_retrieval_escalates_and_returns_no_citations(session, corpus):
    client = FakeClient([response("Some answer.", [])])
    strict = GateThresholds(min_top_score=99.0, min_margin=0.0)

    result = await answer_question(session, "unrelated topic", client, RetrievalConfig(), strict)

    assert result.decision.escalate is True
    assert result.citations == []


async def test_token_usage_and_prompt_version_are_reported(session, corpus):
    client = FakeClient([response("x", [])])

    result = await answer_question(
        session, "dependency injection", client, RetrievalConfig(), PERMISSIVE
    )

    assert result.output_tokens > 0
    assert result.prompt_version == "answer_v1"


async def test_citations_referencing_unretrieved_chunks_are_dropped(session, corpus):
    client = FakeClient([response("x", [999999])])

    result = await answer_question(
        session, "dependency injection", client, RetrievalConfig(), PERMISSIVE
    )

    assert result.citations == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_answer_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.answer.service'`

- [ ] **Step 3: Write the prompt file**

```markdown
<!-- services/api/src/deflect/answer/prompts/answer_v1.md -->
You are a support assistant for the FastAPI web framework. Answer using only the
numbered context passages below.

Rules:
- Use only information stated in the passages. Do not draw on outside knowledge.
- Cite the id of every passage you used in `cited_chunk_ids`.
- If the passages do not contain the answer, say so plainly and set `grounded` to false.
- Set `grounded` to true only if every claim in your answer is supported by a passage.

Context passages:
{context}

Question: {question}
```

- [ ] **Step 4: Write service.py**

```python
# services/api/src/deflect/answer/service.py
"""The single answer code path. The API route and the eval runner both call this."""

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from deflect.answer.gate import GateDecision, GateThresholds, evaluate_gate
from deflect.llm.base import LLMClient
from deflect.retrieval.pipeline import RetrievalConfig, retrieve
from deflect.retrieval.search import Hit

PROMPT_VERSION = "answer_v1"
PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md").read_text()

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "cited_chunk_ids": {"type": "array", "items": {"type": "integer"}},
        "grounded": {"type": "boolean"},
    },
    "required": ["answer", "cited_chunk_ids", "grounded"],
}


@dataclass(frozen=True)
class Citation:
    source_path: str
    heading_path: str
    chunk_id: int


@dataclass(frozen=True)
class AnswerResult:
    answer: str
    citations: list[Citation]
    decision: GateDecision
    hits: list[Hit]
    input_tokens: int
    output_tokens: int
    model: str
    prompt_version: str


def _format_context(hits: list[Hit]) -> str:
    return "\n\n".join(
        f"[id: {hit.chunk_id}] {hit.heading_path}\n{hit.text}" for hit in hits
    )


async def answer_question(
    session: AsyncSession,
    question: str,
    client: LLMClient,
    retrieval_config: RetrievalConfig,
    thresholds: GateThresholds,
) -> AnswerResult:
    hits = await retrieve(session, question, retrieval_config)
    prompt = PROMPT_TEMPLATE.format(context=_format_context(hits), question=question)

    completion = await client.complete(prompt, schema=RESPONSE_SCHEMA)
    payload = json.loads(completion.text)

    decision = evaluate_gate(hits, grounded=payload["grounded"], thresholds=thresholds)

    # Citations are resolved against retrieved chunks, so a hallucinated id cannot
    # produce a citation that links nowhere.
    by_id = {hit.chunk_id: hit for hit in hits}
    citations = (
        []
        if decision.escalate
        else [
            Citation(by_id[cid].source_path, by_id[cid].heading_path, cid)
            for cid in payload["cited_chunk_ids"]
            if cid in by_id
        ]
    )

    return AnswerResult(
        answer=payload["answer"],
        citations=citations,
        decision=decision,
        hits=hits,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        model=completion.model,
        prompt_version=PROMPT_VERSION,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_answer_service.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "add answer service with citation resolution and gating"
```

---

## Task 11: Traces, escalations, and the ask endpoint

**Files:**
- Create: `services/api/migrations/versions/0002_traces_and_escalations.py`, `services/api/src/deflect/telemetry.py`, `services/api/src/deflect/routes/ask.py`
- Modify: `services/api/src/deflect/models.py`, `services/api/src/deflect/main.py`
- Test: `services/api/tests/test_ask_route.py`

**Interfaces:**
- Consumes: `answer_question`, `AnswerResult` (Task 10); `get_session` (Task 1)
- Produces: `Trace(id, question, answer, escalated, reason, top_score, margin, retrieved: JSON, input_tokens, output_tokens, cost_usd, model, prompt_version, latency_ms, created_at)`; `Escalation(id, trace_id, question, reason, created_at)`; `estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float`; `async record_trace(session, question, result, latency_ms) -> Trace`; router at `POST /ask` streaming SSE

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_ask_route.py
import json

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from deflect.models import Escalation, Trace
from deflect.telemetry import estimate_cost


def test_cost_scales_with_token_counts():
    cheap = estimate_cost("gemini-2.0-flash", 1000, 1000)
    expensive = estimate_cost("gemini-2.0-flash", 10000, 10000)

    assert expensive > cheap > 0


def test_unknown_model_has_no_priced_cost():
    assert estimate_cost("fake", 100, 100) == 0.0


async def test_ask_streams_answer_then_a_final_metadata_event(session, corpus, fake_client_app):
    transport = ASGITransport(app=fake_client_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "dependency injection"})

    events = [line for line in response.text.splitlines() if line.startswith("data:")]
    final = json.loads(events[-1].removeprefix("data:").strip())

    assert final["type"] == "done"
    assert final["escalated"] is False
    assert final["citations"]


async def test_answering_writes_a_trace_with_cost_and_latency(session, corpus, fake_client_app):
    transport = ASGITransport(app=fake_client_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/ask", json={"question": "dependency injection"})

    trace = (await session.execute(select(Trace))).scalars().one()
    assert trace.latency_ms > 0
    assert trace.retrieved
    assert trace.escalated is False


async def test_escalated_answer_writes_an_escalation_row(session, corpus, escalating_app):
    transport = ASGITransport(app=escalating_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/ask", json={"question": "unrelated"})

    escalation = (await session.execute(select(Escalation))).scalars().one()
    assert escalation.reason == "ungrounded_answer"
```

Add fixtures to `conftest.py` that build an app with `get_session` and the LLM client dependency overridden to the test session and a `FakeClient`. The `corpus` fixture is the one from Task 10, moved into `conftest.py` so both test modules share it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ask_route.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.telemetry'`

- [ ] **Step 3: Add the models and migration**

```python
# append to services/api/src/deflect/models.py
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, func


class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    escalated: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    top_score: Mapped[float] = mapped_column(Float)
    margin: Mapped[float] = mapped_column(Float)
    retrieved: Mapped[list] = mapped_column(JSON)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[int] = mapped_column(ForeignKey("traces.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

Generate the migration: `uv run alembic revision --autogenerate -m "traces and escalations"`, then review the produced file and rename it `0002_traces_and_escalations.py`.

- [ ] **Step 4: Write telemetry.py**

```python
# services/api/src/deflect/telemetry.py
"""Token cost accounting and trace persistence."""

from sqlalchemy.ext.asyncio import AsyncSession

from deflect.answer.service import AnswerResult
from deflect.models import Escalation, Trace

# USD per million tokens, input and output. Unpriced models cost nothing to record.
PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-pro": (1.25, 5.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in PRICING:
        return 0.0
    input_price, output_price = PRICING[model]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


async def record_trace(
    session: AsyncSession, question: str, result: AnswerResult, latency_ms: int
) -> Trace:
    trace = Trace(
        question=question,
        answer=result.answer,
        escalated=result.decision.escalate,
        reason=result.decision.reason,
        top_score=result.decision.top_score,
        margin=result.decision.margin,
        retrieved=[
            {"chunk_id": h.chunk_id, "source_path": h.source_path, "score": h.score}
            for h in result.hits
        ],
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=estimate_cost(result.model, result.input_tokens, result.output_tokens),
        model=result.model,
        prompt_version=result.prompt_version,
        latency_ms=latency_ms,
    )
    session.add(trace)
    await session.flush()

    if result.decision.escalate:
        session.add(
            Escalation(
                trace_id=trace.id, question=question, reason=result.decision.reason
            )
        )
        await session.flush()

    return trace
```

- [ ] **Step 5: Write routes/ask.py**

```python
# services/api/src/deflect/routes/ask.py
import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from deflect.answer.gate import GateThresholds
from deflect.answer.service import answer_question
from deflect.db import get_session
from deflect.llm.base import LLMClient, get_client
from deflect.retrieval.pipeline import RetrievalConfig
from deflect.telemetry import record_trace

router = APIRouter()

# Chosen in Task 20 from the swept deflection-rate curve.
THRESHOLDS = GateThresholds(min_top_score=0.5, min_margin=0.05)


class AskRequest(BaseModel):
    question: str


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/ask")
async def ask(
    request: AskRequest,
    session: AsyncSession = Depends(get_session),
    client: LLMClient = Depends(get_client),
) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        started = time.monotonic()
        result = await answer_question(
            session, request.question, client, RetrievalConfig(), THRESHOLDS
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        trace = await record_trace(session, request.question, result, latency_ms)
        await session.commit()

        # The provider returns structured JSON in one call, so streaming is done by
        # chunking the finished answer. This keeps one code path for app and evals.
        for word in result.answer.split(" "):
            yield _event({"type": "token", "text": word + " "})
            await asyncio.sleep(0)

        yield _event(
            {
                "type": "done",
                "trace_id": trace.id,
                "escalated": result.decision.escalate,
                "reason": result.decision.reason,
                "citations": [
                    {
                        "source_path": c.source_path,
                        "heading_path": c.heading_path,
                        "chunk_id": c.chunk_id,
                    }
                    for c in result.citations
                ],
                "latency_ms": latency_ms,
            }
        )

    return StreamingResponse(stream(), media_type="text/event-stream")
```

Register it in `main.py`:

```python
# services/api/src/deflect/main.py -- add
from deflect.routes import ask

app.include_router(ask.router)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run alembic upgrade head && uv run pytest tests/test_ask_route.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "add trace recording, cost accounting, and streaming ask endpoint"
```

---

## Task 12: Golden dataset and loader

**Files:**
- Create: `evals/golden.yaml`, `services/api/src/deflect/evals/dataset.py`
- Test: `services/api/tests/test_dataset.py`

**Interfaces:**
- Consumes: nothing
- Produces: `@dataclass GoldenItem(id: str, question: str, ideal_answer: str, expected_sources: list[str], should_escalate: bool)`; `load_dataset(path: Path) -> list[GoldenItem]`

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_dataset.py
import pytest

from deflect.evals.dataset import load_dataset


def test_loads_items_with_all_fields(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        "- id: q1\n"
        "  question: How do I declare a dependency?\n"
        "  ideal_answer: Use Depends.\n"
        "  expected_sources: [tutorial/dependencies/index.md]\n"
        "  should_escalate: false\n"
    )

    items = load_dataset(path)

    assert len(items) == 1
    assert items[0].id == "q1"
    assert items[0].expected_sources == ["tutorial/dependencies/index.md"]
    assert items[0].should_escalate is False


def test_unanswerable_items_need_no_expected_sources(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        "- id: q2\n"
        "  question: What is the FastAPI pricing?\n"
        "  ideal_answer: Not covered by the documentation.\n"
        "  should_escalate: true\n"
    )

    assert load_dataset(path)[0].expected_sources == []


def test_answerable_item_without_expected_sources_is_rejected(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        "- id: q3\n"
        "  question: q\n"
        "  ideal_answer: a\n"
        "  should_escalate: false\n"
    )

    with pytest.raises(ValueError, match="q3"):
        load_dataset(path)


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        "- {id: q1, question: a, ideal_answer: a, expected_sources: [x.md], should_escalate: false}\n"
        "- {id: q1, question: b, ideal_answer: b, expected_sources: [y.md], should_escalate: false}\n"
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(path)


def test_the_real_dataset_loads_and_covers_the_refusal_path():
    from pathlib import Path

    items = load_dataset(Path(__file__).parents[3] / "evals" / "golden.yaml")

    assert len(items) >= 80
    assert sum(1 for i in items if i.should_escalate) >= 15
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dataset.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.evals'`

- [ ] **Step 3: Write dataset.py**

```python
# services/api/src/deflect/evals/dataset.py
"""Loads and validates the hand-labeled golden dataset."""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class GoldenItem:
    id: str
    question: str
    ideal_answer: str
    expected_sources: list[str]
    should_escalate: bool


def load_dataset(path: Path) -> list[GoldenItem]:
    raw = yaml.safe_load(path.read_text())

    items: list[GoldenItem] = []
    seen: set[str] = set()
    for entry in raw:
        item = GoldenItem(
            id=entry["id"],
            question=entry["question"],
            ideal_answer=entry["ideal_answer"],
            expected_sources=entry.get("expected_sources", []),
            should_escalate=entry["should_escalate"],
        )
        if item.id in seen:
            raise ValueError(f"duplicate item id: {item.id}")
        # Retrieval metrics are computed against expected_sources; an answerable item
        # without them would silently score zero and look like a retrieval regression.
        if not item.should_escalate and not item.expected_sources:
            raise ValueError(f"answerable item {item.id} has no expected_sources")
        seen.add(item.id)
        items.append(item)

    return items
```

- [ ] **Step 4: Author the golden dataset**

Write `evals/golden.yaml` with at least 80 items against the ingested FastAPI docs: roughly 65 answerable (`should_escalate: false`, with real `expected_sources` paths verified against the `documents` table) and at least 15 unanswerable (`should_escalate: true`) covering things the docs do not address, such as pricing, unrelated frameworks, and questions about the future roadmap.

Spread the answerable items across difficulty: exact-term lookups (`422`, `Depends`, `BackgroundTasks`), paraphrased conceptual questions, and multi-section questions whose answer spans two documents.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_dataset.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "add golden dataset and validating loader"
```

---

## Task 13: Deterministic retrieval metrics

**Files:**
- Create: `services/api/src/deflect/evals/metrics.py`
- Test: `services/api/tests/test_metrics.py`

**Interfaces:**
- Consumes: nothing (pure module operating on source-path strings)
- Produces: `hit_at_k(retrieved: list[str], expected: list[str], k: int) -> float`; `mrr(retrieved: list[str], expected: list[str]) -> float`; `precision_at_k(retrieved: list[str], expected: list[str], k: int) -> float`

Kept free of LLM calls so a regression can be attributed to retrieval or to generation without ambiguity.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_metrics.py
import pytest

from deflect.evals.metrics import hit_at_k, mrr, precision_at_k


def test_hit_at_k_is_one_when_an_expected_source_is_within_k():
    assert hit_at_k(["a.md", "b.md", "c.md"], ["c.md"], k=3) == 1.0


def test_hit_at_k_is_zero_when_the_expected_source_falls_outside_k():
    assert hit_at_k(["a.md", "b.md", "c.md"], ["c.md"], k=2) == 0.0


def test_mrr_uses_the_rank_of_the_first_expected_source():
    assert mrr(["a.md", "b.md"], ["b.md"]) == 0.5
    assert mrr(["b.md", "a.md"], ["b.md"]) == 1.0


def test_mrr_is_zero_when_nothing_expected_was_retrieved():
    assert mrr(["a.md"], ["z.md"]) == 0.0


def test_precision_counts_distinct_expected_sources_within_k():
    assert precision_at_k(["a.md", "b.md", "c.md", "d.md"], ["a.md", "c.md"], k=4) == 0.5


def test_duplicate_retrieved_sources_do_not_inflate_precision():
    assert precision_at_k(["a.md", "a.md"], ["a.md"], k=2) == 0.5


def test_metrics_reject_a_non_positive_k():
    with pytest.raises(ValueError):
        hit_at_k(["a.md"], ["a.md"], k=0)


def test_empty_expected_sources_score_zero():
    assert hit_at_k(["a.md"], [], k=1) == 0.0
    assert mrr(["a.md"], []) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.evals.metrics'`

- [ ] **Step 3: Write metrics.py**

```python
# services/api/src/deflect/evals/metrics.py
"""Retrieval metrics. Deterministic and LLM-free, so regressions are attributable."""


def _validate(k: int) -> None:
    if k < 1:
        raise ValueError("k must be at least 1")


def hit_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    _validate(k)
    return 1.0 if set(retrieved[:k]) & set(expected) else 0.0


def mrr(retrieved: list[str], expected: list[str]) -> float:
    wanted = set(expected)
    for rank, source in enumerate(retrieved, start=1):
        if source in wanted:
            return 1 / rank
    return 0.0


def precision_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    _validate(k)
    window = retrieved[:k]
    if not window:
        return 0.0
    # Counted over distinct sources: the same document retrieved twice is one
    # correct document, not two.
    return len(set(window) & set(expected)) / len(window)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "add deterministic retrieval metrics"
```

---

## Task 14: LLM-as-judge

**Files:**
- Create: `services/api/src/deflect/evals/prompts/judge_v1.md`, `services/api/src/deflect/evals/judge.py`
- Test: `services/api/tests/test_judge.py`

**Interfaces:**
- Consumes: `LLMClient`, `FakeClient` (Task 8); `Hit` (Task 5); `GoldenItem` (Task 12)
- Produces: `@dataclass JudgeScores(faithfulness: float, answer_relevance: float, context_relevance: float, rationale: str)`; `async judge_answer(client, item: GoldenItem, answer: str, hits: list[Hit]) -> JudgeScores`

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_judge.py
import json

import pytest

from deflect.evals.dataset import GoldenItem
from deflect.evals.judge import judge_answer
from deflect.llm.fake import FakeClient
from deflect.retrieval.search import Hit

ITEM = GoldenItem("q1", "How do I declare a dependency?", "Use Depends.", ["deps.md"], False)
HITS = [Hit(1, 1, "deps.md", "Dependencies", "Use Depends to declare a dependency.", 0.9)]


def scores(faithfulness=1.0, answer_relevance=1.0, context_relevance=1.0) -> str:
    return json.dumps(
        {
            "faithfulness": faithfulness,
            "answer_relevance": answer_relevance,
            "context_relevance": context_relevance,
            "rationale": "supported by the context",
        }
    )


async def test_judge_returns_the_three_ragas_scores():
    result = await judge_answer(FakeClient([scores()]), ITEM, "Use Depends.", HITS)

    assert result.faithfulness == 1.0
    assert result.answer_relevance == 1.0
    assert result.context_relevance == 1.0
    assert result.rationale


async def test_judge_prompt_contains_question_ideal_answer_context_and_answer():
    client = FakeClient([scores()])

    await judge_answer(client, ITEM, "Use Depends.", HITS)

    prompt = client.prompts[0]
    assert ITEM.question in prompt
    assert ITEM.ideal_answer in prompt
    assert "Use Depends to declare a dependency." in prompt


async def test_scores_outside_the_unit_interval_are_rejected():
    with pytest.raises(ValueError):
        await judge_answer(FakeClient([scores(faithfulness=1.7)]), ITEM, "x", HITS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_judge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.evals.judge'`

- [ ] **Step 3: Write the judge prompt**

```markdown
<!-- services/api/src/deflect/evals/prompts/judge_v1.md -->
Score a support answer against its retrieved context and a reference answer.

Score each dimension from 0.0 to 1.0:
- faithfulness: every claim in the answer is supported by the context passages
- answer_relevance: the answer addresses the question that was asked
- context_relevance: the retrieved passages were useful for answering the question

Judge only what is present. Do not reward an answer for outside knowledge that
happens to be correct but is absent from the context.

Question: {question}

Reference answer: {ideal_answer}

Context passages:
{context}

Answer under evaluation: {answer}
```

- [ ] **Step 4: Write judge.py**

```python
# services/api/src/deflect/evals/judge.py
"""LLM-as-judge scoring of generated answers."""

import json
from dataclasses import dataclass
from pathlib import Path

from deflect.evals.dataset import GoldenItem
from deflect.llm.base import LLMClient
from deflect.retrieval.search import Hit

JUDGE_VERSION = "judge_v1"
PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / f"{JUDGE_VERSION}.md").read_text()

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "faithfulness": {"type": "number"},
        "answer_relevance": {"type": "number"},
        "context_relevance": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["faithfulness", "answer_relevance", "context_relevance", "rationale"],
}


@dataclass(frozen=True)
class JudgeScores:
    faithfulness: float
    answer_relevance: float
    context_relevance: float
    rationale: str


async def judge_answer(
    client: LLMClient, item: GoldenItem, answer: str, hits: list[Hit]
) -> JudgeScores:
    context = "\n\n".join(f"{h.heading_path}\n{h.text}" for h in hits)
    prompt = PROMPT_TEMPLATE.format(
        question=item.question,
        ideal_answer=item.ideal_answer,
        context=context,
        answer=answer,
    )

    completion = await client.complete(prompt, schema=SCORE_SCHEMA)
    payload = json.loads(completion.text)

    scores = JudgeScores(
        faithfulness=payload["faithfulness"],
        answer_relevance=payload["answer_relevance"],
        context_relevance=payload["context_relevance"],
        rationale=payload["rationale"],
    )
    # An out-of-range score means the judge misread its instructions; averaging it
    # into a run would silently corrupt the whole run's metrics.
    for value in (scores.faithfulness, scores.answer_relevance, scores.context_relevance):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"judge returned an out-of-range score: {value}")

    return scores
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_judge.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "add llm-as-judge scoring"
```

---

## Task 15: Eval run storage and runner

**Files:**
- Create: `services/api/migrations/versions/0003_eval_runs.py`, `services/api/src/deflect/evals/runner.py`, `services/api/scripts/run_evals.py`
- Modify: `services/api/src/deflect/models.py`
- Test: `services/api/tests/test_eval_runner.py`

**Interfaces:**
- Consumes: `answer_question` (Task 10); `load_dataset`, `GoldenItem` (Task 12); metrics (Task 13); `judge_answer` (Task 14)
- Produces: `EvalRun(id, git_sha, prompt_version, judge_version, model, retrieval_config: JSON, thresholds: JSON, item_count, metrics: JSON, created_at)`; `EvalResult(id, run_id, item_id, question, answer, escalated, expected_escalate, retrieved_sources: JSON, hit_at_5, mrr, faithfulness, answer_relevance, context_relevance, rationale)`; `async run_evals(session, items, answer_client, judge_client, retrieval_config, thresholds, git_sha) -> EvalRun`

The runner calls `answer_question`. It must not reimplement retrieval or prompting.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_eval_runner.py
import json

from sqlalchemy import select

from deflect.answer.gate import GateThresholds
from deflect.evals.dataset import GoldenItem
from deflect.evals.runner import run_evals
from deflect.llm.fake import FakeClient
from deflect.models import EvalResult, EvalRun
from deflect.retrieval.pipeline import RetrievalConfig

PERMISSIVE = GateThresholds(min_top_score=-1.0, min_margin=-1.0)


def answer_response(text: str, grounded: bool = True) -> str:
    return json.dumps({"answer": text, "cited_chunk_ids": [], "grounded": grounded})


def judge_response() -> str:
    return json.dumps(
        {
            "faithfulness": 1.0,
            "answer_relevance": 1.0,
            "context_relevance": 1.0,
            "rationale": "ok",
        }
    )


async def test_run_persists_a_result_per_item_and_aggregate_metrics(session, corpus):
    items = [
        GoldenItem("q1", "dependency injection", "Use Depends.", ["deps.md"], False),
        GoldenItem("q2", "dependency injection", "Use Depends.", ["deps.md"], False),
    ]

    run = await run_evals(
        session,
        items,
        FakeClient([answer_response("Use Depends.")] * 2),
        FakeClient([judge_response()] * 2),
        RetrievalConfig(),
        PERMISSIVE,
        git_sha="abc123",
    )

    results = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(results) == 2
    assert run.item_count == 2
    assert run.metrics["faithfulness"] == 1.0
    assert run.git_sha == "abc123"


async def test_run_records_configuration_needed_to_reproduce_it(session, corpus):
    items = [GoldenItem("q1", "dependency injection", "Use Depends.", ["deps.md"], False)]

    run = await run_evals(
        session,
        items,
        FakeClient([answer_response("x")]),
        FakeClient([judge_response()]),
        RetrievalConfig(use_rerank=False),
        PERMISSIVE,
        git_sha="sha",
    )

    assert run.retrieval_config["use_rerank"] is False
    assert run.prompt_version == "answer_v1"


async def test_unanswerable_item_that_escalates_is_not_judged(session, corpus):
    items = [GoldenItem("q1", "unrelated", "Not covered.", [], True)]
    judge = FakeClient([])

    run = await run_evals(
        session,
        items,
        FakeClient([answer_response("Not covered.", grounded=False)]),
        judge,
        RetrievalConfig(),
        PERMISSIVE,
        git_sha="sha",
    )

    assert judge.prompts == []
    assert run.metrics["escalation_recall"] == 1.0


async def test_escalation_precision_penalizes_refusing_an_answerable_question(session, corpus):
    items = [GoldenItem("q1", "dependency injection", "Use Depends.", ["deps.md"], False)]

    run = await run_evals(
        session,
        items,
        FakeClient([answer_response("x", grounded=False)]),
        FakeClient([]),
        RetrievalConfig(),
        PERMISSIVE,
        git_sha="sha",
    )

    assert run.metrics["escalation_precision"] == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_eval_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.evals.runner'`

- [ ] **Step 3: Add the models and migration**

```python
# append to services/api/src/deflect/models.py
class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    git_sha: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    judge_version: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    retrieval_config: Mapped[dict] = mapped_column(JSON)
    thresholds: Mapped[dict] = mapped_column(JSON)
    item_count: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    item_id: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    escalated: Mapped[bool] = mapped_column(Boolean)
    expected_escalate: Mapped[bool] = mapped_column(Boolean)
    retrieved_sources: Mapped[list] = mapped_column(JSON)
    hit_at_5: Mapped[float] = mapped_column(Float)
    mrr: Mapped[float] = mapped_column(Float)
    faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True)
    answer_relevance: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_relevance: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Generate with `uv run alembic revision --autogenerate -m "eval runs"`, review, rename to `0003_eval_runs.py`.

- [ ] **Step 4: Write runner.py**

```python
# services/api/src/deflect/evals/runner.py
"""Executes the golden dataset against the live answer path and persists the run."""

from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession

from deflect.answer.gate import GateThresholds
from deflect.answer.service import answer_question
from deflect.evals.dataset import GoldenItem
from deflect.evals.judge import JUDGE_VERSION, judge_answer
from deflect.evals.metrics import hit_at_k, mrr
from deflect.llm.base import LLMClient
from deflect.models import EvalResult, EvalRun
from deflect.retrieval.pipeline import RetrievalConfig


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _aggregate(results: list[EvalResult]) -> dict:
    judged = [r for r in results if r.faithfulness is not None]
    escalated = [r for r in results if r.escalated]
    should_escalate = [r for r in results if r.expected_escalate]

    return {
        "hit_at_5": _mean([r.hit_at_5 for r in results if not r.expected_escalate]),
        "mrr": _mean([r.mrr for r in results if not r.expected_escalate]),
        "faithfulness": _mean([r.faithfulness for r in judged]),
        "answer_relevance": _mean([r.answer_relevance for r in judged]),
        "context_relevance": _mean([r.context_relevance for r in judged]),
        "escalation_precision": (
            _mean([1.0 if r.expected_escalate else 0.0 for r in escalated])
        ),
        "escalation_recall": (
            _mean([1.0 if r.escalated else 0.0 for r in should_escalate])
        ),
        "deflection_rate": _mean([0.0 if r.escalated else 1.0 for r in results]),
    }


async def run_evals(
    session: AsyncSession,
    items: list[GoldenItem],
    answer_client: LLMClient,
    judge_client: LLMClient,
    retrieval_config: RetrievalConfig,
    thresholds: GateThresholds,
    git_sha: str,
) -> EvalRun:
    run = EvalRun(
        git_sha=git_sha,
        prompt_version="",
        judge_version=JUDGE_VERSION,
        model="",
        retrieval_config=asdict(retrieval_config),
        thresholds=asdict(thresholds),
        item_count=len(items),
        metrics={},
    )
    session.add(run)
    await session.flush()

    results: list[EvalResult] = []
    for item in items:
        outcome = await answer_question(
            session, item.question, answer_client, retrieval_config, thresholds
        )
        run.prompt_version = outcome.prompt_version
        run.model = outcome.model

        sources = [hit.source_path for hit in outcome.hits]
        scores = None
        # Judging a refusal wastes tokens: there is no answer to score, and the
        # escalation metrics already capture whether refusing was correct.
        if not outcome.decision.escalate:
            scores = await judge_answer(judge_client, item, outcome.answer, outcome.hits)

        result = EvalResult(
            run_id=run.id,
            item_id=item.id,
            question=item.question,
            answer=outcome.answer,
            escalated=outcome.decision.escalate,
            expected_escalate=item.should_escalate,
            retrieved_sources=sources,
            hit_at_5=hit_at_k(sources, item.expected_sources, k=5),
            mrr=mrr(sources, item.expected_sources),
            faithfulness=scores.faithfulness if scores else None,
            answer_relevance=scores.answer_relevance if scores else None,
            context_relevance=scores.context_relevance if scores else None,
            rationale=scores.rationale if scores else None,
        )
        results.append(result)

    session.add_all(results)
    run.metrics = _aggregate(results)
    await session.flush()
    return run
```

- [ ] **Step 5: Write the runner script**

```python
# services/api/scripts/run_evals.py
import argparse
import asyncio
import subprocess
from pathlib import Path

from deflect.answer.gate import GateThresholds
from deflect.config import get_settings
from deflect.db import SessionFactory
from deflect.evals.dataset import load_dataset
from deflect.llm.base import get_client
from deflect.retrieval.pipeline import RetrievalConfig
from deflect.evals.runner import run_evals
from deflect.routes.ask import THRESHOLDS


async def main(dataset: Path, limit: int | None, fail_under: float | None) -> None:
    items = load_dataset(dataset)[:limit]
    settings = get_settings()
    git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    async with SessionFactory() as session:
        run = await run_evals(
            session,
            items,
            get_client(model=settings.generation_model),
            get_client(model=settings.judge_model),
            RetrievalConfig(),
            THRESHOLDS,
            git_sha,
        )
        await session.commit()
        metrics = run.metrics

    for name, value in sorted(metrics.items()):
        print(f"{name}: {value:.3f}")

    if fail_under is not None and metrics["faithfulness"] < fail_under:
        raise SystemExit(
            f"faithfulness {metrics['faithfulness']:.3f} below threshold {fail_under}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("../../evals/golden.yaml"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--fail-under", type=float, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.dataset, args.limit, args.fail_under))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run alembic upgrade head && uv run pytest tests/test_eval_runner.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "add eval runner with run and per-item persistence"
```

---

## Task 16: Eval and trace read APIs

**Files:**
- Create: `services/api/src/deflect/routes/evals.py`, `services/api/src/deflect/routes/traces.py`
- Modify: `services/api/src/deflect/main.py`
- Test: `services/api/tests/test_read_routes.py`

**Interfaces:**
- Consumes: `EvalRun`, `EvalResult` (Task 15); `Trace` (Task 11)
- Produces: `GET /eval-runs`, `GET /eval-runs/{run_id}`, `GET /eval-runs/diff?base={id}&head={id}`, `GET /traces`, `GET /traces/{trace_id}`

The diff endpoint is the one carrying real logic: it pairs results by `item_id` and reports which items regressed.

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_read_routes.py
from httpx import ASGITransport, AsyncClient

from deflect.models import EvalResult, EvalRun


def make_run(sha: str, faithfulness: float) -> EvalRun:
    return EvalRun(
        git_sha=sha,
        prompt_version="answer_v1",
        judge_version="judge_v1",
        model="fake",
        retrieval_config={},
        thresholds={},
        item_count=1,
        metrics={"faithfulness": faithfulness},
    )


def make_result(run_id: int, item_id: str, faithfulness: float) -> EvalResult:
    return EvalResult(
        run_id=run_id,
        item_id=item_id,
        question="q",
        answer="a",
        escalated=False,
        expected_escalate=False,
        retrieved_sources=["a.md"],
        hit_at_5=1.0,
        mrr=1.0,
        faithfulness=faithfulness,
        answer_relevance=1.0,
        context_relevance=1.0,
        rationale="ok",
    )


async def test_run_list_is_newest_first(session, app_with_session):
    session.add_all([make_run("old", 0.9), make_run("new", 0.8)])
    await session.flush()

    transport = ASGITransport(app=app_with_session)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/eval-runs")).json()

    assert [r["git_sha"] for r in body] == ["new", "old"]


async def test_run_detail_includes_per_item_results(session, app_with_session):
    run = make_run("sha", 1.0)
    session.add(run)
    await session.flush()
    session.add(make_result(run.id, "q1", 1.0))
    await session.flush()

    transport = ASGITransport(app=app_with_session)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get(f"/eval-runs/{run.id}")).json()

    assert body["metrics"]["faithfulness"] == 1.0
    assert [r["item_id"] for r in body["results"]] == ["q1"]


async def test_diff_flags_items_whose_score_dropped(session, app_with_session):
    base, head = make_run("base", 1.0), make_run("head", 0.5)
    session.add_all([base, head])
    await session.flush()
    session.add_all(
        [
            make_result(base.id, "q1", 1.0),
            make_result(base.id, "q2", 1.0),
            make_result(head.id, "q1", 0.2),
            make_result(head.id, "q2", 1.0),
        ]
    )
    await session.flush()

    transport = ASGITransport(app=app_with_session)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get(f"/eval-runs/diff?base={base.id}&head={head.id}")).json()

    assert [item["item_id"] for item in body["regressed"]] == ["q1"]
    assert body["regressed"][0]["base_faithfulness"] == 1.0
    assert body["regressed"][0]["head_faithfulness"] == 0.2


async def test_missing_run_returns_404(session, app_with_session):
    transport = ASGITransport(app=app_with_session)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/eval-runs/999999")).status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_read_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'deflect.routes.evals'`

- [ ] **Step 3: Write routes/evals.py**

```python
# services/api/src/deflect/routes/evals.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deflect.db import get_session
from deflect.models import EvalResult, EvalRun

router = APIRouter(prefix="/eval-runs")


def _run_summary(run: EvalRun) -> dict:
    return {
        "id": run.id,
        "git_sha": run.git_sha,
        "prompt_version": run.prompt_version,
        "model": run.model,
        "item_count": run.item_count,
        "metrics": run.metrics,
        "retrieval_config": run.retrieval_config,
        "created_at": run.created_at.isoformat(),
    }


def _result_row(result: EvalResult) -> dict:
    return {
        "item_id": result.item_id,
        "question": result.question,
        "answer": result.answer,
        "escalated": result.escalated,
        "expected_escalate": result.expected_escalate,
        "retrieved_sources": result.retrieved_sources,
        "hit_at_5": result.hit_at_5,
        "mrr": result.mrr,
        "faithfulness": result.faithfulness,
        "rationale": result.rationale,
    }


async def _load_run(session: AsyncSession, run_id: int) -> EvalRun:
    run = await session.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"eval run {run_id} not found")
    return run


async def _results_for(session: AsyncSession, run_id: int) -> list[EvalResult]:
    statement = select(EvalResult).where(EvalResult.run_id == run_id).order_by(EvalResult.item_id)
    return list((await session.execute(statement)).scalars().all())


@router.get("")
async def list_runs(session: AsyncSession = Depends(get_session)) -> list[dict]:
    statement = select(EvalRun).order_by(EvalRun.id.desc()).limit(50)
    return [_run_summary(run) for run in (await session.execute(statement)).scalars()]


@router.get("/diff")
async def diff_runs(
    base: int, head: int, session: AsyncSession = Depends(get_session)
) -> dict:
    base_run, head_run = await _load_run(session, base), await _load_run(session, head)
    base_by_item = {r.item_id: r for r in await _results_for(session, base)}

    regressed = []
    for result in await _results_for(session, head):
        previous = base_by_item.get(result.item_id)
        if previous is None or previous.faithfulness is None or result.faithfulness is None:
            continue
        if result.faithfulness < previous.faithfulness:
            regressed.append(
                {
                    "item_id": result.item_id,
                    "question": result.question,
                    "base_faithfulness": previous.faithfulness,
                    "head_faithfulness": result.faithfulness,
                }
            )

    return {
        "base": _run_summary(base_run),
        "head": _run_summary(head_run),
        "regressed": regressed,
    }


@router.get("/{run_id}")
async def get_run(run_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    run = await _load_run(session, run_id)
    return _run_summary(run) | {"results": [_result_row(r) for r in await _results_for(session, run_id)]}
```

Route order matters: `/diff` is declared before `/{run_id}` so it is not captured by the path parameter.

- [ ] **Step 4: Write routes/traces.py**

```python
# services/api/src/deflect/routes/traces.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deflect.db import get_session
from deflect.models import Trace

router = APIRouter(prefix="/traces")


def _serialize(trace: Trace) -> dict:
    return {
        "id": trace.id,
        "question": trace.question,
        "answer": trace.answer,
        "escalated": trace.escalated,
        "reason": trace.reason,
        "top_score": trace.top_score,
        "margin": trace.margin,
        "retrieved": trace.retrieved,
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "cost_usd": trace.cost_usd,
        "model": trace.model,
        "prompt_version": trace.prompt_version,
        "latency_ms": trace.latency_ms,
        "created_at": trace.created_at.isoformat(),
    }


@router.get("")
async def list_traces(session: AsyncSession = Depends(get_session)) -> list[dict]:
    statement = select(Trace).order_by(Trace.id.desc()).limit(100)
    return [_serialize(trace) for trace in (await session.execute(statement)).scalars()]


@router.get("/{trace_id}")
async def get_trace(trace_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    trace = await session.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"trace {trace_id} not found")
    return _serialize(trace)
```

Register both routers in `main.py` alongside `ask`, and add CORS for the web origin:

```python
# services/api/src/deflect/main.py -- add
from fastapi.middleware.cors import CORSMiddleware

from deflect.routes import ask, evals, traces

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.include_router(ask.router)
app.include_router(evals.router)
app.include_router(traces.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_read_routes.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "add eval run and trace read endpoints with run diffing"
```

---

## Task 17: Next.js app with the Ask surface

**Files:**
- Create: `apps/web/` via `create-next-app`, `apps/web/lib/api.ts`, `apps/web/app/api/ask/route.ts`, `apps/web/app/page.tsx`, `apps/web/components/answer-panel.tsx`
- Test: `apps/web/components/answer-panel.test.tsx`

**Interfaces:**
- Consumes: `POST /ask` SSE stream (Task 11)
- Produces: `type Citation = { source_path: string; heading_path: string; chunk_id: number }`; `type AskDone = { type: "done"; trace_id: number; escalated: boolean; reason: string | null; citations: Citation[]; latency_ms: number }`; `<AnswerPanel answer={string} done={AskDone | null} />`

- [ ] **Step 1: Scaffold the app**

Run: `npx create-next-app@latest apps/web --typescript --tailwind --app --eslint --no-src-dir --import-alias "@/*"`
Then: `cd apps/web && npx shadcn@latest init -d && npx shadcn@latest add button input card badge table && npm i -D vitest @vitejs/plugin-react @testing-library/react @testing-library/jest-dom jsdom`

Add to `apps/web/package.json` scripts: `"test": "vitest run"`, and create `vitest.config.ts` with the react plugin and `environment: "jsdom"`.

- [ ] **Step 2: Write the failing test**

```tsx
// apps/web/components/answer-panel.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnswerPanel } from "./answer-panel";

const done = {
  type: "done" as const,
  trace_id: 1,
  escalated: false,
  reason: null,
  citations: [{ source_path: "deps.md", heading_path: "Dependencies", chunk_id: 7 }],
  latency_ms: 120,
};

describe("AnswerPanel", () => {
  it("renders the answer and its citations", () => {
    render(<AnswerPanel answer="Use Depends." done={done} />);

    expect(screen.getByText("Use Depends.")).toBeDefined();
    expect(screen.getByText("Dependencies")).toBeDefined();
  });

  it("shows an escalation notice with its reason instead of citations", () => {
    render(
      <AnswerPanel
        answer="Not covered."
        done={{ ...done, escalated: true, reason: "low_retrieval_score", citations: [] }}
      />,
    );

    expect(screen.getByText(/escalated to a human/i)).toBeDefined();
    expect(screen.getByText(/low_retrieval_score/)).toBeDefined();
  });

  it("renders nothing but the streaming answer before the done event arrives", () => {
    render(<AnswerPanel answer="Use " done={null} />);

    expect(screen.getByText("Use")).toBeDefined();
    expect(screen.queryByText(/escalated/i)).toBeNull();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/web && npm test`
Expected: FAIL, cannot resolve `./answer-panel`

- [ ] **Step 4: Write the SSE proxy route**

```ts
// apps/web/app/api/ask/route.ts
const API_URL = process.env.API_URL ?? "http://localhost:8000";

// The browser never holds an API key. The stream is proxied so the model provider
// is only ever reachable from the FastAPI service.
export async function POST(request: Request) {
  const upstream = await fetch(`${API_URL}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });

  return new Response(upstream.body, {
    status: upstream.status,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
  });
}
```

- [ ] **Step 5: Write lib/api.ts and answer-panel.tsx**

```ts
// apps/web/lib/api.ts
export type Citation = { source_path: string; heading_path: string; chunk_id: number };

export type AskDone = {
  type: "done";
  trace_id: number;
  escalated: boolean;
  reason: string | null;
  citations: Citation[];
  latency_ms: number;
};

export type AskEvent = { type: "token"; text: string } | AskDone;

export async function* askStream(question: string): AsyncGenerator<AskEvent> {
  const response = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!response.body) throw new Error("ask endpoint returned no body");

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += value;

    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      if (frame.startsWith("data:")) yield JSON.parse(frame.slice(5));
    }
  }
}

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function getJSON<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}
```

```tsx
// apps/web/components/answer-panel.tsx
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import type { AskDone } from "@/lib/api";

export function AnswerPanel({ answer, done }: { answer: string; done: AskDone | null }) {
  return (
    <Card className="p-6 space-y-4">
      <p className="whitespace-pre-wrap leading-relaxed">{answer}</p>

      {done?.escalated && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
          <p className="font-medium">Escalated to a human</p>
          <p className="text-muted-foreground">Reason: {done.reason}</p>
        </div>
      )}

      {done && !done.escalated && done.citations.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Sources</p>
          <div className="flex flex-wrap gap-2">
            {done.citations.map((citation) => (
              <Badge key={citation.chunk_id} variant="secondary">
                {citation.heading_path}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {done && (
        <p className="text-xs text-muted-foreground">
          {done.latency_ms} ms &middot; trace {done.trace_id}
        </p>
      )}
    </Card>
  );
}
```

- [ ] **Step 6: Write the Ask page**

```tsx
// apps/web/app/page.tsx
"use client";

import { useState } from "react";

import { AnswerPanel } from "@/components/answer-panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type AskDone, askStream } from "@/lib/api";

export default function AskPage() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [done, setDone] = useState<AskDone | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setAnswer("");
    setDone(null);
    setPending(true);

    for await (const event of askStream(question)) {
      if (event.type === "token") setAnswer((current) => current + event.text);
      else setDone(event);
    }
    setPending(false);
  }

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-8">
      <h1 className="text-2xl font-semibold">Deflect</h1>
      <form onSubmit={submit} className="flex gap-2">
        <Input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ask a FastAPI question"
        />
        <Button type="submit" disabled={pending || !question}>
          Ask
        </Button>
      </form>
      {(answer || done) && <AnswerPanel answer={answer} done={done} />}
    </main>
  );
}
```

- [ ] **Step 7: Run the test and the full stack**

Run: `npm test`
Expected: PASS (3 tests)

Run the API and web app together, ask a real question, confirm a streamed answer with citations.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "add next.js ask surface with sse proxy and citation rendering"
```

---

## Task 18: Evals dashboard

**Files:**
- Create: `apps/web/app/evals/page.tsx`, `apps/web/components/run-table.tsx`, `apps/web/components/run-diff.tsx`
- Test: `apps/web/components/run-diff.test.tsx`

**Interfaces:**
- Consumes: `getJSON` (Task 17); `GET /eval-runs`, `GET /eval-runs/diff` (Task 16)
- Produces: `type EvalRunSummary`; `<RunTable runs={EvalRunSummary[]} />`; `<RunDiff diff={DiffResponse} />`

The diff view is the logic worth testing on the frontend; the surrounding pages are covered by the end-to-end demo path.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/web/components/run-diff.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunDiff } from "./run-diff";

const run = {
  id: 1,
  git_sha: "abc1234",
  prompt_version: "answer_v1",
  model: "gemini-2.0-flash",
  item_count: 80,
  metrics: { faithfulness: 0.9, hit_at_5: 0.8, deflection_rate: 0.7 },
  retrieval_config: {},
  created_at: "2026-07-31T00:00:00+00:00",
};

describe("RunDiff", () => {
  it("lists regressed items with both scores", () => {
    render(
      <RunDiff
        diff={{
          base: run,
          head: { ...run, id: 2, metrics: { ...run.metrics, faithfulness: 0.6 } },
          regressed: [
            { item_id: "q7", question: "How do background tasks work?", base_faithfulness: 1, head_faithfulness: 0.3 },
          ],
        }}
      />,
    );

    expect(screen.getByText("q7")).toBeDefined();
    expect(screen.getByText(/1\.00/)).toBeDefined();
    expect(screen.getByText(/0\.30/)).toBeDefined();
  });

  it("reports a clean diff when nothing regressed", () => {
    render(<RunDiff diff={{ base: run, head: { ...run, id: 2 }, regressed: [] }} />);

    expect(screen.getByText(/no regressions/i)).toBeDefined();
  });

  it("marks a metric that fell between the two runs", () => {
    render(
      <RunDiff
        diff={{
          base: run,
          head: { ...run, id: 2, metrics: { ...run.metrics, faithfulness: 0.6 } },
          regressed: [],
        }}
      />,
    );

    expect(screen.getByTestId("metric-faithfulness").className).toContain("text-red");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test`
Expected: FAIL, cannot resolve `./run-diff`

- [ ] **Step 3: Write run-diff.tsx**

```tsx
// apps/web/components/run-diff.tsx
import type { EvalRunSummary } from "@/lib/api";

export type DiffResponse = {
  base: EvalRunSummary;
  head: EvalRunSummary;
  regressed: {
    item_id: string;
    question: string;
    base_faithfulness: number;
    head_faithfulness: number;
  }[];
};

export function RunDiff({ diff }: { diff: DiffResponse }) {
  const names = Object.keys(diff.base.metrics);

  return (
    <section className="space-y-6">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        {names.map((name) => {
          const base = diff.base.metrics[name];
          const head = diff.head.metrics[name];
          return (
            <div key={name} className="rounded-md border p-3">
              <p className="text-xs uppercase text-muted-foreground">{name}</p>
              <p
                data-testid={`metric-${name}`}
                className={head < base ? "text-red-500" : "text-emerald-500"}
              >
                {head.toFixed(2)}
                <span className="ml-2 text-xs text-muted-foreground">from {base.toFixed(2)}</span>
              </p>
            </div>
          );
        })}
      </div>

      {diff.regressed.length === 0 ? (
        <p className="text-sm text-muted-foreground">No regressions.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-2">Item</th>
              <th>Question</th>
              <th>Base</th>
              <th>Head</th>
            </tr>
          </thead>
          <tbody>
            {diff.regressed.map((item) => (
              <tr key={item.item_id} className="border-t">
                <td className="py-2 font-mono text-xs">{item.item_id}</td>
                <td>{item.question}</td>
                <td>{item.base_faithfulness.toFixed(2)}</td>
                <td className="text-red-500">{item.head_faithfulness.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
```

- [ ] **Step 4: Write run-table.tsx and the evals page**

Add `EvalRunSummary` to `lib/api.ts`:

```ts
// append to apps/web/lib/api.ts
export type EvalRunSummary = {
  id: number;
  git_sha: string;
  prompt_version: string;
  model: string;
  item_count: number;
  metrics: Record<string, number>;
  retrieval_config: Record<string, unknown>;
  created_at: string;
};
```

```tsx
// apps/web/components/run-table.tsx
import type { EvalRunSummary } from "@/lib/api";

export function RunTable({ runs }: { runs: EvalRunSummary[] }) {
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-muted-foreground">
          <th className="py-2">Run</th>
          <th>Commit</th>
          <th>Prompt</th>
          <th>Faithfulness</th>
          <th>Hit@5</th>
          <th>Deflection</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr key={run.id} className="border-t">
            <td className="py-2">{run.id}</td>
            <td className="font-mono text-xs">{run.git_sha.slice(0, 7)}</td>
            <td>{run.prompt_version}</td>
            <td>{run.metrics.faithfulness?.toFixed(2)}</td>
            <td>{run.metrics.hit_at_5?.toFixed(2)}</td>
            <td>{run.metrics.deflection_rate?.toFixed(2)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

```tsx
// apps/web/app/evals/page.tsx
import { RunDiff, type DiffResponse } from "@/components/run-diff";
import { RunTable } from "@/components/run-table";
import { type EvalRunSummary, getJSON } from "@/lib/api";

export default async function EvalsPage() {
  const runs = await getJSON<EvalRunSummary[]>("/eval-runs");
  const diff =
    runs.length >= 2
      ? await getJSON<DiffResponse>(`/eval-runs/diff?base=${runs[1].id}&head=${runs[0].id}`)
      : null;

  return (
    <main className="mx-auto max-w-5xl space-y-10 p-8">
      <h1 className="text-2xl font-semibold">Eval runs</h1>
      <RunTable runs={runs} />
      {diff && (
        <section className="space-y-4">
          <h2 className="text-lg font-medium">
            Latest run vs previous
          </h2>
          <RunDiff diff={diff} />
        </section>
      )}
    </main>
  );
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `npm test`
Expected: PASS (6 tests total across both component test files)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "add evals dashboard with run history and regression diff"
```

---

## Task 19: Traces surface

**Files:**
- Create: `apps/web/app/traces/page.tsx`, `apps/web/components/trace-row.tsx`, `apps/web/components/nav.tsx`
- Modify: `apps/web/app/layout.tsx`

**Interfaces:**
- Consumes: `getJSON` (Task 17); `GET /traces` (Task 16)
- Produces: `type TraceSummary`; `<TraceRow trace={TraceSummary} />`; `<Nav />`

No component test here: this surface is a read-only table with no branching logic, and it is exercised by the end-to-end demo path.

- [ ] **Step 1: Add the type to lib/api.ts**

```ts
// append to apps/web/lib/api.ts
export type TraceSummary = {
  id: number;
  question: string;
  escalated: boolean;
  reason: string | null;
  top_score: number;
  margin: number;
  retrieved: { chunk_id: number; source_path: string; score: number }[];
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  model: string;
  latency_ms: number;
  created_at: string;
};
```

- [ ] **Step 2: Write trace-row.tsx**

```tsx
// apps/web/components/trace-row.tsx
import { Badge } from "@/components/ui/badge";
import type { TraceSummary } from "@/lib/api";

export function TraceRow({ trace }: { trace: TraceSummary }) {
  return (
    <details className="border-t py-3">
      <summary className="flex cursor-pointer items-center justify-between gap-4 text-sm">
        <span className="truncate">{trace.question}</span>
        <span className="flex shrink-0 items-center gap-3 text-muted-foreground">
          {trace.escalated && <Badge variant="outline">{trace.reason}</Badge>}
          <span>{trace.latency_ms} ms</span>
          <span>${trace.cost_usd.toFixed(5)}</span>
        </span>
      </summary>

      <dl className="mt-3 grid grid-cols-2 gap-2 text-xs text-muted-foreground sm:grid-cols-4">
        <div><dt>Top score</dt><dd>{trace.top_score.toFixed(3)}</dd></div>
        <div><dt>Margin</dt><dd>{trace.margin.toFixed(3)}</dd></div>
        <div><dt>Tokens in</dt><dd>{trace.input_tokens}</dd></div>
        <div><dt>Tokens out</dt><dd>{trace.output_tokens}</dd></div>
      </dl>

      <ol className="mt-3 space-y-1 text-xs">
        {trace.retrieved.map((chunk) => (
          <li key={chunk.chunk_id} className="flex justify-between gap-4">
            <span className="font-mono">{chunk.source_path}</span>
            <span className="text-muted-foreground">{chunk.score.toFixed(3)}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}
```

- [ ] **Step 3: Write the traces page and nav**

```tsx
// apps/web/app/traces/page.tsx
import { TraceRow } from "@/components/trace-row";
import { type TraceSummary, getJSON } from "@/lib/api";

export default async function TracesPage() {
  const traces = await getJSON<TraceSummary[]>("/traces");

  return (
    <main className="mx-auto max-w-4xl space-y-6 p-8">
      <h1 className="text-2xl font-semibold">Traces</h1>
      <div>
        {traces.map((trace) => (
          <TraceRow key={trace.id} trace={trace} />
        ))}
      </div>
    </main>
  );
}
```

```tsx
// apps/web/components/nav.tsx
import Link from "next/link";

export function Nav() {
  return (
    <nav className="border-b">
      <div className="mx-auto flex max-w-5xl gap-6 p-4 text-sm">
        <Link href="/">Ask</Link>
        <Link href="/evals">Evals</Link>
        <Link href="/traces">Traces</Link>
      </div>
    </nav>
  );
}
```

Render `<Nav />` above `{children}` in `apps/web/app/layout.tsx`.

- [ ] **Step 4: Verify manually**

Ask several questions on `/`, then confirm `/traces` shows each with its retrieved chunks, scores, latency, and cost, and that escalated ones display a reason badge.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "add traces surface and top-level navigation"
```

---

## Task 20: Ablation, threshold sweep, and README

**Files:**
- Create: `services/api/scripts/ablate.py`, `services/api/scripts/sweep_thresholds.py`, `README.md`
- Modify: `services/api/src/deflect/routes/ask.py` (final threshold values)

**Interfaces:**
- Consumes: `run_evals` (Task 15); `RetrievalConfig` (Task 7); `GateThresholds` (Task 9); `load_dataset` (Task 12)
- Produces: two scripts printing markdown tables, and a README containing their output

This task produces the artifacts the spec names as success criteria. It is where the numbers that go on the resume come from.

- [ ] **Step 1: Write the ablation script**

```python
# services/api/scripts/ablate.py
"""Measures each retrieval stage independently. Output is pasted into the README."""

import asyncio
from pathlib import Path

from deflect.db import SessionFactory
from deflect.evals.dataset import load_dataset
from deflect.evals.metrics import hit_at_k, mrr
from deflect.retrieval.pipeline import RetrievalConfig, retrieve

VARIANTS = {
    "dense only": RetrievalConfig(use_lexical=False, use_rerank=False),
    "hybrid": RetrievalConfig(use_rerank=False),
    "hybrid + rerank": RetrievalConfig(),
}


async def main(dataset: Path) -> None:
    items = [item for item in load_dataset(dataset) if not item.should_escalate]

    print("| variant | hit@5 | MRR |")
    print("| --- | --- | --- |")
    async with SessionFactory() as session:
        for name, config in VARIANTS.items():
            hits_at_5, reciprocal_ranks = [], []
            for item in items:
                results = await retrieve(session, item.question, config)
                sources = [hit.source_path for hit in results]
                hits_at_5.append(hit_at_k(sources, item.expected_sources, k=5))
                reciprocal_ranks.append(mrr(sources, item.expected_sources))

            mean_hit = sum(hits_at_5) / len(hits_at_5)
            mean_mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
            print(f"| {name} | {mean_hit:.3f} | {mean_mrr:.3f} |")


if __name__ == "__main__":
    asyncio.run(main(Path("../../evals/golden.yaml")))
```

Run: `uv run python scripts/ablate.py`
Expected: three rows with hit@5 and MRR rising across variants. If reranking does not improve MRR, try the alternate cross-encoder in `rerank_model` before accepting the result, and report whichever wins.

- [ ] **Step 2: Write the threshold sweep**

```python
# services/api/scripts/sweep_thresholds.py
"""Produces the deflection-rate against wrong-answer-rate curve used to pick thresholds."""

import asyncio
from pathlib import Path

from deflect.answer.gate import GateThresholds, evaluate_gate
from deflect.db import SessionFactory
from deflect.evals.dataset import load_dataset
from deflect.retrieval.pipeline import RetrievalConfig, retrieve

CANDIDATES = [round(0.1 * i, 2) for i in range(0, 10)]


async def main(dataset: Path) -> None:
    items = load_dataset(dataset)

    # Retrieval is run once per item and reused across thresholds: the gate is a
    # pure function of scores, so sweeping it needs no further model calls.
    async with SessionFactory() as session:
        retrieved = {
            item.id: await retrieve(session, item.question, RetrievalConfig())
            for item in items
        }

    print("| min_top_score | answered | wrongly refused | wrongly answered |")
    print("| --- | --- | --- | --- |")
    for threshold in CANDIDATES:
        thresholds = GateThresholds(min_top_score=threshold, min_margin=0.0)
        answered = wrongly_refused = wrongly_answered = 0

        for item in items:
            decision = evaluate_gate(retrieved[item.id], grounded=True, thresholds=thresholds)
            if not decision.escalate:
                answered += 1
                if item.should_escalate:
                    wrongly_answered += 1
            elif not item.should_escalate:
                wrongly_refused += 1

        total = len(items)
        print(
            f"| {threshold:.2f} | {answered / total:.2f} "
            f"| {wrongly_refused / total:.2f} | {wrongly_answered / total:.2f} |"
        )


if __name__ == "__main__":
    asyncio.run(main(Path("../../evals/golden.yaml")))
```

Run: `uv run python scripts/sweep_thresholds.py`

- [ ] **Step 3: Choose the operating point**

From the swept table, pick the largest `min_top_score` whose wrongly-answered rate is at or below 0.05, preferring higher answered rate on ties. Update `THRESHOLDS` in `services/api/src/deflect/routes/ask.py` with the chosen values and record the reasoning in the README.

- [ ] **Step 4: Write the README**

```markdown
# Deflect

Answers FastAPI support questions from the official documentation with citations, and
escalates to a human when its confidence signals say it should not guess.

## Why the escalation matters

A support assistant that answers everything is worse than one that answers less. A
confidently wrong answer costs a support team more than no answer, because it has to
be discovered and undone. Deflect measures both rates rather than optimizing one.

## Results

Retrieval ablation over the answerable half of the golden dataset:

<paste the ablate.py table>

Threshold sweep. The operating point is <chosen value>, the highest threshold holding
the wrongly-answered rate at or below 5 percent:

<paste the sweep_thresholds.py table>

## Architecture

<the diagram from the spec>

Retrieval runs dense pgvector search and Postgres full-text search concurrently, merges
them with Reciprocal Rank Fusion, and reranks the top 20 candidates with a local
cross-encoder. Dense search alone misses exact tokens such as `Depends` and `422`;
lexical search alone misses paraphrase.

The web app never calls a model. It proxies an SSE stream from the FastAPI service, so
provider keys stay server-side and the eval harness exercises the same answer code path
the live app does.

## Evals

`evals/golden.yaml` holds 80 hand-labeled questions, 15 of which are unanswerable from
the corpus and must be refused. Metrics are split into deterministic retrieval scores
(hit@5, MRR) and LLM-as-judge generation scores (faithfulness, answer relevance,
context relevance), so a regression can be attributed to retrieval or to generation
without ambiguity.

CI runs a 10-item smoke set on every pull request and fails the build on regression.
The full dataset runs nightly.

## Running it

<setup steps: docker compose up, ingest, run the API, run the web app>

Corpus is pinned to fastapi/fastapi at <SHA>.
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "add retrieval ablation, threshold sweep, and README results"
```

---

## Task 21: CI with an eval regression gate

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/nightly-evals.yml`

**Interfaces:**
- Consumes: `scripts/run_evals.py` (Task 15)
- Produces: a pull request check that fails when faithfulness drops below the floor

- [ ] **Step 1: Write the CI workflow**

```yaml
# .github/workflows/ci.yml
name: ci

on: [push, pull_request]

jobs:
  api:
    runs-on: ubuntu-latest
    services:
      db:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: deflect
          POSTGRES_PASSWORD: deflect
          POSTGRES_DB: deflect
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U deflect"
          --health-interval 5s --health-retries 10
    env:
      DATABASE_URL: postgresql+asyncpg://deflect:deflect@localhost:5432/deflect
    defaults:
      run:
        working-directory: services/api
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync
      - run: uv run ruff check .
      - run: uv run alembic upgrade head
      - run: uv run pytest -q

  web:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: apps/web
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: npm
          cache-dependency-path: apps/web/package-lock.json
      - run: npm ci
      - run: npm run lint
      - run: npm test
      - run: npm run build

  eval-smoke:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    services:
      db:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: deflect
          POSTGRES_PASSWORD: deflect
          POSTGRES_DB: deflect
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U deflect"
          --health-interval 5s --health-retries 10
    env:
      DATABASE_URL: postgresql+asyncpg://deflect:deflect@localhost:5432/deflect
      GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    defaults:
      run:
        working-directory: services/api
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync
      - run: uv run alembic upgrade head
      - name: Ingest a pinned corpus snapshot
        run: |
          git clone --depth 1 https://github.com/fastapi/fastapi /tmp/fastapi-src
          uv run python scripts/ingest.py /tmp/fastapi-src/docs/en/docs "$(git -C /tmp/fastapi-src rev-parse HEAD)"
      - name: Eval smoke set
        run: uv run python scripts/run_evals.py --limit 10 --fail-under 0.85
```

- [ ] **Step 2: Write the nightly workflow**

```yaml
# .github/workflows/nightly-evals.yml
name: nightly-evals

on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch:

jobs:
  full-evals:
    runs-on: ubuntu-latest
    services:
      db:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: deflect
          POSTGRES_PASSWORD: deflect
          POSTGRES_DB: deflect
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U deflect"
          --health-interval 5s --health-retries 10
    env:
      DATABASE_URL: postgresql+asyncpg://deflect:deflect@localhost:5432/deflect
      GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    defaults:
      run:
        working-directory: services/api
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync
      - run: uv run alembic upgrade head
      - run: |
          git clone --depth 1 https://github.com/fastapi/fastapi /tmp/fastapi-src
          uv run python scripts/ingest.py /tmp/fastapi-src/docs/en/docs "$(git -C /tmp/fastapi-src rev-parse HEAD)"
      - run: uv run python scripts/run_evals.py --fail-under 0.85
```

- [ ] **Step 3: Add the repository secret**

Add `GEMINI_API_KEY` under repository settings, Secrets and variables, Actions.

- [ ] **Step 4: Prove the gate works**

Open a pull request that weakens the answer prompt, for example by deleting the line
requiring the model to use only the passages. Confirm the `eval-smoke` job fails on
faithfulness. Close the pull request without merging, then screenshot the failing check
for the README.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "add ci with eval regression gate and nightly full run"
```

---

## Task 22: Deploy

**Files:**
- Create: `render.yaml`
- Modify: `apps/web/.env.example`, `README.md`

**Interfaces:**
- Consumes: everything above
- Produces: a public URL running the demo path

- [ ] **Step 1: Provision Neon**

Create a Neon project, run `CREATE EXTENSION vector`, and record the pooled connection string. Run `alembic upgrade head` against it, then ingest the pinned corpus.

- [ ] **Step 2: Write render.yaml and deploy the API**

```yaml
services:
  - type: web
    name: deflect-api
    runtime: docker
    dockerfilePath: ./services/api/Dockerfile
    dockerContext: ./services/api
    healthCheckPath: /health
    envVars:
      - key: DATABASE_URL
        sync: false
      - key: GEMINI_API_KEY
        sync: false
```

Deploy, then confirm `/health` returns `{"status": "ok", "database": "connected"}`.

- [ ] **Step 3: Deploy the web app**

Deploy `apps/web` to Vercel with `API_URL` and `NEXT_PUBLIC_API_URL` set to the Render
URL. Update the CORS `allow_origins` list in `main.py` to include the Vercel domain.

- [ ] **Step 4: Verify the demo path end to end**

On the deployed URL: ask an answerable question and confirm a streamed answer with
working citations; ask an unanswerable one and confirm the escalation card; open
`/traces` and confirm both requests appear with costs; open `/evals` and confirm run
history renders.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "add render service definition and deployment notes"
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: corpus and pinning (4), architecture (1, 17), models table (8), retrieval pipeline with all four stages (3, 5, 6, 7), confidence gate including the structured-output groundedness signal (9, 10), golden dataset with the 15 unanswerable items (12), split metric families (13, 14), run storage with SHA and prompt version (15), data model (2, 11, 15), three web surfaces (17, 18, 19), testing including Vitest scoped to the dashboard (17, 18), CI with the PR smoke gate and nightly run (21), deployment without Kubernetes (22), and both success-criteria artifacts (20).

**Deferred decisions resolved.** The spec deferred chunk size, threshold value, and reranker choice to implementation. Chunk size defaults are set in Task 3 and adjustable via `chunk_markdown` parameters; the threshold is chosen in Task 20 Step 3 from the swept table; the reranker comparison happens in Task 20 Step 1.

**Type consistency.** `Hit` flows unchanged from Task 5 through fusion, rerank, gate, answer service, and runner. `RetrievalConfig` and `GateThresholds` are constructed identically in the route, the runner, and both scripts. `AskDone` in `lib/api.ts` matches the `done` event emitted by `routes/ask.py` field for field. `EvalRunSummary` matches `_run_summary`.

**Known ordering constraint.** In Task 16, `/eval-runs/diff` must be declared before `/eval-runs/{run_id}` or FastAPI captures `diff` as a path parameter. This is called out in the task.
