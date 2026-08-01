"""Cross-encoder reranking of fused candidates."""

from functools import lru_cache

from deflect_common.schemas import Hit
from fastembed.rerank.cross_encoder import TextCrossEncoder

from retrieval.config import get_settings


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
    rescored = [
        hit.model_copy(update={"score": float(score)})
        for hit, score in zip(hits, scores, strict=True)
    ]
    return sorted(rescored, key=lambda h: h.score, reverse=True)[:limit]
