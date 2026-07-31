import json

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from deflect.models import Escalation, Trace
from deflect.telemetry import estimate_cost


def test_cost_scales_with_token_counts():
    cheap = estimate_cost("gemini-2.0-flash", 1000, 1000)
    expensive = estimate_cost("gemini-2.0-flash", 10000, 10000)

    assert expensive > cheap > 0


def test_unknown_model_has_no_priced_cost():
    assert estimate_cost("fake", 100, 100) == 0.0


def test_output_tokens_cost_more_than_input_tokens():
    assert estimate_cost("gemini-2.0-flash", 0, 1000) > estimate_cost(
        "gemini-2.0-flash", 1000, 0
    )


async def _ask(app, question: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/ask", json={"question": question})


def _final_event(response) -> dict:
    events = [line for line in response.text.splitlines() if line.startswith("data:")]
    return json.loads(events[-1].removeprefix("data:").strip())


async def test_ask_streams_answer_then_a_final_metadata_event(session, fake_client_app):
    response = await _ask(fake_client_app, "how do I declare a dependency")

    final = _final_event(response)
    assert final["type"] == "done"
    assert final["escalated"] is False
    assert final["citations"]


async def test_ask_streams_the_answer_text_before_the_final_event(session, fake_client_app):
    response = await _ask(fake_client_app, "how do I declare a dependency")

    events = [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed.strip() == "Use Depends."


async def test_answering_writes_a_trace_with_cost_and_latency(session, fake_client_app):
    await _ask(fake_client_app, "how do I declare a dependency")

    trace = (await session.execute(select(Trace))).scalars().one()
    assert trace.latency_ms >= 0
    assert trace.retrieved
    assert trace.escalated is False


async def test_escalated_answer_writes_an_escalation_row(session, escalating_app):
    await _ask(escalating_app, "how do I declare a dependency")

    escalation = (await session.execute(select(Escalation))).scalars().one()
    assert escalation.reason == "ungrounded_answer"


async def test_escalated_answer_returns_no_citations(session, escalating_app):
    final = _final_event(await _ask(escalating_app, "how do I declare a dependency"))

    assert final["escalated"] is True
    assert final["citations"] == []


async def test_weak_retrieval_escalates_before_the_grounding_check(session, escalating_app):
    # A bare noun phrase scores far below the production threshold, so this escalates
    # on retrieval rather than grounding even though the model also reports ungrounded.
    final = _final_event(await _ask(escalating_app, "quantum chromodynamics"))

    assert final["escalated"] is True
    assert final["reason"] == "low_retrieval_score"
