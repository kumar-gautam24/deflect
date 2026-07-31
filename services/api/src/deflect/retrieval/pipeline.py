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
