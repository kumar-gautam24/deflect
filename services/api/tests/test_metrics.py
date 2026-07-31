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


def test_empty_retrieval_scores_zero_rather_than_raising():
    assert hit_at_k([], ["a.md"], k=5) == 0.0
    assert mrr([], ["a.md"]) == 0.0
    assert precision_at_k([], ["a.md"], k=5) == 0.0
