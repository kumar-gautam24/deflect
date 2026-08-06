import httpx
import pytest
import pytest_asyncio
from helpers import make_result, make_run
from httpx import ASGITransport, AsyncClient

from evals.db import get_session
from evals.main import _smoke_subset, build_answer_client, build_judge
from evals.main import app as fastapi_app


@pytest_asyncio.fixture
async def app(session):
    session.commit = session.flush
    fastapi_app.dependency_overrides[get_session] = lambda: session
    fastapi_app.dependency_overrides[build_judge] = lambda: None
    fastapi_app.dependency_overrides[build_answer_client] = lambda: None
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


async def get(app, path: str) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_run_list_is_newest_first(session, app):
    session.add_all([make_run("order-old", 0.9), make_run("order-new", 0.8)])
    await session.flush()

    body = (await get(app, "/eval-runs")).json()

    # Filtered to this test's own two runs rather than sliced off the front: ordering is
    # the claim, and a run committed outside this test would occupy the first slots
    # without saying anything about whether the sort works.
    ours = [r["git_sha"] for r in body if r["git_sha"] in {"order-old", "order-new"}]
    assert ours == ["order-new", "order-old"]


async def test_run_detail_includes_per_item_results(session, app):
    run = make_run("sha", 1.0)
    session.add(run)
    await session.flush()
    session.add(make_result(run.id, "q1", 1.0))
    await session.flush()

    body = (await get(app, f"/eval-runs/{run.id}")).json()

    assert body["metrics"]["faithfulness"] == 1.0
    assert [r["item_id"] for r in body["results"]] == ["q1"]


async def test_diff_flags_items_whose_score_dropped(session, app):
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

    body = (await get(app, f"/eval-runs/diff?base={base.id}&head={head.id}")).json()

    assert [i["item_id"] for i in body["regressed"]] == ["q1"]
    assert body["regressed"][0]["base_faithfulness"] == 1.0
    assert body["regressed"][0]["head_faithfulness"] == 0.2


async def test_diff_ignores_items_the_judge_did_not_score(session, app):
    base, head = make_run("base", 1.0), make_run("head", 0.5)
    session.add_all([base, head])
    await session.flush()
    # An escalated item has no faithfulness score; comparing it would raise.
    session.add_all([make_result(base.id, "q1", 1.0), make_result(head.id, "q1", None)])
    await session.flush()

    body = (await get(app, f"/eval-runs/diff?base={base.id}&head={head.id}")).json()

    assert body["regressed"] == []


async def test_diff_route_is_not_captured_by_the_run_id_parameter(session, app):
    """`/eval-runs/diff` must be declared before `/eval-runs/{run_id}`.

    Reversed, FastAPI matches `diff` as a run id and fails to parse it as an int,
    returning 422 instead of a diff.
    """
    base, head = make_run("base", 1.0), make_run("head", 1.0)
    session.add_all([base, head])
    await session.flush()

    response = await get(app, f"/eval-runs/diff?base={base.id}&head={head.id}")

    assert response.status_code == 200
    assert "regressed" in response.json()


async def test_missing_run_returns_404(session, app):
    assert (await get(app, "/eval-runs/999999")).status_code == 404


@pytest.mark.parametrize("limit", [4, 8, 12])
def test_smoke_subset_always_includes_refusals(limit):
    from evals.dataset import GoldenItem

    items = [
        GoldenItem(f"q{i}", "q", "a", ["deps.md"], False) for i in range(20)
    ] + [GoldenItem(f"u{i}", "q", "a", [], True) for i in range(5)]

    subset = _smoke_subset(items, limit)

    assert len(subset) == limit
    assert any(i.should_escalate for i in subset), "a limited run must still test refusal"
    assert any(not i.should_escalate for i in subset)


def test_smoke_subset_returns_everything_when_unlimited():
    from evals.dataset import GoldenItem

    items = [GoldenItem("q1", "q", "a", ["deps.md"], False)]

    assert _smoke_subset(items, None) == items
    assert _smoke_subset(items, 50) == items
