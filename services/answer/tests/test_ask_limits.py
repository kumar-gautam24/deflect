from datetime import UTC, datetime, timedelta

from doubles import FakeRetrieval
from httpx import ASGITransport, AsyncClient

from answer.models import Trace
from answer.ratelimit import questions_today


def _trace(created_at: datetime) -> Trace:
    return Trace(
        question="q",
        answer="a",
        escalated=False,
        reason=None,
        top_score=5.0,
        margin=1.0,
        retrieved=[],
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        model="fake",
        prompt_version="v1",
        latency_ms=1,
        min_top_score=2.0,
        min_margin=0.0,
        created_at=created_at,
    )


async def test_only_todays_questions_count_towards_the_cap(session):
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    session.add_all(
        [
            _trace(now),
            _trace(now - timedelta(hours=6)),           # earlier today
            _trace(now - timedelta(days=1)),            # yesterday
        ]
    )
    await session.flush()

    assert await questions_today(session, now) == 2


async def test_a_question_just_after_midnight_starts_a_fresh_day(session):
    now = datetime(2026, 8, 5, 0, 0, 1, tzinfo=UTC)
    session.add_all([_trace(now), _trace(now - timedelta(seconds=2))])
    await session.flush()

    assert await questions_today(session, now) == 1


async def test_the_daily_cap_rejects_once_the_budget_is_spent(
    session, make_app, hits, answer_payload, monkeypatch
):
    """Fails closed: the counter is a database query, and if that is broken the ask
    path is already broken, so refusing is honest."""
    monkeypatch.setenv("ASK_DAILY_LIMIT", "1")
    from answer.config import get_settings

    get_settings.cache_clear()

    app = make_app([answer_payload("Use Depends.", [1], True)] * 2, FakeRetrieval(hits))
    session.add(_trace(datetime.now(UTC)))
    await session.flush()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "anything"})

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    get_settings.cache_clear()


async def test_a_question_within_budget_is_still_answered(
    session, make_app, hits, answer_payload
):
    app = make_app([answer_payload("Use Depends.", [1], True)], FakeRetrieval(hits))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "how do I declare one"})

    assert response.status_code == 200
