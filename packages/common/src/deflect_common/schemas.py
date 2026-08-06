"""Wire contracts between services.

These are the only types that cross a service boundary. Both sides of every call
import them from here, so a change to a contract is a single edit that breaks
compilation on both sides rather than a silent mismatch discovered at runtime.
"""

from pydantic import BaseModel, Field


class Hit(BaseModel):
    chunk_id: int
    document_id: int
    source_path: str
    heading_path: str
    text: str
    score: float


class SearchRequest(BaseModel):
    query: str
    use_dense: bool = True
    use_lexical: bool = True
    use_rerank: bool = True
    candidate_limit: int = 20
    final_limit: int = 5


class SearchResponse(BaseModel):
    hits: list[Hit]


class IngestRequest(BaseModel):
    root: str
    commit_sha: str


class IngestResponse(BaseModel):
    chunks: int


class Citation(BaseModel):
    source_path: str
    heading_path: str
    chunk_id: int


class AnswerRequest(BaseModel):
    question: str
    # Evals sweep retrieval variants through the same endpoint the app uses, so the
    # config has to be part of the contract rather than baked into the service.
    search: SearchRequest | None = None
    min_top_score: float | None = None
    min_margin: float | None = None


class AnswerResponse(BaseModel):
    trace_id: int
    answer: str
    citations: list[Citation]
    escalated: bool
    reason: str | None
    top_score: float
    margin: float
    hits: list[Hit]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str
    prompt_version: str
    latency_ms: int
    # The gate configuration that produced this result. Reported rather than assumed
    # so an eval run can record what it actually measured against.
    min_top_score: float
    min_margin: float


class RunEvalsRequest(BaseModel):
    limit: int | None = None
    # Removed: a run is submitted before anything is scored, so there is no
    # faithfulness to compare against. Leaving it would silently ignore a caller's
    # gate, which is worse than making them notice it moved. CI polls and applies
    # the threshold itself.
    # Sweeps a retrieval variant through the answer service end to end. The query is
    # replaced per item; the rest is what the run is measuring.
    search: SearchRequest | None = None
