from pathlib import Path

from deflect_common.schemas import (
    IngestRequest,
    IngestResponse,
    SearchRequest,
    SearchResponse,
)
from fastapi import FastAPI
from sqlalchemy import text

from retrieval.db import SessionDep
from retrieval.ingest.pipeline import ingest_directory
from retrieval.pipeline import RetrievalConfig, retrieve

app = FastAPI(title="Deflect retrieval")


@app.get("/health")
async def health(session: SessionDep) -> dict[str, str]:
    await session.execute(text("select 1"))
    return {"status": "ok", "database": "connected"}


@app.post("/search")
async def search(request: SearchRequest, session: SessionDep) -> SearchResponse:
    config = RetrievalConfig(
        use_dense=request.use_dense,
        use_lexical=request.use_lexical,
        use_rerank=request.use_rerank,
        candidate_limit=request.candidate_limit,
        final_limit=request.final_limit,
    )
    return SearchResponse(hits=await retrieve(session, request.query, config))


@app.post("/ingest")
async def ingest(request: IngestRequest, session: SessionDep) -> IngestResponse:
    count = await ingest_directory(session, Path(request.root), request.commit_sha)
    await session.commit()
    return IngestResponse(chunks=count)
