# API Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put one public front door in front of Deflect's four services, so edge policy — rate limiting, routing, docs exposure, correlation — is enforced in one readable place instead of four.

**Architecture:** A fifth FastAPI service, `services/gateway`, owning no data and no database. A declarative route table maps path and method to an upstream, a required principal, and a timeout; a streaming httpx proxy carries the request through with the caller's original credential untouched, and every upstream service keeps its own `principal_guard`. Rate limiting moves to the gateway and becomes a leaky bucket keyed by the client address, which at an edge is the *rightmost* `X-Forwarded-For` entry rather than the leftmost.

**Tech Stack:** Python 3.12, FastAPI, httpx (streaming), Redis, gunicorn with uvicorn workers, pytest with `asyncio_mode = "auto"`, ruff.

**Spec:** `docs/superpowers/specs/2026-08-07-api-gateway-design.md`

**Branch:** `api-gateway`, off `main` at `2c17f60`.

## Global Constraints

- Python `>=3.12`. Ruff `line-length = 100`, rules `["E", "F", "I", "UP", "B"]`. Every task ends ruff-clean.
- **Commit messages carry no attribution trailers.** No `Co-Authored-By`, no `Generated with`. Zero exist in this repository's history. Lowercase sentence subject describing the behaviour change; body explains *why* and what was rejected.
- **`packages/common` receives credentials and connection strings as arguments, never from a settings singleton.** It imports nothing from any service.
- Something two or more services need goes in `packages/common`; something one service needs stays in that service.
- **The gateway owns no database and gets no migrations.** Its only state is Redis.
- **Every test runs with no Redis, no network and no provider key.** Every Redis-backed class gets an in-memory twin, exactly as `SessionStore` and `JobQueue` already have.
- **`--forwarded-allow-ips ""` stays on all five services, gateway included.** No task may remove it. The gateway parses `X-Forwarded-For` itself.
- **Upstream services keep `principal_guard` unchanged.** No task deletes or weakens an authorisation check in `services/{retrieval,answer,evals,auth}`. Only *rate limits* move.
- **`OPERATOR_TOKEN` must still open every route it opens today.** CI depends on it.
- **Never log a token, a session value, or a password.** A user id is the only identifier that may appear.
- Comments explain the reasoning and the rejected alternative, not the mechanics.
- **Never run `docker compose down -v`** — the databases hold an ingested corpus and completed eval runs.
- Six suites must stay green at every task boundary: retrieval 70, answer 54, evals 82+1 skip, auth 42, common 94, web 21. The gateway adds a seventh.

## Deviation from the spec — read before Task 3

The spec says `/docs`, `/redoc` and `/openapi.json` are "appended to the table only when `ENV` is not `production`". Implemented literally that means proxying each upstream's docs through the gateway, which needs a disambiguating path prefix per service and produces four sets of docs describing routes the gateway does not expose the same way.

**This plan does something simpler and stricter:** upstream docs are never routed at all, and the gateway's own `/docs` follows the identical `ENV` rule every other service already uses. Once the services are private their docs are unreachable, which is the outcome the spec wanted. The per-service `ENV` checks stay exactly where they are as defence in depth.

Flag this to the plan's author if it is not acceptable; everything else follows the spec as written.

## File Structure

**Created**

| path | responsibility |
| --- | --- |
| `services/gateway/src/gateway/routes.py` | The route table as data. Imports nothing but `dataclasses`. |
| `services/gateway/src/gateway/policy.py` | Limits, timeouts and circuit-breaker numbers, each with its reason. |
| `services/gateway/src/gateway/config.py` | Settings: upstream URLs, tokens, Redis URL, `ENV`. |
| `services/gateway/src/gateway/proxy.py` | Streaming passthrough, header hygiene, 502/504 translation. |
| `services/gateway/src/gateway/breaker.py` | Per-upstream circuit breaker. |
| `services/gateway/src/gateway/main.py` | App assembly: registers the table, wires limiter and proxy. |
| `services/gateway/{Dockerfile,pyproject.toml}` | Build and dependencies. |
| `services/gateway/tests/` | `conftest.py`, `doubles.py`, and one test module per unit below. |

**Modified**

| path | change |
| --- | --- |
| `packages/common/src/deflect_common/ratelimit.py` | Adds `edge_address`, `Decision`, `Limiter`, `InMemoryLeakyBucket`, `RedisLeakyBucket`. `client_address` and `SlidingWindowLimiter` stay. |
| `services/answer/src/answer/main.py` | Drops `_ask_limiter` and its 429; keeps the daily cap. |
| `services/auth/src/auth/main.py` | Drops `_login_limiter` and its 429. |
| `apps/web/app/api/ask/route.ts`, `apps/web/lib/api.ts` | Point at the gateway. |
| `docker-compose.yml`, `render.yaml`, `.env.example`, `README.md` | Configuration and documentation. |

## The route table this plan implements

Copied from the spec so no task has to leave this document.

| method | path | upstream | principal | timeout | stream |
| --- | --- | --- | --- | --- | --- |
| POST | `/ask` | answer | — | 30 | yes |
| POST | `/auth/login` | auth | — | 10 | |
| POST | `/auth/logout` | auth | session | 10 | |
| POST | `/auth/logout-all` | auth | session | 10 | |
| GET | `/auth/me` | auth | session | 10 | |
| GET | `/traces` | answer | viewer | 15 | |
| GET | `/traces/{trace_id}` | answer | viewer | 15 | |
| POST | `/search` | retrieval | service | 15 | |
| GET | `/documents` | retrieval | service | 15 | |
| POST | `/ingest` | retrieval | operator | 15 | |
| GET | `/jobs/{job_id}` | retrieval | operator | 15 | |
| GET | `/jobs/{job_id}/events` | retrieval | operator | 30 | yes |
| POST | `/runs` | evals | operator | 15 | |
| GET | `/eval-runs` | evals | — | 15 | |
| GET | `/eval-runs/diff` | evals | — | 15 | |
| GET | `/eval-runs/{run_id}` | evals | — | 15 | |
| GET | `/eval-runs/{run_id}/events` | evals | — | 30 | yes |

`principal = None` means public. `/metrics` appears nowhere and is therefore unroutable.

**Principal semantics, unchanged from `packages/common`:** `service` is satisfied by `SERVICE_TOKEN` only; `operator` by `OPERATOR_TOKEN` or an `admin` session; `viewer` by `OPERATOR_TOKEN` or any valid session. `session` is new to the gateway and means *any valid session, and no token* — it exists because `/auth/logout` needs a session row to revoke and a token has none.

---

## Task 1: The edge address rule

**Files:**
- Modify: `packages/common/src/deflect_common/ratelimit.py`
- Modify: `packages/common/tests/test_ratelimit.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `edge_address(request: Request, trusted_hops: int = 1) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `packages/common/tests/test_ratelimit.py`:

```python
from deflect_common.ratelimit import edge_address


def _request(headers: dict[str, str], peer: str | None = "10.0.0.1") -> Request:
    """A Starlette request with the given headers and peer, without a server."""
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "client": (peer, 1234) if peer else None,
    }
    return Request(scope)


def test_the_edge_takes_the_entry_its_own_proxy_appended():
    """Render appends the real client to whatever the caller sent, so the LAST entry is
    the only one the gateway's own proxy wrote."""
    request = _request({"X-Forwarded-For": "203.0.113.7"})

    assert edge_address(request) == "203.0.113.7"


def test_a_spoofed_leading_entry_is_ignored():
    """The whole point. A caller who sends their own X-Forwarded-For would otherwise mint
    a fresh rate-limit key per request and make the limit decorative."""
    request = _request({"X-Forwarded-For": "9.9.9.9, 203.0.113.7"})

    assert edge_address(request) == "203.0.113.7"


def test_many_spoofed_entries_are_still_ignored():
    request = _request({"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 3.3.3.3, 203.0.113.7"})

    assert edge_address(request) == "203.0.113.7"


def test_two_trusted_hops_takes_the_second_from_the_right():
    """A deployment behind two proxies needs a different number, not different code."""
    request = _request({"X-Forwarded-For": "9.9.9.9, 203.0.113.7, 10.0.0.9"})

    assert edge_address(request, trusted_hops=2) == "203.0.113.7"


def test_no_forwarded_header_falls_back_to_the_peer():
    """Direct connections happen in development, and must not all share one key."""
    request = _request({}, peer="192.168.1.5")

    assert edge_address(request) == "192.168.1.5"


def test_fewer_entries_than_trusted_hops_falls_back_to_the_peer():
    """A header too short to contain a trusted entry is not evidence of anything, so it
    must not be believed."""
    request = _request({"X-Forwarded-For": "9.9.9.9"}, peer="192.168.1.5")

    assert edge_address(request, trusted_hops=2) == "192.168.1.5"


def test_no_peer_and_no_header_is_a_single_known_bucket():
    request = _request({}, peer=None)

    assert edge_address(request) == "unknown"


def test_whitespace_and_empty_entries_do_not_shift_the_answer():
    request = _request({"X-Forwarded-For": "9.9.9.9 , , 203.0.113.7 "})

    assert edge_address(request) == "203.0.113.7"
```

Add `from fastapi import Request` to the file's imports if it is not already present.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd packages/common && uv run pytest tests/test_ratelimit.py -k edge -v`
Expected: FAIL — `ImportError: cannot import name 'edge_address'`

- [ ] **Step 3: Implement it**

Append to `packages/common/src/deflect_common/ratelimit.py`:

