"""Retrieval stage orchestration. Stages are toggleable so the ablation is reproducible."""

from dataclasses import dataclass

from deflect_common.schemas import Hit
from sqlalchemy.ext.asyncio import AsyncSession

from retrieval.fusion import reciprocal_rank_fusion
from retrieval.rerank import rerank
from retrieval.search import dense_search, lexical_search


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

    # Run sequentially, not with asyncio.gather. An AsyncSession does not permit
    # concurrent operations: two queries racing to provision its connection raises
    # InvalidRequestError. Both searches are index-backed and fast, so the ordering
    # costs little, and sharing one session is what lets callers see uncommitted rows.
    rankings: list[list[Hit]] = []
    if config.use_dense:
        rankings.append(await dense_search(session, query, config.candidate_limit))
    if config.use_lexical:
        rankings.append(await lexical_search(session, query, config.candidate_limit))

    fused = reciprocal_rank_fusion(rankings)

    if config.use_rerank:
        return rerank(query, fused[: config.candidate_limit], config.final_limit)
    return fused[: config.final_limit]
