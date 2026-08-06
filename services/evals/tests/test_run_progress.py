from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from evals.main import app
from evals.models import EvalItemJob, EvalRun


async def get(path: str, headers=None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


async def _running_run(session, done: int, total: int) -> EvalRun:
    run = EvalRun(
        git_sha="abc", prompt_version="", judge_version="v1", model="m",
        retrieval_config={}, thresholds={}, item_count=0, metrics={},
        items_total=total, status="running",
    )
    session.add(run)
    await session.flush()
    for n in range(total):
        session.add(
            EvalItemJob(run_id=run.id, item_id=f"q{n}", status="done" if n < done else "queued")
        )
    await session.flush()
    return run


async def test_a_running_run_reports_its_progress(session, queue):
    run = await _running_run(session, done=3, total=10)

    body = (await get(f"/eval-runs/{run.id}")).json()

    assert body["status"] == "running"
    assert body["progress"] == {"finished": 3, "total": 10}


async def test_progress_counts_failed_items_as_finished(session, queue):
    """Otherwise progress would stick below 100% on a run that is genuinely over."""
    run = await _running_run(session, done=0, total=2)
    jobs = (
        await session.execute(select(EvalItemJob).where(EvalItemJob.run_id == run.id))
    ).scalars().all()
    jobs[0].status = "failed"
    jobs[1].status = "done"
    await session.flush()

    assert (await get(f"/eval-runs/{run.id}")).json()["progress"]["finished"] == 2


async def test_the_run_list_carries_status(session, queue):
    await _running_run(session, done=1, total=4)

    assert (await get("/eval-runs")).json()[0]["status"] == "running"


async def test_a_run_stays_publicly_readable(session, queue):
    """Watching an eval run is the most interesting thing this project does; putting it
    behind a credential would hide the demo."""
    run = await _running_run(session, done=1, total=2)

    assert (await get(f"/eval-runs/{run.id}")).status_code == 200


async def test_the_event_stream_404s_for_an_unknown_run(session, queue):
    """Resolved before streaming starts, so this returns without opening a stream at
    all -- the same contract GET /eval-runs/{run_id} has."""
    assert (await get("/eval-runs/999999/events")).status_code == 404


async def test_a_finished_run_closes_its_stream(session, queue):
    """The loop must terminate on a terminal status, or every viewer of a completed run
    holds a connection open forever.

    Only the terminal case is exercised over HTTP. ASGITransport drives the app to
    completion rather than truly streaming, so a request against a still-running run
    would never return -- the test would hang instead of failing, which is worse than
    not testing it. The disconnect path has the same limitation and is covered by
    inspection.
    """
    run = await _running_run(session, done=2, total=2)
    run.status = "complete"
    await session.flush()

    response = await get(f"/eval-runs/{run.id}/events")

    assert response.status_code == 200
    assert '"status": "complete"' in response.text