```python
def edge_address(request: Request, trusted_hops: int = 1) -> str:
    """The client address, for a service that IS the edge.

    `client_address` takes the leftmost X-Forwarded-For entry, which is right when the only
    forwarder is trusted and overwrites the header -- the web BFF does exactly that. It is
    wrong here. A load balancer in front of a public edge APPENDS the real client to
    whatever the caller sent, so the leftmost entry is attacker-supplied and the rightmost
    is the one our own proxy wrote.

    Two functions rather than another boolean on one: the rules are genuinely different,
    and a flag would invite a future caller to pick the wrong one and never find out.

    `trusted_hops` is explicit because the right entry is n-from-the-right. A deployment
    behind two proxies needs a different number, not different code. If the header is too
    short to hold that many, it is not evidence of anything and the peer is used instead.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]

    if len(entries) >= trusted_hops >= 1:
        return entries[-trusted_hops]

    return request.client.host if request.client else "unknown"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd packages/common && uv run pytest tests/test_ratelimit.py -v && uv run ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add packages/common/src/deflect_common/ratelimit.py packages/common/tests/test_ratelimit.py
git commit -m "read the forwarded address the way an edge has to

client_address takes the leftmost X-Forwarded-For entry, which is correct while
the only trusted forwarder is the web BFF -- it computes the address itself and
overwrites the header rather than relaying the visitor's.

A public edge behind a load balancer inverts that. The balancer appends the real
client to whatever the caller sent, so leftmost is attacker-supplied: a caller
who sets their own header would mint a fresh rate-limit key per request and the
limit would be decorative, which is the uvicorn defect reached by another route.

A separate function rather than another boolean, because the two rules are
genuinely different and a flag invites picking the wrong one silently."
```

---

## Task 2: Gateway skeleton and the route table

**Files:**
- Create: `services/gateway/pyproject.toml`, `services/gateway/src/gateway/__init__.py`, `services/gateway/src/gateway/routes.py`, `services/gateway/src/gateway/policy.py`, `services/gateway/src/gateway/config.py`, `services/gateway/src/gateway/main.py`
- Create: `services/gateway/tests/conftest.py`, `services/gateway/tests/test_routes.py`

**Interfaces:**
- Consumes: nothing from Task 1 yet.
- Produces: `Route` dataclass with fields `method: str`, `path: str`, `upstream: str`, `principal: str | None`, `timeout: float`, `stream: bool`, `limit: str | None`; module constant `ROUTES: tuple[Route, ...]`; `Policy` class in `policy.py`; `get_settings() -> Settings` in `config.py`; `app` in `main.py`.

- [ ] **Step 1: Create the package scaffolding**

`services/gateway/pyproject.toml`:

```toml
[project]
name = "deflect-gateway"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "deflect-common",
    "fastapi>=0.115",
    "gunicorn>=23.0",
    "uvicorn[standard]>=0.32",
    "pydantic-settings>=2.6",
    "httpx>=0.28",
    "redis>=5.2",
]

[dependency-groups]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "pytest-env>=1.1", "ruff>=0.8"]

[tool.uv.sources]
deflect-common = { path = "../../packages/common", editable = true }

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"

[tool.ruff]
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/gateway"]
```

There is no `env = [...]` line because the gateway has no database.

Create an empty `services/gateway/src/gateway/__init__.py`.

- [ ] **Step 2: Write the failing test**

`services/gateway/tests/test_routes.py`:

```python
import pytest

from gateway.routes import ROUTES, Route

VALID_PRINCIPALS = {None, "service", "operator", "viewer", "session"}


def test_every_route_names_a_known_principal():
    """A typo here would build a guard nothing satisfies, and the route would 401 for
    everyone with nothing explaining why."""
    unknown = {r.principal for r in ROUTES} - VALID_PRINCIPALS

    assert unknown == set()


def test_every_route_names_a_known_upstream():
    unknown = {r.upstream for r in ROUTES} - {"retrieval", "answer", "evals", "auth"}

    assert unknown == set()


def test_no_route_exposes_metrics():
    """/metrics is unroutable rather than protected: a path absent from the table cannot
    be reached even if a guard is later mis-wired."""
    assert not any("/metrics" in r.path for r in ROUTES)


def test_no_route_exposes_interactive_docs():
    assert not any(r.path.startswith(("/docs", "/redoc", "/openapi")) for r in ROUTES)


def test_no_two_routes_share_a_method_and_path():
    pairs = [(r.method, r.path) for r in ROUTES]

    assert len(pairs) == len(set(pairs))


def test_every_route_has_a_positive_timeout():
    assert all(r.timeout > 0 for r in ROUTES)


@pytest.mark.parametrize(
    ("method", "path", "principal"),
    [
        ("POST", "/ask", None),
        ("POST", "/auth/login", None),
        ("GET", "/auth/me", "session"),
        ("GET", "/traces", "viewer"),
        ("POST", "/search", "service"),
        ("POST", "/ingest", "operator"),
        ("POST", "/runs", "operator"),
        ("GET", "/eval-runs", None),
    ],
)
def test_the_table_matches_the_spec(method, path, principal):
    """Pinned individually so a careless edit to the table fails a named test rather than
    silently changing who can reach what."""
    route = next(r for r in ROUTES if r.method == method and r.path == path)

    assert route.principal == principal


def test_streaming_routes_are_the_ones_that_stream():
    streaming = {(r.method, r.path) for r in ROUTES if r.stream}

    assert streaming == {
        ("POST", "/ask"),
        ("GET", "/jobs/{job_id}/events"),
        ("GET", "/eval-runs/{run_id}/events"),
    }


def test_a_route_is_frozen():
    """The table is data. Mutating it at runtime would make the security posture depend on
    import order."""
    with pytest.raises(AttributeError):
        ROUTES[0].principal = "service"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd services/gateway && uv sync && uv run pytest tests/test_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.routes'`

- [ ] **Step 4: Write `routes.py`**

```python
"""The route table.

This module is the artifact a reviewer reads to answer "what is exposed, and to whom".
It deliberately imports nothing but the standard library: the table should be legible
without following an import into an HTTP client or a settings object.

A path that is absent is unroutable, which is a stronger guarantee than a path that is
guarded. /metrics and the interactive docs are absent for exactly that reason.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    """One public path, and everything the gateway needs to know to serve it.

    `principal` is the requirement a caller must satisfy, resolved by
    deflect_common.auth; None means public. `limit` names a limiter in policy.py rather
    than holding numbers, so the table stays about routing and the policy module stays
    about values.
    """

    method: str
    path: str
    upstream: str
    principal: str | None = None
    timeout: float = 15.0
    stream: bool = False
    limit: str | None = None


ROUTES: tuple[Route, ...] = (
    Route("POST", "/ask", "answer", None, timeout=30, stream=True, limit="ask"),
    Route("POST", "/auth/login", "auth", None, timeout=10, limit="login"),
    Route("POST", "/auth/logout", "auth", "session", timeout=10),
    Route("POST", "/auth/logout-all", "auth", "session", timeout=10),
    Route("GET", "/auth/me", "auth", "session", timeout=10),
    Route("GET", "/traces", "answer", "viewer"),
    Route("GET", "/traces/{trace_id}", "answer", "viewer"),
    Route("POST", "/search", "retrieval", "service"),
    Route("GET", "/documents", "retrieval", "service"),
    Route("POST", "/ingest", "retrieval", "operator"),
    Route("GET", "/jobs/{job_id}", "retrieval", "operator"),
    Route("GET", "/jobs/{job_id}/events", "retrieval", "operator", timeout=30, stream=True),
    Route("POST", "/runs", "evals", "operator"),
    Route("GET", "/eval-runs", "evals", None),
    Route("GET", "/eval-runs/diff", "evals", None),
    Route("GET", "/eval-runs/{run_id}", "evals", None),
    Route("GET", "/eval-runs/{run_id}/events", "evals", None, timeout=30, stream=True),
)
```

- [ ] **Step 5: Write `policy.py`**

```python
"""Gateway constants in one place, each with the reason it has that value.

The same reasoning as auth/policy.py: a number without its justification is a number
nobody can safely change six months later.
"""


class Policy:
    # Carried unchanged from the answer service, so moving the limit does not also
    # change how much traffic an hour permits. Burst is new -- see below.
    ASK_PER_HOUR = 20
    # Five questions back to back is someone trying the demo, not abusing it. The old
    # sliding-window log allowed all twenty at once, so this is strictly smoother.
    ASK_BURST = 5

    # Carried unchanged from auth. Sized so an attacker filling this bucket cannot also
    # stop a legitimate admin logging in -- which is why it is not lower.
    LOGIN_PER_HOUR = 60
    LOGIN_BURST = 10

    WINDOW_SECONDS = 3600

    # Connect and write are short because a healthy upstream on the same private network
    # answers in milliseconds; a slow one is a failure, not a slow success. The read
    # timeout is per-route, since /ask legitimately takes far longer than /traces.
    CONNECT_TIMEOUT = 5.0
    WRITE_TIMEOUT = 5.0

    # Five consecutive failures is a pattern rather than a blip. Thirty seconds is long
    # enough for a restart to finish and short enough that recovery is not noticed as an
    # outage of its own. Starting points, chosen rather than defaulted.
    BREAKER_FAILURES = 5
    BREAKER_COOLDOWN_SECONDS = 30
```

- [ ] **Step 6: Write `config.py`**

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    retrieval_url: str = "http://localhost:8001"
    answer_url: str = "http://localhost:8002"
    evals_url: str = "http://localhost:8003"
    auth_url: str = "http://localhost:8004"

    redis_url: str = "redis://localhost:6379/0"

    service_token: str = ""
    operator_token: str = ""

    # How many proxies sit in front of this process. One on Render; zero locally, where
    # the fallback to the peer address is the right answer anyway.
    trusted_proxy_hops: int = 1

    # production disables the interactive API docs.
    env: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 7: Write a minimal `main.py`**

```python
from deflect_common.logging import configure_logging
from deflect_common.observability import RequestIdMiddleware, metrics_response
from fastapi import APIRouter, Depends, FastAPI, Response

from gateway.config import get_settings

# Interactive docs are an inventory of the attack surface. The rule is identical to every
# other service's -- the gateway does not get an exemption for being the front door.
_docs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if get_settings().env == "production"
    else {}
)
app = FastAPI(title="Deflect gateway", **_docs)
configure_logging()
app.add_middleware(RequestIdMiddleware)
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: this process is answering. Deliberately touches no dependency."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness: this process can route.

    It deliberately does not probe its upstreams. A gateway that reports unready because
    one service is sick turns a single outage into a total one, which is the opposite of
    what an edge is for -- the circuit breaker handles a sick upstream per-route.
    """
    return {"status": "ok"}


app.include_router(router)
```

