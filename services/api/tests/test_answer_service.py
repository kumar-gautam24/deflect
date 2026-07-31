from sqlalchemy import select

from deflect.answer.gate import GateThresholds
from deflect.answer.service import answer_question
from deflect.llm.fake import FakeClient
from deflect.models import Chunk
from deflect.retrieval.pipeline import RetrievalConfig

# Cross-encoder scores are unbounded logits, not 0-1 similarities: real queries
# range from about -11 to +8. Only an infinite floor is genuinely permissive.
PERMISSIVE = GateThresholds(min_top_score=float("-inf"), min_margin=float("-inf"))


async def test_answer_includes_citations_for_the_chunks_the_model_used(
    session, corpus, answer_payload
):
    chunk_id = (
        await session.execute(select(Chunk.id).where(Chunk.document_id == corpus.id))
    ).scalars().first()
    client = FakeClient([answer_payload("Use Depends.", [chunk_id], True)])

    result = await answer_question(
        session, "how do I inject a dependency", client, RetrievalConfig(), PERMISSIVE
    )

    assert result.answer == "Use Depends."
    assert [c.chunk_id for c in result.citations] == [chunk_id]
    assert result.citations[0].source_path == "deps.md"


async def test_retrieved_chunks_are_present_in_the_prompt(session, corpus, answer_payload):
    client = FakeClient([answer_payload("x", [], True)])

    await answer_question(session, "dependency injection", client, RetrievalConfig(), PERMISSIVE)

    assert "Use Depends" in client.prompts[0]
    assert "dependency injection" in client.prompts[0]


async def test_ungrounded_model_response_escalates(session, corpus, answer_payload):
    client = FakeClient([answer_payload("Invented answer.", [], False)])

    result = await answer_question(
        session, "dependency injection", client, RetrievalConfig(), PERMISSIVE
    )

    assert result.decision.escalate is True
    assert result.decision.reason == "ungrounded_answer"


async def test_weak_retrieval_escalates_and_returns_no_citations(session, corpus, answer_payload):
    client = FakeClient([answer_payload("Some answer.", [], True)])
    strict = GateThresholds(min_top_score=99.0, min_margin=0.0)

    result = await answer_question(session, "unrelated topic", client, RetrievalConfig(), strict)

    assert result.decision.escalate is True
    assert result.citations == []


async def test_token_usage_and_prompt_version_are_reported(session, corpus, answer_payload):
    client = FakeClient([answer_payload("x", [], True)])

    result = await answer_question(
        session, "dependency injection", client, RetrievalConfig(), PERMISSIVE
    )

    assert result.output_tokens > 0
    assert result.prompt_version == "answer_v1"


async def test_citations_referencing_unretrieved_chunks_are_dropped(
    session, corpus, answer_payload
):
    client = FakeClient([answer_payload("x", [999999], True)])

    result = await answer_question(
        session, "dependency injection", client, RetrievalConfig(), PERMISSIVE
    )

    assert result.citations == []
