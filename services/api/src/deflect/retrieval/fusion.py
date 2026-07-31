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