`metrics_response`, `Depends` and `Response` are imported here because Task 5 adds the guarded `/metrics` route that uses them. If ruff flags them as unused at this task, remove them and re-add in Task 5 rather than suppressing the warning.

- [ ] **Step 8: Write `conftest.py`**

```python
import os

os.environ["SERVICE_TOKEN"] = "test-service-token"
os.environ["OPERATOR_TOKEN"] = "test-operator-token"
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `cd services/gateway && uv run pytest -v && uv run ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 10: Commit**

```bash
git add services/gateway
git commit -m "add a gateway service whose route table is the readable artifact

The table is data in a module that imports nothing, because it is what a
reviewer reads to answer 'what is exposed, and to whom'. Four services'
decorators cannot be read that way.

A path absent from the table is unroutable, which is stronger than a path that
is guarded: /metrics and the interactive docs are absent rather than protected,
so a later mis-wired guard cannot expose them.

No database and no migrations. The gateway owns no data, and giving it a
database to satisfy a pattern would invert the rule rather than follow it."
```

---

## Task 3: The streaming proxy

**Files:**
- Create: `services/gateway/src/gateway/proxy.py`, `services/gateway/tests/doubles.py`, `services/gateway/tests/test_proxy.py`

**Interfaces:**
- Consumes: `Route` from Task 2.
- Produces: `async def forward(route: Route, request: Request, base_url: str, client: httpx.AsyncClient) -> Response`; `clean_request_headers(headers) -> dict[str, str]`.

- [ ] **Step 1: Write the fake upstream**

`services/gateway/tests/doubles.py`:

```python
"""A fake upstream, served in-process.

The gateway is given an httpx client whose transport is this app, so proxy behaviour is
exercised for real -- routing, headers, status, streaming -- without a socket.
"""

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def build_upstream() -> FastAPI:
    app = FastAPI()
    app.state.seen_headers = {}
    app.state.seen_paths = []
    app.state.release = asyncio.Event()

    @app.middleware("http")
    async def record(request: Request, call_next):
        """Record EVERY request, not only the ones this double has a route for.

        Recording inside a handler can only ever see paths the double defines, so a test
        asserting "the upstream was never called" would pass identically when the upstream
        WAS called on a path it happens not to serve -- which is exactly the regression
        those tests exist to catch.
        """
        app.state.seen_paths.append(request.url.path)
        app.state.seen_headers = dict(request.headers)
        return await call_next(request)

    @app.api_route("/echo", methods=["GET", "POST"])
    async def echo(request: Request) -> JSONResponse:
        return JSONResponse(
            {"path": request.url.path, "query": str(request.url.query), "body": (await request.body()).decode()}
        )

    @app.get("/slow-stream")
    async def slow_stream() -> StreamingResponse:
        """Emits one frame, then blocks until the test releases it.

        Gating on an event rather than sleeping is what makes the no-buffering test
        deterministic: if the gateway buffered, the first frame could not arrive before
        the second is even produced.
        """

        async def frames():
            yield b"data: first\n\n"
            await app.state.release.wait()
            yield b"data: second\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    @app.get("/boom")
    async def boom() -> JSONResponse:
        return JSONResponse({"detail": "upstream said no"}, status_code=418)

    return app
```

- [ ] **Step 2: Write the failing tests**

`services/gateway/tests/test_proxy.py`:

```python
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
```

The two upstream-failure tests need their own app rather than the shared `gateway` fixture, so they are added in Step 5 once `forward` exists. Everything above uses the fixture.

**Note the split, and do not collapse it.** Every test except the streaming one runs in-process through `ASGITransport`, which is fast and sufficient for headers, status and body. The streaming test alone runs over real sockets, because `ASGITransport` drives the whole app to completion before `send()` returns — an in-process no-buffering test deadlocks against the transport and proves nothing either way. Verified empirically against httpx 0.28.1 before this plan was written.

- [ ] **Step 3: Run to verify failure**

Run: `cd services/gateway && uv run pytest tests/test_proxy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.proxy'`

- [ ] **Step 4: Implement `proxy.py`**

```python
"""Carrying a request to an upstream and its response back.

The response is streamed rather than read. A gateway that buffers turns a token-by-token
answer into a thirty-second wait, and the failure is invisible in any test that only
checks the final body.
"""

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from gateway.policy import Policy
from gateway.routes import Route

# Defined by RFC 9110 as meaningful only for a single connection. Relaying them corrupts
# connection handling in ways that surface under load rather than in a test.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Never relayed from a caller. The gateway writes its own; accepting an inbound copy would
# let a client author a header some upstream might one day be taught to trust. Nothing
# trusts them today, and this is what keeps that true by construction.
_NEVER_FROM_CLIENT = ("x-forwarded-", "x-deflect-", "x-real-ip")


def clean_request_headers(headers) -> dict[str, str]:
    """The caller's headers, minus what must not travel.

    Authorization is deliberately kept: the upstream re-resolves the caller itself, which
    is the property that makes bypassing the gateway a non-event rather than a breach.
    """
    return {
        key: value
        for key, value in dict(headers).items()
        if (lowered := key.lower()) not in _HOP_BY_HOP
        and lowered != "host"
        and lowered != "content-length"
        and not lowered.startswith(_NEVER_FROM_CLIENT)
    }


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP and key.lower() != "content-length"
    }


async def forward(
    route: Route, request: Request, base_url: str, client: httpx.AsyncClient
) -> StreamingResponse:
    """Send this request to `base_url` and stream the answer back.

    Timeouts are split: connect and write are short because a healthy upstream on a
    private network answers in milliseconds, while read is per-route because /ask
    legitimately takes far longer than /traces. A streaming route gets no read timeout at
    all -- a long gap between SSE frames is the normal shape of a slow answer, not a
    failure -- and is bounded by the client going away instead.
    """
    timeout = httpx.Timeout(
        connect=Policy.CONNECT_TIMEOUT,
        write=Policy.WRITE_TIMEOUT,
        read=None if route.stream else route.timeout,
        pool=Policy.CONNECT_TIMEOUT,
    )

    upstream_request = client.build_request(
        request.method,
        f"{base_url}{request.url.path}",
        headers=clean_request_headers(request.headers),
        params=request.query_params,
        content=await request.body(),
        timeout=timeout,
    )

    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.TimeoutException as exc:
        # 504 rather than 502: the difference between "did not answer in time" and "could
        # not be reached" is the first thing anyone debugging this will want.
        raise HTTPException(504, f"{route.upstream} did not answer in time") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"{route.upstream} could not be reached") from exc

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        # Closing the upstream response is what returns its connection to the pool. Without
        # this the pool leaks one connection per request and the gateway stalls under load.
        background=BackgroundTask(upstream.aclose),
    )
```

- [ ] **Step 5: Add the two upstream-failure tests**

Append to `tests/test_proxy.py`:

```python
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
```

Remove the now-unused `pytest` import if ruff flags it.

- [ ] **Step 6: Run to verify passing**

Run: `cd services/gateway && uv run pytest tests/test_proxy.py -v && uv run ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add services/gateway
git commit -m "carry requests through without buffering them

The response is streamed rather than read. A gateway that buffers turns a
token-by-token answer into a thirty-second wait, and the failure is invisible to
any test that only checks the final body -- so the test gates the fake upstream
on an event and asserts the first frame arrives while the second is still
unwritten.

The caller's Authorization is passed through unchanged rather than swapped for
the service token, because the upstream re-resolves the caller itself. That is
what makes reaching a service directly a non-event rather than a breach.

Inbound X-Forwarded-* and X-Deflect-* are dropped. Nothing upstream trusts them
today, and refusing to relay them is what keeps that true by construction."
```

---

## Task 4: Principals at the edge

**Files:**
- Create: `services/gateway/src/gateway/principal.py`, `services/gateway/tests/test_principals.py`
- Modify: `services/gateway/src/gateway/main.py`

**Interfaces:**
- Consumes: `Route` (Task 2), `forward` (Task 3), `resolve_principal`/`satisfies` from `deflect_common.auth`.
- Produces: `async def allowed(route: Route, authorization: str | None, sessions: SessionStore, service_token: str, operator_token: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

`services/gateway/tests/test_principals.py`:

```python
import pytest
from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore

from gateway.principal import allowed
from gateway.routes import ROUTES, Route

SERVICE = "test-service-token"
OPERATOR = "test-operator-token"


async def _store_with(role: str | None) -> tuple[FakeSessionStore, str | None]:
    store = FakeSessionStore()
    if role is None:
        return store, None
    token = f"session-{role}"
    await store.put(hash_token(token), user_id="7", role=role, ttl_seconds=3600)
    return store, token


async def _decide(route: Route, credential: str | None, role: str | None = None) -> bool:
    store, session_token = await _store_with(role)
    value = session_token if credential == "session" else credential
    header = f"Bearer {value}" if value else None
    return await allowed(route, header, store, SERVICE, OPERATOR)


PUBLIC = Route("POST", "/ask", "answer", None)
SERVICE_ONLY = Route("POST", "/search", "retrieval", "service")
OPERATOR_ONLY = Route("POST", "/ingest", "retrieval", "operator")
VIEWER_OK = Route("GET", "/traces", "answer", "viewer")
SESSION_ONLY = Route("GET", "/auth/me", "auth", "session")


async def test_a_public_route_needs_nothing():
    assert await _decide(PUBLIC, None) is True


async def test_a_public_route_still_admits_a_credentialled_caller():
    assert await _decide(PUBLIC, SERVICE) is True


@pytest.mark.parametrize(
    ("route", "credential", "role", "expected"),
    [
        (SERVICE_ONLY, SERVICE, None, True),
        (SERVICE_ONLY, OPERATOR, None, False),
        (SERVICE_ONLY, "session", "admin", False),
        (OPERATOR_ONLY, OPERATOR, None, True),
        (OPERATOR_ONLY, SERVICE, None, False),
        (OPERATOR_ONLY, "session", "admin", True),
        (OPERATOR_ONLY, "session", "viewer", False),
        (VIEWER_OK, OPERATOR, None, True),
        (VIEWER_OK, "session", "viewer", True),
        (VIEWER_OK, "session", "admin", True),
        (VIEWER_OK, SERVICE, None, False),
    ],
    ids=lambda v: str(v),
)
async def test_the_matrix_matches_the_shared_rules(route, credential, role, expected):
    """The gateway must reach the same verdict as the service behind it. A gateway that
    was stricter would break a caller the service accepts; one that was looser would send
    traffic that can only 401, and hide which layer refused it."""
    assert await _decide(route, credential, role) is expected


