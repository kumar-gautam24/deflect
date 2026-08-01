"""The wire contracts. A change here breaks every service at once, so the shape is
asserted explicitly rather than left to whichever service happens to fail first."""

import json

import pytest
from pydantic import ValidationError

from deflect_common.schemas import AnswerRequest, AnswerResponse, Hit, SearchRequest


def hit(**overrides) -> Hit:
    return Hit(
        **{
            "chunk_id": 1,
            "document_id": 1,
            "source_path": "deps.md",
            "heading_path": "Dependencies",
            "text": "Use Depends.",
            "score": 6.0,
        }
        | overrides
    )


def test_search_request_defaults_match_the_production_pipeline():
    request = SearchRequest(query="q")

    assert (request.use_dense, request.use_lexical, request.use_rerank) == (True, True, True)
    assert (request.candidate_limit, request.final_limit) == (20, 5)


def test_answer_request_carries_a_search_variant_for_sweeps():
    request = AnswerRequest(
        question="q", search=SearchRequest(query="q", use_rerank=False, final_limit=3)
    )

    assert request.search.use_rerank is False
    assert request.search.final_limit == 3


def test_answer_request_needs_only_a_question():
    request = AnswerRequest(question="q")

    assert request.search is None
    assert request.min_top_score is None


def test_hit_rejects_a_missing_field():
    with pytest.raises(ValidationError):
        Hit(chunk_id=1, document_id=1, source_path="a.md", heading_path="A", text="t")


def test_answer_response_reports_the_gate_configuration_that_produced_it():
    """Eval runs record these for reproducibility, so they are required, not optional."""
    fields = AnswerResponse.model_fields

    assert fields["min_top_score"].is_required()
    assert fields["min_margin"].is_required()


def test_hit_round_trips_through_json():
    original = hit(score=-3.25)

    assert Hit.model_validate(json.loads(original.model_dump_json())) == original
