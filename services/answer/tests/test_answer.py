import json

from doubles import FakeRetrieval, hit
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from answer.models import Escalation, Trace
from answer.retrieval_client import RetrievalClient
from answer.telemetry import estimate_cost


async def post(app, path: str, body: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # /answer is service-only. Sending the credential here rather than overriding the
        # guard keeps these tests exercising the real dependency chain, so a guard that
        # stopped working would still fail them.
        return await client.post(
            path, json=body, headers={"Authorization": "Bearer test-service-token"}
        )


def test_cost_scales_with_token_counts():
    assert estimate_cost("gemini-2.0-flash", 10000, 10000) > estimate_cost(
        "gemini-2.0-flash", 1000, 1000
    ) > 0


def test_unknown_model_has_no_priced_cost():
    assert estimate_cost("fake", 100, 100) == 0.0


async def test_answer_returns_citations_for_the_chunks_the_model_used(
    session, make_app, hits, answer_payload
):
    app = make_app([answer_payload("Use Depends.", [1], True)], FakeRetrieval(hits))

    body = (await post(app, "/answer", {"question": "how do I declare a dependency"})).json()

    assert body["answer"] == "Use Depends."
    assert [c["chunk_id"] for c in body["citations"]] == [1]
    assert body["escalated"] is False


async def test_retrieved_chunks_reach_the_prompt(session, make_app, hits, answer_payload):
    retrieval = FakeRetrieval(hits)
    app = make_app([answer_payload("x", [], True)], retrieval)

    await post(app, "/answer", {"question": "how do I declare a dependency"})

    assert retrieval.requests[0].query == "how do I declare a dependency"


async def test_ungrounded_answer_escalates_and_drops_citations(
    session, make_app, hits, answer_payload
):
    app = make_app([answer_payload("Invented.", [1], False)], FakeRetrieval(hits))

    body = (await post(app, "/answer", {"question": "how do I declare a dependency"})).json()

    assert body["escalated"] is True
    assert body["reason"] == "ungrounded_answer"
    assert body["citations"] == []


async def test_weak_retrieval_escalates(session, make_app, answer_payload):
    app = make_app([answer_payload("x", [], True)], FakeRetrieval([hit(1, "unrelated", -6.0)]))

    body = (await post(app, "/answer", {"question": "unrelated"})).json()

    assert body["escalated"] is True
    assert body["reason"] == "low_retrieval_score"


async def test_citations_referencing_unretrieved_chunks_are_dropped(
    session, make_app, hits, answer_payload
):
    app = make_app([answer_payload("x", [999999], True)], FakeRetrieval(hits))

    body = (await post(app, "/answer", {"question": "how do I declare a dependency"})).json()

    assert body["citations"] == []


async def test_answering_writes_a_trace_and_an_escalation_row(
    session, make_app, hits, answer_payload
):
    app = make_app([answer_payload("Invented.", [], False)], FakeRetrieval(hits))

    await post(app, "/answer", {"question": "how do I declare a dependency"})

    trace = (await session.execute(select(Trace))).scalars().one()
    escalation = (await session.execute(select(Escalation))).scalars().one()
    assert trace.model == "fake"
    assert escalation.trace_id == trace.id


async def test_retrieval_being_down_returns_503_rather_than_answering(
    session, make_app, answer_payload
):
    """A failure mode created by the split, exercised through the real client.

    A fake would only prove the fake raises. This points RetrievalClient at a port
    nothing listens on, so the httpx error and its translation are both real.
    """
    app = make_app(
        [answer_payload("x", [], True)],
        RetrievalClient("http://127.0.0.1:1", "test-service-token", timeout=1.0),
    )

    response = await post(app, "/answer", {"question": "how do I declare a dependency"})

    assert response.status_code == 503
    assert "retrieval service unavailable" in response.json()["detail"]


async def test_ask_streams_tokens_then_a_done_event(session, make_app, hits, answer_payload):
    app = make_app([answer_payload("Use Depends.", [1], True)], FakeRetrieval(hits))

    response = await post(app, "/ask", {"question": "how do I declare a dependency"})

    events = [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed.strip() == "Use Depends."
    assert events[-1]["type"] == "done"
    assert events[-1]["citations"]


async def test_search_config_passes_through_so_evals_can_sweep_variants(
    session, make_app, hits, answer_payload
):
    retrieval = FakeRetrieval(hits)
    app = make_app([answer_payload("x", [], True)], retrieval)

    await post(
        app,
        "/answer",
        {
            "question": "q",
            "search": {"query": "q", "use_rerank": False, "final_limit": 3},
        },
    )

    assert retrieval.requests[0].use_rerank is False
    assert retrieval.requests[0].final_limit == 3