async def test_a_service_token_never_satisfies_operator():
    """Collapsing machines and operators would let any service trigger spend."""
    assert await _decide(OPERATOR_ONLY, SERVICE) is False


@pytest.mark.parametrize("credential", [SERVICE, OPERATOR])
async def test_a_token_does_not_satisfy_a_session_route(credential):
    """/auth/logout revokes a session row, and a token has none to revoke. Admitting a
    token here would reach a route that can only fail."""
    assert await _decide(SESSION_ONLY, credential) is False


async def test_any_valid_session_satisfies_a_session_route():
    assert await _decide(SESSION_ONLY, "session", "viewer") is True
    assert await _decide(SESSION_ONLY, "session", "admin") is True


async def test_an_unknown_session_is_refused():
    store = FakeSessionStore()

    assert await allowed(VIEWER_OK, "Bearer nonsense", store, SERVICE, OPERATOR) is False


async def test_a_malformed_header_is_refused():
    store = FakeSessionStore()

    for header in ["", "Bearer", "Basic abc", "sess-abc", "Bearer "]:
        assert await allowed(VIEWER_OK, header, store, SERVICE, OPERATOR) is False


async def test_an_unknown_role_fails_closed():
    """A role written straight into the database must refuse rather than raise."""
    store = FakeSessionStore()
    await store.put(hash_token("odd"), user_id="7", role="superuser", ttl_seconds=3600)

    assert await allowed(VIEWER_OK, "Bearer odd", store, SERVICE, OPERATOR) is False


async def test_every_route_in_the_table_is_decidable():
    """No route may name a principal the resolver cannot evaluate."""
    store = FakeSessionStore()

    for route in ROUTES:
        assert await allowed(route, None, store, SERVICE, OPERATOR) in (True, False)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd services/gateway && uv run pytest tests/test_principals.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.principal'`

- [ ] **Step 3: Implement `principal.py`**

```python
"""Whether a caller may reach a route, decided at the edge.

This deliberately duplicates the verdict the upstream service will reach on its own. The
duplication is the design: the gateway refuses obvious traffic early, and the service
still refuses it if anything reaches the service another way. Neither depends on the
other being correct.

It reuses resolve_principal and satisfies rather than reimplementing them, so the two
layers cannot drift into disagreeing about what an admin is.
"""

from deflect_common.auth import resolve_principal, satisfies
from deflect_common.sessions import SessionStore

from gateway.routes import Route


async def allowed(
    route: Route,
    authorization: str | None,
    sessions: SessionStore,
    service_token: str,
    operator_token: str,
) -> bool:
    """Whether this credential satisfies this route's requirement."""
    if route.principal is None:
        return True

    principal = await resolve_principal(
        authorization, service_token, operator_token, sessions
    )
    if principal is None:
        return False

    if route.principal == "session":
        # Not expressible through satisfies, which is about privilege level. This is about
        # kind: /auth/logout revokes a session row and a token has none, so admitting a
        # token would forward a request that can only fail upstream.
        return principal.kind == "session"

    return satisfies(principal, route.principal)
```

- [ ] **Step 4: Run to verify passing**

Run: `cd services/gateway && uv run pytest tests/test_principals.py -v && uv run ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add services/gateway
git commit -m "decide at the edge what the service will decide again

The gateway reaches the same verdict resolve_principal reaches upstream, using
the same two functions rather than a second copy of the rules, so the layers
cannot drift into disagreeing about what an admin is.

Duplicating the check is the point. The gateway turns away obvious traffic
early; the service still refuses it if anything arrives another way. Neither
layer depends on the other being correct, which is what makes a bypass a
non-event instead of a breach.

'session' is a gateway-only requirement and is decided on kind rather than
through satisfies: /auth/logout revokes a session row, and a token has none."
```

---

## Task 5: Wire the table into the app

**Files:**
- Modify: `services/gateway/src/gateway/main.py`
- Create: `services/gateway/tests/test_gateway_routes.py`

**Interfaces:**
- Consumes: everything from Tasks 2–4.
- Produces: `app` serving every route in `ROUTES`; `build_sessions()` and `build_client()` dependency callables, overridable in tests.

- [ ] **Step 1: Write the failing tests**

`services/gateway/tests/test_gateway_routes.py`:

```python
import httpx
import pytest_asyncio
from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore
from doubles import build_upstream
from httpx import ASGITransport, AsyncClient

from gateway.main import app as gateway_app
from gateway.main import build_client, build_sessions

SERVICE = {"Authorization": "Bearer test-service-token"}
OPERATOR = {"Authorization": "Bearer test-operator-token"}


@pytest_asyncio.fixture
async def store():
    store = FakeSessionStore()
    await store.put(hash_token("admin-token"), user_id="7", role="admin", ttl_seconds=3600)
    return store


@pytest_asyncio.fixture
async def upstream():
    return build_upstream()


@pytest_asyncio.fixture
async def app(store, upstream):
    client = AsyncClient(transport=ASGITransport(app=upstream), base_url="http://upstream")
    gateway_app.dependency_overrides[build_sessions] = lambda: store
    gateway_app.dependency_overrides[build_client] = lambda: client
    yield gateway_app
    gateway_app.dependency_overrides.clear()
    await client.aclose()


async def call(app, method: str, path: str, **kwargs) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, **kwargs)


async def test_metrics_never_reaches_an_upstream(app, upstream):
    """/metrics is absent from the route table, so it is unroutable rather than guarded.
    The gateway's own /metrics is a separate route; what must never happen is a proxied
    request to a service's /metrics.

    Asserted on the upstream's record of every path it was asked for, not on the response
    body — a substring check there collides with the gateway's own metric names, and
    seen_headers alone would be vacuous because the double only sets it for paths it
    actually serves.
    """
    await call(app, "GET", "/metrics", headers=SERVICE)

    assert upstream.state.seen_paths == []


async def test_an_unknown_path_is_a_404_before_any_upstream_call(app, upstream):
    response = await call(app, "GET", "/nope", headers=OPERATOR)

    assert response.status_code == 404
    assert upstream.state.seen_paths == []


async def test_an_upstream_docs_path_is_not_routed(app, upstream):
    """Same reasoning. /redoc and /openapi.json belong to the gateway's own FastAPI app
    when ENV is not production, and must never become a proxied request to a service.

    Not asserted on the body: FastAPI puts every handler's docstring into the OpenAPI
    schema, so any word this codebase uses in a docstring — "upstream" among them — will
    appear in /openapi.json for reasons that have nothing to do with routing.
    """
    for path in ["/redoc", "/openapi.json"]:
        await call(app, "GET", path, headers=OPERATOR)

    assert upstream.state.seen_paths == []


async def test_a_guarded_route_refuses_a_missing_credential(app):
    response = await call(app, "POST", "/ingest", json={"root": "/corpus"})

    assert response.status_code == 401


async def test_a_guarded_route_refuses_the_wrong_credential(app):
    response = await call(app, "POST", "/ingest", headers=SERVICE, json={"root": "/corpus"})

    assert response.status_code == 401


async def test_an_admin_session_reaches_an_operator_route(app):
    response = await call(
        app, "POST", "/ingest", headers={"Authorization": "Bearer admin-token"},
        json={"root": "/corpus"},
    )

    assert response.status_code != 401


async def test_health_and_ready_are_the_gateways_own(app):
    for path in ["/health", "/ready"]:
        response = await call(app, "GET", path)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


async def test_the_gateways_own_metrics_needs_the_service_token(app):
    assert (await call(app, "GET", "/metrics")).status_code == 401
```

- [ ] **Step 2: Run to verify failure**

Run: `cd services/gateway && uv run pytest tests/test_gateway_routes.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_client' from 'gateway.main'`

- [ ] **Step 3: Rewrite `main.py`**

```python
from typing import Annotated

import httpx
from deflect_common.auth import bearer_guard
from deflect_common.logging import configure_logging
from deflect_common.observability import RequestIdMiddleware, metrics_response
from deflect_common.sessions import RedisSessionStore, SessionStore
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response

from gateway.config import get_settings
from gateway.principal import allowed
from gateway.proxy import forward
from gateway.routes import ROUTES, Route

_docs = (
    {"docs_url": None, "redoc_url": None, "openapi_url": None}
    if get_settings().env == "production"
    else {}
)
app = FastAPI(title="Deflect gateway", **_docs)
configure_logging()
app.add_middleware(RequestIdMiddleware)

# Built at import: an unset token or url aborts this module and the process exits before
# binding a port, the same refuse-to-boot behaviour every other service has.
_settings = get_settings()
_sessions = RedisSessionStore(_settings.redis_url)
_client = httpx.AsyncClient()
require_service = bearer_guard(_settings.service_token, "service")

_UPSTREAMS = {
    "retrieval": _settings.retrieval_url,
    "answer": _settings.answer_url,
    "evals": _settings.evals_url,
    "auth": _settings.auth_url,
}


def build_sessions() -> SessionStore:
    return _sessions


def build_client() -> httpx.AsyncClient:
    return _client


SessionsDep = Annotated[SessionStore, Depends(build_sessions)]
ClientDep = Annotated[httpx.AsyncClient, Depends(build_client)]


def _handler_for(route: Route):
    """One handler per table entry, closed over its own Route.

    Registered through add_api_route rather than matched by hand, so FastAPI does the
    path parsing -- {job_id} and friends -- and produces the right 404 and 405 without
    the gateway reimplementing either.
    """

    async def handler(
        request: Request,
        sessions: SessionsDep,
        client: ClientDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        if not await allowed(
            route, authorization, sessions, _settings.service_token, _settings.operator_token
        ):
            raise HTTPException(
                status_code=401,
                detail=f"a {route.principal} credential is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await forward(route, request, _UPSTREAMS[route.upstream], client)

    return handler


for _route in ROUTES:
    app.add_api_route(
        _route.path,
        _handler_for(_route),
        methods=[_route.method],
        # The proxied body is whatever the upstream returned; describing it as a model
        # would be a second, drifting copy of the upstream's contract.
        response_model=None,
    )

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: this process is answering. Deliberately touches no dependency."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness: this process can route.

    It deliberately does not probe its upstreams. A gateway that reports unready because
    one service is sick turns one outage into a total one, which is the opposite of what
    an edge is for. A sick upstream is the circuit breaker's job, per route.
    """
    return {"status": "ok"}


@router.get("/metrics", dependencies=[Depends(require_service)])
async def metrics() -> Response:
    """The gateway's OWN metrics. No upstream's /metrics is routable."""
    return metrics_response()


app.include_router(router)
```

