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
