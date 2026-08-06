import asyncio

import httpx
import pytest_asyncio
import uvicorn
from doubles import build_upstream
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from gateway.proxy import clean_request_headers, forward
from gateway.routes import Route

ECHO = Route("GET", "/echo", "answer", None, timeout=5)
STREAM = Route("GET", "/slow-stream", "answer", None, timeout=5, stream=True)


@pytest_asyncio.fixture
async def upstream():
    return build_upstream()


@pytest_asyncio.fixture
async def gateway(upstream):
    """A one-route app that forwards through the fake upstream."""
    client = AsyncClient(transport=ASGITransport(app=upstream), base_url="http://upstream")
    app = FastAPI()

    async def handler(request: Request):
        route = STREAM if request.url.path == "/slow-stream" else ECHO
        return await forward(route, request, "http://upstream", client)

    app.add_api_route("/echo", handler, methods=["GET", "POST"])
    app.add_api_route("/slow-stream", handler, methods=["GET"])
    yield app
    await client.aclose()


async def call(app, method: str, path: str, **kwargs) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, **kwargs)


async def test_the_request_reaches_the_upstream_path(gateway):
    response = await call(gateway, "GET", "/echo")

    assert response.status_code == 200
    assert response.json()["path"] == "/echo"


async def test_the_query_string_survives(gateway):
    response = await call(gateway, "GET", "/echo?limit=3&q=x")

    assert "limit=3" in response.json()["query"]


async def test_the_body_survives(gateway):
    response = await call(gateway, "POST", "/echo", content=b'{"a":1}')

    assert response.json()["body"] == '{"a":1}'


async def test_the_callers_authorization_is_passed_through_unchanged(gateway, upstream):
    """The gateway must not swap in its own token: the upstream re-resolves the caller,
    and that is the whole defence-in-depth argument."""
    await call(gateway, "GET", "/echo", headers={"Authorization": "Bearer sess-abc"})

    assert upstream.state.seen_headers["authorization"] == "Bearer sess-abc"


async def test_a_client_supplied_forwarded_header_never_reaches_the_upstream(gateway, upstream):
    """Otherwise the gateway launders a header the upstream might one day trust."""
    await call(gateway, "GET", "/echo", headers={"X-Forwarded-For": "9.9.9.9"})

    assert "9.9.9.9" not in upstream.state.seen_headers.get("x-forwarded-for", "")


async def test_a_client_supplied_deflect_header_never_reaches_the_upstream(gateway, upstream):
    await call(gateway, "GET", "/echo", headers={"X-Deflect-Principal": "session:admin:1"})

    assert "x-deflect-principal" not in upstream.state.seen_headers


async def _serve(app) -> tuple[uvicorn.Server, asyncio.Task, str]:
    """Run an app on a real socket on an ephemeral port."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, f"http://127.0.0.1:{port}"


async def _stop(server: uvicorn.Server, task: asyncio.Task) -> None:
    server.should_exit = True
    await task


@pytest_asyncio.fixture
async def live_pair():
    """A real upstream and a real gateway, each on its own ephemeral port.

    ASGITransport cannot prove anything about buffering. It drives the whole ASGI app to
    completion and collects the body BEFORE send() returns, so a test that waits for a
    first frame before releasing the upstream deadlocks against the transport rather than
    against the gateway -- and would deadlock identically whether or not the gateway
    buffers, which makes it worthless as evidence.

    This is the same limitation that made the uvicorn forwarded-header defect untestable
    in-process: an in-process transport is not HTTP, and the two places this project
    genuinely depends on HTTP behaviour both need a socket.
    """
    upstream_app = build_upstream()
    upstream_server, upstream_task, upstream_url = await _serve(upstream_app)

    client = AsyncClient(base_url=upstream_url)
    gateway_app = FastAPI()

    async def handler(request: Request):
        return await forward(STREAM, request, upstream_url, client)

    gateway_app.add_api_route("/slow-stream", handler, methods=["GET"])
    gateway_server, gateway_task, gateway_url = await _serve(gateway_app)

    yield upstream_app, gateway_url

    await client.aclose()
    await _stop(gateway_server, gateway_task)
    await _stop(upstream_server, upstream_task)


async def test_a_streamed_response_is_not_buffered(live_pair):
    """The failure mode a naive proxy has by default, invisible until a user waits thirty
    seconds for a first token.

    The upstream emits one frame and then blocks until this test releases it. Receiving
    that frame therefore proves the gateway forwarded it without waiting for the body to
    finish -- and if the gateway buffered, this test would time out rather than pass.
    """
    upstream_app, gateway_url = live_pair

    async with AsyncClient(base_url=gateway_url, timeout=10) as c:
        async with c.stream("GET", "/slow-stream") as response:
            first = None
            async for chunk in response.aiter_bytes():
                first = chunk
                break

            assert first is not None and b"first" in first
            upstream_app.state.release.set()


def test_hop_by_hop_headers_are_dropped():
    """Relaying these breaks connection handling in ways that only show under load."""
    cleaned = clean_request_headers(
        {"connection": "keep-alive", "transfer-encoding": "chunked", "accept": "application/json"}
    )

    assert "connection" not in cleaned
    assert "transfer-encoding" not in cleaned
    assert cleaned["accept"] == "application/json"


def test_the_host_header_is_dropped():
    """Forwarding the gateway's Host would make the upstream build wrong absolute URLs."""
    cleaned = clean_request_headers({"host": "gateway.example.com", "accept": "*/*"})

    assert "host" not in cleaned


async def test_an_upstream_error_status_is_relayed_rather_than_replaced(upstream):
    """A 418 from a service is that service's answer. Turning it into a 502 would hide a
    real response behind a transport error."""
    client = AsyncClient(transport=ASGITransport(app=upstream), base_url="http://upstream")
    app = FastAPI()
    route = Route("GET", "/boom", "answer", None, timeout=5)

    async def handler(request: Request):
        return await forward(route, request, "http://upstream", client)

    app.add_api_route("/boom", handler, methods=["GET"])

    response = await call(app, "GET", "/boom")
    await client.aclose()

    assert response.status_code == 418


async def test_an_unreachable_upstream_is_a_502():
    """Port 1 has nothing listening, so the httpx error and its translation are both real
    rather than mocked."""
    client = AsyncClient(base_url="http://127.0.0.1:1")
    app = FastAPI()
    route = Route("GET", "/echo", "answer", None, timeout=2)

    async def handler(request: Request):
        return await forward(route, request, "http://127.0.0.1:1", client)

    app.add_api_route("/echo", handler, methods=["GET"])

    response = await call(app, "GET", "/echo")
    await client.aclose()

    assert response.status_code == 502
