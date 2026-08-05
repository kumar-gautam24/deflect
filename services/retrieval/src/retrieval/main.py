from pathlib import Path

from deflect_common.auth import bearer_guard
from deflect_common.schemas import (
    IngestRequest,
    IngestResponse,
    SearchRequest,
    SearchResponse,
)
from fastapi import Depends, FastAPI, HTTPException
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


def resolve_corpus_path(root: str, corpus_root: Path) -> Path:
    """Resolve a requested ingest root, refusing anything outside `corpus_root`.

    Resolution happens before the check so a symlink pointing out of the corpus is
    caught, not only a literal `..`. is_relative_to compares path components rather
    than string prefixes, so a sibling named corpus-secrets does not pass as /corpus.
    """
    try:
        requested = Path(root).resolve()
    except (ValueError, OSError) as cause:
        # A path that cannot even be resolved is invalid input, not a server fault.
        # Same message as the containment rejection: a caller learns only that the
        # path was refused, never which of the two checks refused it.
        raise HTTPException(
            status_code=400, detail="ingest root is outside the corpus root"
        ) from cause
    allowed = corpus_root.resolve()

    if requested != allowed and not requested.is_relative_to(allowed):
        # The rejected path is deliberately absent from the message: echoing it back
        # would turn this endpoint into a way to map the container filesystem.
        raise HTTPException(status_code=400, detail="ingest root is outside the corpus root")

    return requested


@app.post("/ingest", dependencies=[Depends(require_operator)])
async def ingest(request: IngestRequest, session: SessionDep) -> IngestResponse:
    root = resolve_corpus_path(request.root, get_settings().corpus_root)
    count = await ingest_directory(session, root, request.commit_sha)
    await session.commit()
    return IngestResponse(chunks=count)