- [ ] **Step 4: Run to verify passing**

Run: `cd services/gateway && uv run pytest -v && uv run ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add services/gateway
git commit -m "serve the table, and nothing that is not in it

Each entry is registered through add_api_route with a handler closed over its
own Route, so FastAPI does the path parsing and produces the right 404 and 405
rather than the gateway reimplementing both badly.

An upstream /metrics is unroutable because it is absent from the table, not
because a guard protects it. The gateway's own /metrics keeps the service-token
guard every other service uses.

/ready deliberately does not probe upstreams: an edge that reports unready
because one service is sick turns one outage into a total one."
```

---

## Task 6: Move rate limiting to the gateway, semantics unchanged

**Files:**
- Modify: `services/gateway/src/gateway/main.py`, `services/answer/src/answer/main.py`, `services/auth/src/auth/main.py`
- Create: `services/gateway/tests/test_limits.py`
- Modify: `services/answer/tests/test_ask_limits.py`, `services/auth/tests/test_routes.py`

**Interfaces:**
- Consumes: `SlidingWindowLimiter` and `edge_address` from `deflect_common.ratelimit`.
- Produces: a 429 on `/ask` and `/auth/login` at the gateway; no limiter in `answer` or `auth`.

This task deliberately keeps the existing sliding-window behaviour. Task 7 changes the algorithm. Two commits, so a behaviour regression is attributable to one of them rather than to both at once.

- [ ] **Step 1: Write the failing gateway tests**

`services/gateway/tests/test_limits.py`:

```python
import httpx
import pytest_asyncio
from deflect_common.sessions import FakeSessionStore
from doubles import build_upstream
from httpx import ASGITransport, AsyncClient

from gateway.main import app as gateway_app
from gateway.main import build_client, build_limiters, build_sessions
from gateway.policy import Policy


@pytest_asyncio.fixture
async def app():
    upstream = build_upstream()
    client = AsyncClient(transport=ASGITransport(app=upstream), base_url="http://upstream")
    gateway_app.dependency_overrides[build_sessions] = lambda: FakeSessionStore()
    gateway_app.dependency_overrides[build_client] = lambda: client
    gateway_app.dependency_overrides[build_limiters] = lambda: _fresh_limiters()
    yield gateway_app
    gateway_app.dependency_overrides.clear()
    await client.aclose()


def _fresh_limiters():
    """A new set per test: a module-level singleton shared across a session makes one
    test's traffic another test's failure."""
    from deflect_common.ratelimit import SlidingWindowLimiter

    return {
        "ask": SlidingWindowLimiter(Policy.ASK_PER_HOUR, Policy.WINDOW_SECONDS),
        "login": SlidingWindowLimiter(Policy.LOGIN_PER_HOUR, Policy.WINDOW_SECONDS),
    }


async def call(app, path: str, headers=None) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(path, json={"question": "q"}, headers=headers or {})


async def test_the_allowance_is_spent_and_then_refused(app):
    for _ in range(Policy.ASK_PER_HOUR):
        assert (await call(app, "/ask")).status_code != 429

    assert (await call(app, "/ask")).status_code == 429


async def test_a_refusal_carries_retry_after(app):
    for _ in range(Policy.ASK_PER_HOUR):
        await call(app, "/ask")

    response = await call(app, "/ask")

    assert int(response.headers["Retry-After"]) > 0


async def test_a_spoofed_forwarded_header_does_not_buy_a_fresh_allowance(app):
    """The regression test for edge_address. If the gateway believed the leftmost entry,
    each of these would be a new key and the limit would never bind."""
    for i in range(Policy.ASK_PER_HOUR):
        response = await call(app, "/ask", headers={"X-Forwarded-For": f"9.9.9.{i}"})
        assert response.status_code != 429

    response = await call(app, "/ask", headers={"X-Forwarded-For": "9.9.9.250"})

    assert response.status_code == 429


async def test_an_unguarded_route_is_not_limited(app):
    """Only routes naming a limiter are limited. /eval-runs is public and cheap."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        for _ in range(Policy.ASK_PER_HOUR + 5):
            assert (await c.get("/eval-runs")).status_code != 429
```

- [ ] **Step 2: Run to verify failure**

Run: `cd services/gateway && uv run pytest tests/test_limits.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_limiters'`

- [ ] **Step 3: Add limiting to `main.py`**

Add these imports:

```python
import time

from deflect_common.ratelimit import SlidingWindowLimiter, edge_address

from gateway.policy import Policy
```

Add after `require_service`:

```python
# One limiter per named allowance. Keyed by the address the edge computed, which is not
# the address uvicorn would have reported -- see edge_address.
_limiters = {
    "ask": SlidingWindowLimiter(Policy.ASK_PER_HOUR, Policy.WINDOW_SECONDS),
    "login": SlidingWindowLimiter(Policy.LOGIN_PER_HOUR, Policy.WINDOW_SECONDS),
}


def build_limiters() -> dict[str, SlidingWindowLimiter]:
    return _limiters


LimitersDep = Annotated[dict[str, SlidingWindowLimiter], Depends(build_limiters)]
```

Change the handler signature to take `limiters: LimitersDep` and insert, before the `allowed` check:

```python
        if route.limit is not None:
            address = edge_address(request, _settings.trusted_proxy_hops)
            if not limiters[route.limit].check(address, time.monotonic()):
                raise HTTPException(
                    status_code=429,
                    detail="too many requests from this address",
                    headers={"Retry-After": str(Policy.WINDOW_SECONDS)},
                )
```

The limit is checked before the credential deliberately: a caller flooding a public route should be turned away at the cheapest possible point, and `/ask` and `/auth/login` are both public anyway.

- [ ] **Step 4: Run the gateway tests**

Run: `cd services/gateway && uv run pytest -v`
Expected: all PASS.

- [ ] **Step 5: Remove the limiter from `answer`**

In `services/answer/src/answer/main.py`: delete `_ask_limiter`, its `SlidingWindowLimiter` import, the `client_address`/`token_matches` use in the limit check, and the 429 block — keeping the daily-cap check that follows it. The function keeps its docstring's second half; rewrite the first half to:

```python
    """Reject a question that would exceed the day's budget.

    The per-address window moved to the gateway, where one address means one thing. This
    is the spend bound, and it stays here because it counts rows in this service's own
    traces table.
    """
```

In `services/answer/tests/test_ask_limits.py`: delete any test asserting the per-address 429. Keep every `questions_today` test and the daily-cap test unchanged.

- [ ] **Step 6: Remove the limiter from `auth`**

In `services/auth/src/auth/main.py`: delete `_login_limiter`, the `SlidingWindowLimiter` and `client_address` imports, and the 429 block at the top of `login_route`. Leave the lockout, the 401 handling and the logging exactly as they are.

In `services/auth/tests/test_routes.py`: nothing to delete — there is no login-429 test today.

- [ ] **Step 7: Run every affected suite**

```bash
cd services/answer  && uv run pytest -q && uv run ruff check .
cd ../auth          && uv run pytest -q && uv run ruff check .
cd ../gateway       && uv run pytest -q && uv run ruff check .
cd ../../packages/common && uv run pytest -q && uv run ruff check .
```
Expected: all green. `answer` and `auth` will report fewer tests than before by exactly the number deleted; record the new counts.

- [ ] **Step 8: Commit**

```bash
git add services/gateway services/answer services/auth
git commit -m "move per-address limiting to the one place that sees an address

The answer and auth services each kept their own per-address window, so an
address meant two different things depending on which door it came through. At
the edge it means one thing.

Behaviour is deliberately unchanged: the same sliding window, the same rates.
The algorithm changes in the next commit, separately, so a regression is
attributable to the move or to the rewrite rather than to both at once.

The daily cap stays in answer. It counts rows in that service's own traces
table and bounds spend rather than abuse -- a different question, asked of a
different thing."
```

---

## Task 7: Swap the algorithm to a leaky bucket

**Files:**
- Modify: `packages/common/src/deflect_common/ratelimit.py`, `packages/common/tests/test_ratelimit.py`
- Modify: `services/gateway/src/gateway/main.py`, `services/gateway/tests/test_limits.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Decision(allowed: bool, retry_after: float)`; `Limiter` protocol with `async def check(self, key: str, now: float) -> Decision`; `InMemoryLeakyBucket(rate, period_seconds, capacity)`; `RedisLeakyBucket(redis, rate, period_seconds, capacity, prefix)`.

**On the formulation.** A leaky bucket can be written two ways, and they are the same algorithm. The *schedule* form (GCRA) stores one number — the earliest time the next request may arrive. The *level* form stores how full the bucket is and drains it at a constant rate. They convert exactly: `level = (tat - now) / emission`, `capacity = burst`.

This plan uses the **level** form. The schedule form is one float smaller, but "theoretical arrival time" is not a thing a reader can picture, and this bucket has to be debuggable at three in the morning by someone who did not write it. A level that drains is. The extra float is not a cost worth the opacity.

- [ ] **Step 1: Write the failing tests**

Append to `packages/common/tests/test_ratelimit.py`:

