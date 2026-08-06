from deflect_common.jobs import EVAL_ITEM_STREAM
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from evals.main import app
from evals.models import EvalItemJob, EvalRun

OPERATOR = {"Authorization": "Bearer test-operator-token"}


async def post(path: str, headers=None, body=None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, headers=headers or {}, json=body or {})


async def test_a_run_is_accepted_and_returns_its_id(session, queue, dataset_of_three):
    response = await post("/runs", OPERATOR, {"limit": None})

    assert response.status_code == 202
    assert isinstance(response.json()["run_id"], int)


async def test_the_run_starts_running_with_a_target(session, queue, dataset_of_three):
    run_id = (await post("/runs", OPERATOR, {"limit": None})).json()["run_id"]

    run = await session.get(EvalRun, run_id)

    assert run.status == "running"
    assert run.items_total == 3
    assert run.item_count == 0


async def test_one_job_row_exists_per_item(session, queue, dataset_of_three):
    run_id = (await post("/runs", OPERATOR, {"limit": None})).json()["run_id"]

    jobs = (
        await session.execute(select(EvalItemJob).where(EvalItemJob.run_id == run_id))
    ).scalars().all()

    assert sorted(j.item_id for j in jobs) == ["q1", "q2", "q3"]
    assert all(j.status == "queued" for j in jobs)


async def test_one_message_is_enqueued_per_item(session, queue, dataset_of_three):
    await post("/runs", OPERATOR, {"limit": None})

    await queue.ensure_group(EVAL_ITEM_STREAM)
    claimed = await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=100)

    assert len(claimed) == 3


async def test_starting_a_run_still_requires_an_operator_credential(
    session, queue, dataset_of_three
):
    assert (await post("/runs", None, {"limit": None})).status_code == 401


async def test_an_empty_dataset_is_rejected_before_a_run_is_created(
    session, queue, tmp_path, monkeypatch
):
    """A run with no items could never finalise: the fan-in waits for a count it will
    never reach."""
    from evals.config import get_settings

    empty = tmp_path / "empty.yaml"
    empty.write_text("[]\n")
    monkeypatch.setenv("DATASET_PATH", str(empty))
    get_settings.cache_clear()
    # A high-water mark rather than an empty-table check: "this request created no run" is
    # the claim, and asserting the table is empty also asserts something about rows this
    # test never wrote.
    highest = (await session.execute(select(func.max(EvalRun.id)))).scalar() or 0
    try:
        assert (await post("/runs", OPERATOR, {"limit": None})).status_code == 422
        assert (
            await session.execute(select(EvalRun).where(EvalRun.id > highest))
        ).scalars().all() == []
    finally:
        get_settings.cache_clear()
