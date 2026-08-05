from httpx import ASGITransport, AsyncClient

from answer.main import app

SERVICE = {"Authorization": "Bearer test-service-token"}
OPERATOR = {"Authorization": "Bearer test-operator-token"}


async def request(method: str, path: str, headers: dict | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers or {}, json={})


async def test_answer_requires_a_service_credential():
    assert (await request("POST", "/answer")).status_code == 401


async def test_traces_requires_an_operator_credential():
    assert (await request("GET", "/traces")).status_code == 401


async def test_a_single_trace_requires_an_operator_credential():
    assert (await request("GET", "/traces/1")).status_code == 401


async def test_health_stays_public():
    assert (await request("GET", "/health")).status_code == 200


async def test_ask_stays_open_to_anonymous_callers():
    """The demo is open. A 401 here would mean the public surface had closed."""
    assert (await request("POST", "/ask")).status_code != 401


async def test_a_service_credential_does_not_open_the_traces_surface():
    assert (await request("GET", "/traces", SERVICE)).status_code == 401


async def test_an_operator_credential_does_not_open_the_answer_route():
    assert (await request("POST", "/answer", OPERATOR)).status_code == 401