```python
from deflect_common.ratelimit import Decision, InMemoryLeakyBucket

RATE, PERIOD, CAPACITY = 20, 3600.0, 5
DRAIN = PERIOD / RATE  # 180 seconds to leak one unit


def _limiter() -> InMemoryLeakyBucket:
    return InMemoryLeakyBucket(rate=RATE, period_seconds=PERIOD, capacity=CAPACITY)


async def _fill(limiter: InMemoryLeakyBucket, now: float = 0.0) -> None:
    for _ in range(CAPACITY):
        await limiter.check("a", now=now)


async def test_a_full_bucket_can_be_filled_back_to_back():
    """Five questions in a row is someone trying the demo, not abusing it."""
    limiter = _limiter()

    for _ in range(CAPACITY):
        assert (await limiter.check("a", now=0.0)).allowed is True


async def test_the_request_that_would_overflow_is_refused():
    limiter = _limiter()
    await _fill(limiter)

    assert (await limiter.check("a", now=0.0)).allowed is False


async def test_the_refusal_says_how_long_until_one_unit_has_leaked():
    limiter = _limiter()
    await _fill(limiter)

    decision = await limiter.check("a", now=0.0)

    assert decision.retry_after == pytest.approx(DRAIN)


async def test_waiting_the_advertised_time_lets_exactly_one_through():
    """The test the hardcoded Retry-After: 3600 could never have passed."""
    limiter = _limiter()
    await _fill(limiter)
    refused = await limiter.check("a", now=0.0)

    assert (await limiter.check("a", now=refused.retry_after)).allowed is True
    assert (await limiter.check("a", now=refused.retry_after)).allowed is False


async def test_it_keeps_leaking_at_the_sustained_rate():
    """Twenty an hour, whatever the capacity. One request every drain interval is
    accepted indefinitely, because that is exactly the rate the hole permits."""
    limiter = _limiter()
    await _fill(limiter)

    for step in range(1, RATE + 1):
        assert (await limiter.check("a", now=step * DRAIN)).allowed is True


async def test_a_long_silence_does_not_bank_credit():
    """An idle bucket is empty, not negative. Without the clamp, an address that went
    quiet for a day would come back able to send an unbounded burst."""
    limiter = _limiter()
    await _fill(limiter)

    for _ in range(CAPACITY):
        assert (await limiter.check("a", now=PERIOD * 24)).allowed is True

    assert (await limiter.check("a", now=PERIOD * 24)).allowed is False


async def test_keys_do_not_share_a_bucket():
    limiter = _limiter()
    await _fill(limiter)

    assert (await limiter.check("b", now=0.0)).allowed is True


async def test_an_allowed_decision_asks_for_no_wait():
    assert (await _limiter().check("a", now=0.0)) == Decision(allowed=True, retry_after=0.0)


async def test_a_capacity_of_one_is_a_plain_rate_limit():
    limiter = InMemoryLeakyBucket(rate=RATE, period_seconds=PERIOD, capacity=1)

    assert (await limiter.check("a", now=0.0)).allowed is True
    assert (await limiter.check("a", now=0.0)).allowed is False


async def test_a_drained_bucket_is_forgotten():
    """One entry per address ever seen would be an unbounded leak on a public endpoint."""
    limiter = _limiter()
    await limiter.check("a", now=0.0)

    assert limiter.tracked_keys(now=0.0) == 1
    assert limiter.tracked_keys(now=DRAIN * CAPACITY * 2) == 0


async def test_a_nonsense_configuration_is_refused_at_construction():
    """A limiter that permits nothing is a misconfiguration, and it should fail where it
    is built rather than on the first request."""
    for kwargs in [
        {"rate": 0, "period_seconds": 60.0, "capacity": 1},
        {"rate": 10, "period_seconds": 0.0, "capacity": 1},
        {"rate": 10, "period_seconds": 60.0, "capacity": 0},
    ]:
        with pytest.raises(ValueError):
            InMemoryLeakyBucket(**kwargs)
```

`test_it_keeps_leaking_at_the_sustained_rate` is the one that pins the rate rather than the burst: after the bucket is full, one request per drain interval is accepted forever. Keep the arithmetic literal rather than simplifying it away.

- [ ] **Step 2: Run to verify failure**

Run: `cd packages/common && uv run pytest tests/test_ratelimit.py -k bucket -v`
Expected: FAIL — `ImportError: cannot import name 'InMemoryLeakyBucket'`

- [ ] **Step 3: Implement the limiters**

Append to `packages/common/src/deflect_common/ratelimit.py`:

```python
@dataclass(frozen=True)
class Decision:
    """Whether the request may proceed, and if not, when to come back.

    retry_after is computed rather than assumed. The sliding-window log this replaces
    could not produce it, which is why both services hardcoded a full hour and told a
    caller one second over the limit to wait sixty minutes.
    """

    allowed: bool
    retry_after: float


class Limiter(Protocol):
    async def check(self, key: str, now: float) -> Decision: ...


class _LeakyBucket:
    """The arithmetic, shared by both implementations so they cannot disagree.

    A bucket of `capacity` units with a hole in it. Every request pours one unit in; the
    hole drains `rate` units per `period_seconds`, continuously. If a request would
    overflow the bucket it is refused, and how long until it fits is division rather than
    a guess -- which is the whole reason the wait can finally be honest.

    Nothing is stored per request, only the level and when it was last observed, so the
    drain is computed on read rather than by a timer. Two floats per key regardless of
    how much traffic an address sends.

    (The same algorithm is often written as GCRA, which tracks the next permitted arrival
    time and needs one float instead of two. It is exactly equivalent -- level is
    (tat - now) / emission -- and it is not used here because a level that drains is
    something a reader can picture and a "theoretical arrival time" is not.)
    """

    def __init__(self, rate: int, period_seconds: float, capacity: int) -> None:
        if rate <= 0 or period_seconds <= 0 or capacity < 1:
            raise ValueError(
                f"a bucket of {capacity} draining {rate} per {period_seconds}s permits "
                "nothing; refusing to build it"
            )
        self._leak_per_second = rate / period_seconds
        self._capacity = float(capacity)

    def _decide(
        self, level: float | None, last_seen: float, now: float
    ) -> tuple[bool, float, float]:
        """Returns (allowed, new_level, retry_after). Pure, so both stores share it."""
        if level is None:
            drained = 0.0
        else:
            # Clamped at zero: an idle bucket is empty, not negative. Without the clamp a
            # long silence would bank credit and the next burst would be unbounded.
            drained = max(0.0, level - (now - last_seen) * self._leak_per_second)

        if drained + 1.0 > self._capacity:
            return False, drained, (drained + 1.0 - self._capacity) / self._leak_per_second

        return True, drained + 1.0, 0.0


class InMemoryLeakyBucket(_LeakyBucket):
    """Per-process, for tests and for a single worker.

    Kept alongside the Redis one for the same reason FakeSessionStore is kept: a test
    suite that needs Redis to run is a test suite that stops being run.
    """

    def __init__(self, rate: int, period_seconds: float, capacity: int) -> None:
        super().__init__(rate, period_seconds, capacity)
        self._buckets: dict[str, tuple[float, float]] = {}

    async def check(self, key: str, now: float) -> Decision:
        held = self._buckets.get(key)
        level, last_seen = held if held is not None else (None, now)
        allowed, new_level, retry_after = self._decide(level, last_seen, now)

        self._buckets[key] = (new_level, now)
        self._evict(now)
        return Decision(allowed, retry_after)

    def _evict(self, now: float) -> None:
        """Drop buckets that have fully drained.

        One entry per address ever seen would be an unbounded leak on an endpoint open to
        the internet -- the same reasoning the sliding window's eviction had.
        """
        empty = [
            key
            for key, (level, last_seen) in self._buckets.items()
            if level - (now - last_seen) * self._leak_per_second <= 0.0
        ]
        for key in empty:
            del self._buckets[key]

    def tracked_keys(self, now: float) -> int:
        self._evict(now)
        return len(self._buckets)


# One round trip, and atomic: a read-modify-write across two calls would let two workers
# both see room in the same bucket and both pour into it.
_LEAK_LUA = """
local held = redis.call('HMGET', KEYS[1], 'level', 'seen')
local now = tonumber(ARGV[1])
local leak = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])

local level = 0.0
if held[1] then
  local drained = tonumber(held[1]) - (now - tonumber(held[2])) * leak
  if drained > 0 then level = drained end
end

if level + 1.0 > capacity then
  return {0, tostring((level + 1.0 - capacity) / leak)}
end

redis.call('HSET', KEYS[1], 'level', tostring(level + 1.0), 'seen', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(((level + 1.0) / leak) * 1000))
return {1, '0'}
"""


class RedisLeakyBucket(_LeakyBucket):
    """The same bucket, shared across workers.

    Values cross the Lua boundary as strings because Redis coerces Lua numbers to
    integers, which would silently truncate every fractional second -- and this algorithm
    is entirely fractional seconds.

    The key's expiry is set to how long the bucket needs to drain completely, so an
    address that stops calling stops costing memory without a sweeper.
    """

    def __init__(
        self,
        redis,
        rate: int,
        period_seconds: float,
        capacity: int,
        prefix: str = "ratelimit:",
    ) -> None:
        super().__init__(rate, period_seconds, capacity)
        self._redis = redis
        self._prefix = prefix
        self._script = redis.register_script(_LEAK_LUA)

    async def check(self, key: str, now: float) -> Decision:
        allowed, retry_after = await self._script(
            keys=[self._prefix + key],
            args=[now, self._leak_per_second, self._capacity],
        )
        return Decision(bool(int(allowed)), float(retry_after))
```

Add to the file's imports: `from dataclasses import dataclass` and `from typing import Protocol`.

- [ ] **Step 4: Run to verify passing**

