from httpx import ASGITransport, AsyncClient

from answer.main import app

SERVICE = {"Authorization": "Bearer test-service-token"}
OPERATOR = {"Authorization": "Bearer test-operator-token"}


async def request(method: str, path: str, headers: dict | None = None):
    # raise_app_exceptions=False because these tests assert what the *guard* layer does.
    # ASGITransport does not run the app's lifespan, so any route that reaches its
    # handler finds app.state.llm_client unset; that is a 500 we do not care about here,
    # and letting it propagate would mask the status code under test.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
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
