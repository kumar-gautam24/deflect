from httpx import ASGITransport, AsyncClient

from evals.main import app

OPERATOR = {"Authorization": "Bearer test-operator-token"}
SERVICE = {"Authorization": "Bearer test-service-token"}


async def request(method: str, path: str, headers: dict | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers or {}, json={})


async def test_creating_a_run_requires_an_operator_credential():
    """The most expensive operation in the system was the only unguarded write."""
    assert (await request("POST", "/runs")).status_code == 401


async def test_a_service_credential_does_not_start_an_eval_run():
    assert (await request("POST", "/runs", SERVICE)).status_code == 401


async def test_health_stays_public():
    assert (await request("GET", "/health")).status_code == 200


async def test_listing_runs_stays_public_for_the_dashboard():
    assert (await request("GET", "/eval-runs")).status_code == 200


async def test_reading_one_run_stays_public():
    """404 rather than 401: the route is reachable, the run merely does not exist."""
    assert (await request("GET", "/eval-runs/999999")).status_code == 404


async def test_diffing_runs_stays_public():
    assert (await request("GET", "/eval-runs/diff?base=1&head=2")).status_code != 401