Run: `cd packages/common && uv run pytest -v && uv run ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 5: Switch the gateway to the leaky bucket**

In `services/gateway/src/gateway/main.py`, replace the limiter construction:

```python
# ASK_BURST is passed as `capacity`: they are the same number seen from two sides. The
# policy constant names what an operator cares about -- how big a burst is tolerated --
# and the parameter names the mechanism that delivers it, the depth of the bucket.
_limiters: dict[str, Limiter] = {
    "ask": InMemoryLeakyBucket(Policy.ASK_PER_HOUR, Policy.WINDOW_SECONDS, Policy.ASK_BURST),
    "login": InMemoryLeakyBucket(
        Policy.LOGIN_PER_HOUR, Policy.WINDOW_SECONDS, Policy.LOGIN_BURST
    ),
}
```

and the check inside the handler:

```python
        if route.limit is not None:
            address = edge_address(request, _settings.trusted_proxy_hops)
            decision = await limiters[route.limit].check(address, time.monotonic())
            if not decision.allowed:
                raise HTTPException(
                    status_code=429,
                    detail="too many requests from this address",
                    # Computed, not assumed. Waiting this long genuinely works.
                    headers={"Retry-After": str(max(1, int(decision.retry_after)))},
                )
```

Update the imports accordingly and delete the `SlidingWindowLimiter` import.

`InMemoryLeakyBucket` rather than `RedisLeakyBucket` is wired here on purpose: the gateway is pinned to one worker in Task 8, and a Redis dependency on the request path should be added when the worker count makes it necessary. `RedisLeakyBucket` exists, is tested, and is a one-line swap when that day comes — which is the point of building it now rather than twice.

- [ ] **Step 6: Update the gateway's limit tests**

In `services/gateway/tests/test_limits.py`, change `_fresh_limiters` to build `InMemoryLeakyBucket` with the burst values as `capacity`, and change `test_the_allowance_is_spent_and_then_refused` to spend `Policy.ASK_BURST` rather than `Policy.ASK_PER_HOUR` requests. Add:

```python
async def test_the_advertised_wait_is_honest(app):
    for _ in range(Policy.ASK_BURST):
        await call(app, "/ask")

    response = await call(app, "/ask")

    assert response.status_code == 429
    # An hour would be the old hardcoded answer; the real one is one emission interval.
    assert int(response.headers["Retry-After"]) < Policy.WINDOW_SECONDS
```

- [ ] **Step 7: Run everything**

```bash
cd packages/common && uv run pytest -q && cd ../../services/gateway && uv run pytest -q && uv run ruff check .
```
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add packages/common services/gateway
git commit -m "make the limiter a leaky bucket that can say when to come back

A sliding-window log is exact but stores an entry per request and permits the
whole allowance at once: twenty questions in a second, then an hour of
refusals. It also cannot compute a wait, which is why both services hardcoded
Retry-After: 3600 and told a caller one second over the limit to wait sixty
minutes.

A leaky bucket holds a level instead of a log. Each request pours in one unit
and the hole drains at the sustained rate, so the state is two floats per key
however much traffic an address sends, and how long until a refused request
fits is division rather than a guess.

Written as a level rather than as GCRA, which is the same algorithm expressed
as the next permitted arrival time and stores one float instead of two. The
extra float buys something worth more: a level that drains is a thing a reader
can picture at three in the morning, and a theoretical arrival time is not.

The sustained rates are unchanged, so the relationship with answer's daily cap
still holds. Capacity is new: five back-to-back questions is someone trying the
demo, and the log this replaces allowed twenty anyway.

The in-memory bucket is wired in because the gateway runs one worker. The Redis
one is built and tested so that stops being a constraint the day it matters."
```

---

## Task 8: The circuit breaker

**Files:**
- Create: `services/gateway/src/gateway/breaker.py`, `services/gateway/tests/test_breaker.py`
- Modify: `services/gateway/src/gateway/main.py`

**Interfaces:**
- Consumes: `Policy` (Task 2).
- Produces: `CircuitBreaker(failures: int, cooldown_seconds: float)` with `is_open(upstream: str, now: float) -> bool`, `record_failure(upstream: str, now: float) -> None`, `record_success(upstream: str) -> None`.

- [ ] **Step 1: Write the failing tests**

`services/gateway/tests/test_breaker.py`:

```python
from gateway.breaker import CircuitBreaker
from gateway.policy import Policy

FAILURES = Policy.BREAKER_FAILURES
COOLDOWN = Policy.BREAKER_COOLDOWN_SECONDS


def _breaker() -> CircuitBreaker:
    return CircuitBreaker(FAILURES, COOLDOWN)


def test_a_healthy_upstream_is_never_open():
    assert _breaker().is_open("answer", now=0.0) is False


def test_fewer_failures_than_the_threshold_keeps_it_closed():
    """One blip is not a pattern, and refusing after one would make the gateway flap."""
    breaker = _breaker()
    for _ in range(FAILURES - 1):
        breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("answer", now=0.0) is False


def test_the_threshold_opens_it():
    breaker = _breaker()
    for _ in range(FAILURES):
        breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("answer", now=0.0) is True


def test_it_closes_again_after_the_cooldown():
    breaker = _breaker()
    for _ in range(FAILURES):
        breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("answer", now=COOLDOWN + 1) is False


def test_a_success_resets_the_count():
    """Consecutive failures, not cumulative: an upstream that fails occasionally over
    days is healthy, and treating that as a pattern would open the circuit on nothing."""
    breaker = _breaker()
    for _ in range(FAILURES - 1):
        breaker.record_failure("answer", now=0.0)
    breaker.record_success("answer")
    breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("answer", now=0.0) is False


def test_one_sick_upstream_does_not_open_another():
    breaker = _breaker()
    for _ in range(FAILURES):
        breaker.record_failure("answer", now=0.0)

    assert breaker.is_open("retrieval", now=0.0) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `cd services/gateway && uv run pytest tests/test_breaker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.breaker'`

- [ ] **Step 3: Implement `breaker.py`**

```python
"""Failing fast on an upstream that is already failing.

Without this, every gateway worker ends up blocked on the same sick service waiting for
the same timeout, and one service being down becomes the edge being down. The point is
not to protect the upstream -- it is to keep the gateway's workers available for the
three upstreams that are still healthy.
"""

from dataclasses import dataclass, field


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: float | None = None


@dataclass
class CircuitBreaker:
    """Per-upstream, because upstreams fail independently.

    Counts CONSECUTIVE failures: an upstream that fails once a day is healthy, and a
    cumulative count would eventually open the circuit on a service that never had a
    problem.
    """

    failures: int
    cooldown_seconds: float
    _states: dict[str, _State] = field(default_factory=dict)

    def is_open(self, upstream: str, now: float) -> bool:
        state = self._states.get(upstream)
        if state is None or state.opened_at is None:
            return False

        if now - state.opened_at >= self.cooldown_seconds:
            # Closed optimistically rather than after a probe: the next real request is
            # the probe, and it either succeeds or opens the circuit again.
            state.opened_at = None
            state.consecutive_failures = 0
            return False

        return True

    def record_failure(self, upstream: str, now: float) -> None:
        state = self._states.setdefault(upstream, _State())
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failures:
            state.opened_at = now

    def record_success(self, upstream: str) -> None:
        state = self._states.get(upstream)
        if state is not None:
            state.consecutive_failures = 0
            state.opened_at = None
```

- [ ] **Step 4: Wire it into `main.py`**

Add `from gateway.breaker import CircuitBreaker`, construct `_breaker = CircuitBreaker(Policy.BREAKER_FAILURES, Policy.BREAKER_COOLDOWN_SECONDS)` with a `build_breaker()` dependency, and wrap the forward call in the handler:

```python
        if _breaker.is_open(route.upstream, time.monotonic()):
            raise HTTPException(503, f"{route.upstream} is failing; not dialling it")

        try:
            response = await forward(route, request, _UPSTREAMS[route.upstream], client)
        except HTTPException as exc:
            # 502 and 504 are transport failures and count. An upstream that answered
            # with a 4xx is healthy -- it just disagreed with the caller.
            if exc.status_code in (502, 504):
                _breaker.record_failure(route.upstream, time.monotonic())
            raise

        _breaker.record_success(route.upstream)
        return response
```

- [ ] **Step 5: Run everything**

Run: `cd services/gateway && uv run pytest -q && uv run ruff check .`
Expected: all PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add services/gateway
git commit -m "stop dialling an upstream that is already failing

Without this every worker ends up blocked on the same sick service waiting for
the same timeout, and one service being down becomes the edge being down. The
breaker is not protecting the upstream; it is keeping workers available for the
three upstreams that are still healthy.

Consecutive failures rather than cumulative: a service that fails once a day is
healthy, and a running total would eventually open the circuit on one that
never had a problem. A 4xx does not count -- the upstream answered, it just
disagreed with the caller.

The circuit closes optimistically after the cooldown rather than after a probe.
The next real request is the probe."
```

---

## Task 9: Build, deploy, and point the web app at it

**Files:**
- Create: `services/gateway/Dockerfile`
- Modify: `docker-compose.yml`, `render.yaml`, `.env.example`, `.github/workflows/ci.yml`, `apps/web/app/api/ask/route.ts`, `apps/web/lib/api.ts`

**Interfaces:**
- Consumes: the gateway app from Tasks 2–8.
- Produces: a runnable gateway container on port 8000, reachable by `apps/web`.

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
# Pinned by digest: the slim tag moves, so an unpinned base means two builds of the same
# commit can differ.
FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# The image mirrors the repository layout rather than flattening it: this service's
# pyproject resolves deflect-common at ../../packages/common, and a flattened WORKDIR
# would make that path escape the filesystem root.
WORKDIR /app/services/gateway
ENV PATH="/app/services/gateway/.venv/bin:$PATH"

COPY packages/common /app/packages/common
COPY services/gateway/pyproject.toml services/gateway/uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY services/gateway .
RUN uv sync --no-dev --frozen

# Runs as a non-root user: a container process that does not need root should not have it.
RUN useradd --system --uid 10001 --create-home --home-dir /home/deflect deflect \
    && chown -R deflect /app
USER deflect

# One worker: the rate limiter is an in-process schedule, so N workers would mean N
# independent buckets and the limit would silently become N times what it says.
# RedisLeakyBucket exists and is tested for the day that stops being acceptable.
#
# --forwarded-allow-ips is explicit and EMPTY even though this service IS the edge. The
# default would have uvicorn rewrite request.client from X-Forwarded-For before the
# application runs, taking the LEFTMOST entry -- which a caller controls. The gateway
# reads the header itself and takes the rightmost, so uvicorn must be told to leave
# request.client alone rather than helpfully corrupting it first.
CMD ["gunicorn", "gateway.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "1", "--bind", "0.0.0.0:8000", \
     "--graceful-timeout", "30", "--timeout", "120", \
     "--forwarded-allow-ips", ""]
```

