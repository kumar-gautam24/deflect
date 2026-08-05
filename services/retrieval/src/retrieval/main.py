from pathlib import Path

from deflect_common.auth import bearer_guard
from deflect_common.schemas import (
    IngestRequest,
    IngestResponse,
    SearchRequest,
    SearchResponse,
)
from fastapi import Depends, FastAPI
from sqlalchemy import select, text

from retrieval.config import get_settings
from retrieval.db import SessionDep
from retrieval.ingest.pipeline import ingest_directory
from retrieval.models import Document
from retrieval.pipeline import RetrievalConfig, retrieve

app = FastAPI(title="Deflect retrieval")

# Built at import, not in a lifespan: an unset token aborts this module and uvicorn
# exits before binding a port. Module-level names are also what dependency_overrides
# keys on when a test bypasses a guard.
_settings = get_settings()
require_service = bearer_guard(_settings.service_token, "service")
require_operator = bearer_guard(_settings.operator_token, "operator")


@app.get("/health")
async def health(session: SessionDep) -> dict[str, str]:
    await session.execute(text("select 1"))
    return {"status": "ok", "database": "connected"}


@app.get("/documents", dependencies=[Depends(require_service)])
async def documents(session: SessionDep) -> dict[str, list[str]]:
    """Every ingested source path.

    Exposed so the eval service can check that the golden dataset names documents
    that actually exist. A typo there would otherwise look like a permanent
    retrieval regression rather than a bad label.
    """
    paths = (await session.execute(select(Document.source_path))).scalars().all()
    return {"source_paths": sorted(paths)}


@app.post("/search", dependencies=[Depends(require_service)])
async def search(request: SearchRequest, session: SessionDep) -> SearchResponse:
    config = RetrievalConfig(
        use_dense=request.use_dense,
        use_lexical=request.use_lexical,
        use_rerank=request.use_rerank,
        candidate_limit=request.candidate_limit,
        final_limit=request.final_limit,
    )
    return SearchResponse(hits=await retrieve(session, request.query, config))


@app.post("/ingest", dependencies=[Depends(require_operator)])
async def ingest(request: IngestRequest, session: SessionDep) -> IngestResponse:
    count = await ingest_directory(session, Path(request.root), request.commit_sha)
    await session.commit()
    return IngestResponse(chunks=count)
