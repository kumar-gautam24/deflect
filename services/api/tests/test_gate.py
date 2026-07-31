import pytest

from deflect.answer.gate import GateThresholds, evaluate_gate
from deflect.retrieval.search import Hit


def hits(*scores: float) -> list[Hit]:
    return [Hit(i, 1, "a.md", "A", "text", score) for i, score in enumerate(scores)]


THRESHOLDS = GateThresholds(min_top_score=0.5, min_margin=0.1)


def test_confident_grounded_answer_is_not_escalated():
    decision = evaluate_gate(hits(0.9, 0.4), grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is False
    assert decision.reason is None


def test_weak_top_score_escalates():
    decision = evaluate_gate(hits(0.2, 0.1), grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is True
    assert decision.reason == "low_retrieval_score"


def test_ambiguous_results_escalate_even_when_the_top_score_is_high():
    decision = evaluate_gate(hits(0.9, 0.88), grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is True
    assert decision.reason == "ambiguous_retrieval"


def test_ungrounded_answer_escalates_despite_strong_retrieval():
    decision = evaluate_gate(hits(0.95, 0.3), grounded=False, thresholds=THRESHOLDS)

    assert decision.escalate is True
    assert decision.reason == "ungrounded_answer"


def test_no_retrieved_chunks_escalates():
    decision = evaluate_gate([], grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is True
    assert decision.reason == "no_results"


def test_single_hit_uses_its_own_score_as_the_margin():
    decision = evaluate_gate(hits(0.9), grounded=True, thresholds=THRESHOLDS)

    assert decision.escalate is False
    assert decision.margin == 0.9


def test_decision_reports_the_signals_it_used():
    decision = evaluate_gate(hits(0.9, 0.4), grounded=True, thresholds=THRESHOLDS)

    assert decision.top_score == 0.9
    assert decision.margin == pytest.approx(0.5)


def test_grounding_can_be_disabled_independently_of_retrieval_thresholds():
    thresholds = GateThresholds(min_top_score=0.5, min_margin=0.1, require_grounded=False)

    decision = evaluate_gate(hits(0.9, 0.4), grounded=False, thresholds=thresholds)

    assert decision.escalate is False


def test_non_finite_thresholds_are_rejected():
    # Thresholds are persisted as JSON with every eval run, and JSON has no infinity.
    with pytest.raises(ValueError, match="must be finite"):
        GateThresholds(min_top_score=float("-inf"))