- [ ] **Step 2: Generate the lock file**

Run: `cd services/gateway && uv lock`
Expected: `services/gateway/uv.lock` created.

- [ ] **Step 3: Add the gateway to `docker-compose.yml`**

Insert after the `auth` service:

```yaml
  gateway:
    build:
      context: .
      dockerfile: services/gateway/Dockerfile
    environment:
      RETRIEVAL_URL: http://retrieval:8001
      ANSWER_URL: http://answer:8002
      EVALS_URL: http://evals:8003
      AUTH_URL: http://auth:8004
      REDIS_URL: redis://:${REDIS_PASSWORD:-dev-redis-password}@redis:6379/0
      ENV: ${ENV:-development}
      SERVICE_TOKEN: ${SERVICE_TOKEN:-dev-service-token}
      OPERATOR_TOKEN: ${OPERATOR_TOKEN:-dev-operator-token}
      # Nothing sits in front of the gateway locally, so there is no hop to trust and
      # edge_address falls back to the peer -- which is the right answer here.
      TRUSTED_PROXY_HOPS: 0
    ports: ["${GATEWAY_PORT:-8000}:8000"]
    depends_on:
      redis:
        condition: service_healthy
      auth:
        condition: service_started
      answer:
        condition: service_started
```

Every service keeps its published port. Local development benefits from reaching a service directly, and the gateway is not a security boundary on a laptop.

- [ ] **Step 4: Add the gateway to `render.yaml`**

Add a `deflect-gateway` `type: web` entry mirroring `deflect-auth`'s shape, with `healthCheckPath: /ready`, the four upstream URLs pointing at the other services' internal addresses, `TRUSTED_PROXY_HOPS: 1`, and `ENV` set to the literal `production` rather than `sync: false`.

**Then attempt the private split.** Change `deflect-retrieval`, `deflect-answer`, `deflect-evals` and `deflect-auth` from `type: web` to `type: pserv`.

**If `pserv` is unavailable on the account's plan:** leave all four as `type: web` and add a comment above them recording that they remain publicly reachable, that the gateway is therefore a front door beside other doors rather than the only one, and that `principal_guard` and the per-service `ENV` checks are what actually protect them. Do not silently pretend the split happened — the README paragraph in Task 10 must match whichever is true.

- [ ] **Step 5: Add `GATEWAY_URL` to `.env.example`**

```bash
# The public edge. apps/web talks to this and to nothing else.
GATEWAY_URL=http://localhost:8000
```

- [ ] **Step 6: Point `apps/web` at the gateway**

In `apps/web/app/api/ask/route.ts`, replace `ANSWER_URL` with `GATEWAY_URL` (default `http://localhost:8000`) and the fetch target with `${GATEWAY_URL}/ask`. Leave the address computation, the overwrite of `X-Forwarded-For` and the `SERVICE_TOKEN` guard exactly as they are — the BFF is still a trusted forwarder, and it is still the thing that turns a visitor into an address.

In `apps/web/lib/api.ts`, replace each per-service base URL with `GATEWAY_URL`.

- [ ] **Step 7: Add the gateway to CI**

In `.github/workflows/ci.yml`: add `gateway` to the build matrix, add port `8000` to the smoke job's wait loop, and — closing a gap the auth service left — add `8004` to that loop too, plus `docker compose exec -T auth alembic upgrade head` alongside the other migrations.

- [ ] **Step 8: Verify against the running stack**

```bash
docker compose up -d --build gateway
sleep 10
curl -s -o /dev/null -w "gateway /ready -> %{http_code}\n" localhost:8000/ready
curl -s -o /dev/null -w "public /eval-runs -> %{http_code}\n" localhost:8000/eval-runs
curl -s -o /dev/null -w "unguarded /ingest -> %{http_code}\n" -X POST localhost:8000/ingest
curl -s -o /dev/null -w "operator /ingest -> %{http_code}\n" -X POST localhost:8000/ingest \
  -H "Authorization: Bearer ${OPERATOR_TOKEN:-dev-operator-token}" \
  -H 'Content-Type: application/json' -d '{"root":"/corpus","commit_sha":"gw"}'
curl -s -o /dev/null -w "unroutable /metrics -> %{http_code}\n" localhost:8000/metrics
```
Expected: 200, 200, 401, 202, 401.

**Then verify streaming over real HTTP, which no in-process test can prove:**

```bash
curl -N -s -X POST localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question":"how do I declare a dependency"}' | head -5
```
Expected: SSE frames arriving progressively, not all at once after a pause.

**And verify the forwarded-address rule over real HTTP,** for the same reason the uvicorn fix needed it — `ASGITransport` never passes through the middleware that would rewrite `request.client`:

```bash
for i in $(seq 1 8); do
  curl -s -o /dev/null -w "%{http_code} " -X POST localhost:8000/ask \
    -H "X-Forwarded-For: 9.9.9.$i" -H 'Content-Type: application/json' \
    -d '{"question":"q"}'
done; echo
```
Expected: the burst is consumed and a 429 appears despite every request carrying a different spoofed address. If all eight return 200, `edge_address` is not being reached — stop and diagnose before continuing.

- [ ] **Step 9: Commit**

```bash
git add services/gateway docker-compose.yml render.yaml .env.example .github apps/web
git commit -m "run the gateway, and send the web app through it

--forwarded-allow-ips is explicit and empty even here, where this service is
the edge. The default has uvicorn rewrite request.client from the LEFTMOST
X-Forwarded-For entry before the application runs, and that entry is the one a
caller controls. The gateway reads the header itself and takes the rightmost,
so uvicorn has to be told to leave request.client alone rather than corrupting
it helpfully first.

The BFF keeps computing and overwriting the visitor's address: it is still a
trusted forwarder, and still the thing that turns a browser into an address.

CI gains the gateway, and also gains the auth wait and migration that the auth
sub-project left out -- a broken auth image would have sailed through the smoke
job because nothing called it."
```

---

## Task 10: Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite the architecture table and diagram**

Add the gateway as port 8000, database "none", owning "routing and edge policy". Replace the request diagram with the gateway-fronted one from the spec.

- [ ] **Step 2: Replace the Security principal table with the route table**

The gateway's table is now where the answer lives. Keep the per-service rows as a second, smaller table titled so it is clear they are defence in depth rather than the primary control.

- [ ] **Step 3: Rewrite "What it still does not need"**

It currently names "no service mesh, no Kubernetes, no distributed tracing backend". A gateway is adjacent to a mesh, so the paragraph must distinguish them: a gateway terminates public traffic at one place; a mesh governs traffic *between* services, and this project still has no use for that.

- [ ] **Step 4: Add the honesty paragraph**

State plainly: the gateway is here for breadth, this project is built for a resume, the four-origin problem is real and is what it was pointed at, and — unlike the message broker, which earned its place after two eval runs were destroyed — nothing forced this one. The README's credibility comes from having once said "this was not needed"; it survives only if it also says "this one was chosen, not forced."

- [ ] **Step 5: Document the address rule**

A short subsection under Security explaining why `client_address` and `edge_address` are two functions and not one flag, because it is the third appearance of this defect class in the project and the next person to touch it needs the reasoning.

- [ ] **Step 6: Fix the counts**

`render.yaml:1` says "Three services, three databases"; `README.md:344` and the `packages/common` logging docstring say "all three". There are now five services and four databases.

- [ ] **Step 7: Commit**

```bash
git add README.md render.yaml packages/common
git commit -m "say why the gateway is here, and what it still is not

'What it still does not need' has to distinguish a gateway from a mesh rather
than quietly drop the sentence: a gateway terminates public traffic at one
place, a mesh governs traffic between services, and this project still has no
use for the second.

The new paragraph says plainly that the gateway is here for breadth and that
nothing forced it, unlike the broker that earned its place after two destroyed
eval runs. The repository's credibility rests on having once said 'this was not
needed'; it survives only by also saying 'this one was chosen, not forced.'

Counts corrected in four places that still said three services."
```

---

## Self-Review

**Spec coverage.** Route table → Task 2. Trust model and credential passthrough → Tasks 3, 4. `edge_address` → Task 1, verified over real HTTP in Task 9. Leaky bucket, burst tuning, honest `Retry-After` → Task 7. Limits moved, daily cap stays → Task 6. 502/504/429/404 → Tasks 3, 5, 6. Circuit breaker → Task 8. Correlation ids → Task 2 (`RequestIdMiddleware`). Streaming not buffered → Task 3, and again over real HTTP in Task 9. Header hygiene → Task 3. Private split with documented fallback → Task 9. README obligations → Task 10. Out-of-scope items appear in no task, as intended.

**One deliberate deviation**, flagged above: upstream `/docs` is not proxied at all. Recorded rather than silently taken.

**Placeholders.** None. Every code step carries the code; the two prose-only steps (Task 9's `render.yaml` edit, Task 10's README rewrites) describe edits to existing files whose content is quoted or located precisely.

**Type consistency.** `Route` fields match every use. `Decision(allowed, retry_after)` is constructed in Task 7 and read in Tasks 6–7. `allowed(...)` keeps one signature across Tasks 4 and 5. `build_sessions`/`build_client`/`build_limiters`/`build_breaker` are introduced before they are overridden in tests. `InMemoryLeakyBucket(rate, period_seconds, capacity)` is called positionally in `main.py` and by keyword in tests — both match the definition.

**Known ordering hazard.** Task 6 wires `SlidingWindowLimiter` and Task 7 replaces it. That is intentional per the spec, and it is the one place where a task's output is deliberately discarded by its successor. Do not "optimise" by skipping Task 6 — the separation is what makes a behaviour regression attributable.
