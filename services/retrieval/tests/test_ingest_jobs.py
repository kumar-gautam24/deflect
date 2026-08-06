from deflect_common.jobs import INGEST_STREAM
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from retrieval.main import app
from retrieval.models import IngestJob

OPERATOR = {"Authorization": "Bearer test-operator-token"}


async def request(method: str, path: str, headers=None, body=None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers or {}, json=body or {})


async def test_ingest_accepts_and_returns_a_job_id(session, queue):
    body = {"root": "/corpus", "commit_sha": "abc"}

    response = await request("POST", "/ingest", OPERATOR, body)

    assert response.status_code == 202
    assert isinstance(response.json()["job_id"], int)


async def test_ingest_records_the_job_before_enqueueing(session, queue):
    """A job that exists in Redis must always have a row behind it, or the worker has
    work referencing nothing."""
    await request("POST", "/ingest", OPERATOR, {"root": "/corpus", "commit_sha": "abc"})

    job = (await session.execute(select(IngestJob))).scalars().one()

    assert job.status == "queued"
    assert job.root == "/corpus"
    assert await queue.pending_count(INGEST_STREAM) == 0  # enqueued, not yet claimed


async def test_ingest_confinement_still_applies_before_a_job_is_created(session, queue):
    """Rejecting the path after creating a job would leave a queued job that can only
    ever fail."""
    response = await request("POST", "/ingest", OPERATOR, {"root": "/etc", "commit_sha": "x"})

    assert response.status_code == 400
    assert (await session.execute(select(IngestJob))).scalars().all() == []


async def test_ingest_still_requires_an_operator_credential(session, queue):
    assert (await request("POST", "/ingest", None, {"root": "/corpus"})).status_code == 401


async def test_job_status_is_readable(session, queue):
    created = await request("POST", "/ingest", OPERATOR, {"root": "/corpus", "commit_sha": "a"})
    job_id = created.json()["job_id"]

    response = await request("GET", f"/jobs/{job_id}", OPERATOR)

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


async def test_job_status_requires_an_operator_credential(session, queue):
    assert (await request("GET", "/jobs/1")).status_code == 401


async def test_an_unknown_job_is_a_404(session, queue):
    assert (await request("GET", "/jobs/999999", OPERATOR)).status_code == 404


async def test_the_event_stream_404s_for_an_unknown_job(session, queue):
    """The sibling route 404s. A 200 carrying an error frame would make the two disagree
    about what missing looks like."""
    assert (await request("GET", "/jobs/999999/events", OPERATOR)).status_code == 404


async def test_the_event_stream_requires_an_operator_credential(session, queue):
    assert (await request("GET", "/jobs/1/events")).status_code == 401
