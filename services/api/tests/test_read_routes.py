from httpx import ASGITransport, AsyncClient

from deflect.models import EvalResult, EvalRun, Trace


def make_run(sha: str, faithfulness: float) -> EvalRun:
    return EvalRun(
        git_sha=sha,
        prompt_version="answer_v1",
        judge_version="judge_v1",
        model="fake",
        retrieval_config={},
        thresholds={},
        item_count=1,
        metrics={"faithfulness": faithfulness},
    )


def make_result(run_id: int, item_id: str, faithfulness: float | None) -> EvalResult:
    return EvalResult(
        run_id=run_id,
        item_id=item_id,
        question="q",
        answer="a",
        escalated=False,
        expected_escalate=False,
        retrieved_sources=["a.md"],
        hit_at_5=1.0,
        mrr=1.0,
        faithfulness=faithfulness,
        answer_relevance=1.0,
        context_relevance=1.0,
        rationale="ok",
    )


async def get(app, path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_run_list_is_newest_first(session, app_with_session):
    session.add_all([make_run("old", 0.9), make_run("new", 0.8)])
    await session.flush()

    body = (await get(app_with_session, "/eval-runs")).json()

    assert [r["git_sha"] for r in body[:2]] == ["new", "old"]


async def test_run_detail_includes_per_item_results(session, app_with_session):
    run = make_run("sha", 1.0)
    session.add(run)
    await session.flush()
    session.add(make_result(run.id, "q1", 1.0))
    await session.flush()

    body = (await get(app_with_session, f"/eval-runs/{run.id}")).json()

    assert body["metrics"]["faithfulness"] == 1.0
    assert [r["item_id"] for r in body["results"]] == ["q1"]


async def test_diff_flags_items_whose_score_dropped(session, app_with_session):
    base, head = make_run("base", 1.0), make_run("head", 0.5)
    session.add_all([base, head])
    await session.flush()
    session.add_all(
        [
            make_result(base.id, "q1", 1.0),
            make_result(base.id, "q2", 1.0),
            make_result(head.id, "q1", 0.2),
            make_result(head.id, "q2", 1.0),
        ]
    )
    await session.flush()

    body = (await get(app_with_session, f"/eval-runs/diff?base={base.id}&head={head.id}")).json()

    assert [item["item_id"] for item in body["regressed"]] == ["q1"]
    assert body["regressed"][0]["base_faithfulness"] == 1.0
    assert body["regressed"][0]["head_faithfulness"] == 0.2


async def test_diff_ignores_items_the_judge_did_not_score(session, app_with_session):
    base, head = make_run("base", 1.0), make_run("head", 0.5)
    session.add_all([base, head])
    await session.flush()
    # An escalated item has no faithfulness score; comparing it would raise.
    session.add_all([make_result(base.id, "q1", 1.0), make_result(head.id, "q1", None)])
    await session.flush()

    body = (await get(app_with_session, f"/eval-runs/diff?base={base.id}&head={head.id}")).json()

    assert body["regressed"] == []


async def test_missing_run_returns_404(session, app_with_session):
    assert (await get(app_with_session, "/eval-runs/999999")).status_code == 404


async def test_missing_trace_returns_404(session, app_with_session):
    assert (await get(app_with_session, "/traces/999999")).status_code == 404


async def test_trace_list_exposes_cost_and_retrieved_chunks(session, app_with_session):
    session.add(
        Trace(
            question="q",
            answer="a",
            escalated=False,
            reason=None,
            top_score=6.0,
            margin=1.0,
            retrieved=[{"chunk_id": 1, "source_path": "a.md", "score": 6.0}],
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.000003,
            model="gemini-2.0-flash",
            prompt_version="answer_v1",
            latency_ms=120,
        )
    )
    await session.flush()

    body = (await get(app_with_session, "/traces")).json()

    assert body[0]["cost_usd"] > 0
    assert body[0]["retrieved"][0]["source_path"] == "a.md"
