# Authentication and Abuse Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three pre-deployment findings — an unauthenticated arbitrary-path `/ingest`, no authentication anywhere, and a free-to-trigger LLM-judged eval run — without breaking the database-per-service invariant.

**Architecture:** Two static bearer tokens (`SERVICE_TOKEN`, `OPERATOR_TOKEN`) carried in the environment and enforced by one shared dependency factory in `packages/common`. Guards are built at module import so an unset token aborts the import of `main.py` and the process exits before binding a port. The single anonymous endpoint, `POST /ask`, is additionally protected by an in-memory per-IP sliding window and a global daily cap derived from rows already in `traces`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, pytest + pytest-asyncio (`asyncio_mode = "auto"`), httpx, Next.js 15 App Router, vitest, ruff.

**Spec:** `docs/superpowers/specs/2026-08-05-auth-design.md`

## Global Constraints

- Python `>=3.12`. Ruff `line-length = 100`, lint rules `["E", "F", "I", "UP", "B"]`. Every task ends ruff-clean.
- **No shared tables and no cross-service joins.** This is why auth is two static tokens rather than an API-key table. Do not add a table to any service in this plan; no task here creates a migration.
- **`packages/common` receives credentials as arguments, never by reading a settings singleton.** Stated in `packages/common/src/deflect_common/llm/base.py`. Any new shared module follows it.
- Something two or more services need goes in `packages/common`; something one service needs stays in that service. Auth is shared by three, rate limiting by one.
- **Commit messages carry no attribution trailers.** The repository has zero across 41 commits and their absence was an explicit review item in the Phase 1 ledger. Match the existing style: lowercase imperative summary, body explaining *why* and what was rejected.
- Comments explain the reasoning and the rejected alternative, not the mechanics. Match the density of `services/retrieval/src/retrieval/config.py` and `services/answer/src/answer/main.py`.
- Tests inject their clock rather than sleeping, and run inside the rolled-back transaction the existing `session` fixture provides.
- Every service's suite runs against its own `_test` database and needs nothing else — no vector database, no embedding model, no provider key. Do not introduce a cross-service test dependency.

## File Structure

**Created**

| path | responsibility |
| --- | --- |
| `packages/common/src/deflect_common/auth.py` | `token_matches` predicate and `bearer_guard` dependency factory. The only place the 401 policy is expressed. |
| `packages/common/tests/test_auth.py` | Guard acceptance, rejection, and refuse-to-build-when-empty. |
| `services/answer/src/answer/ratelimit.py` | Pure limiter logic: sliding window, client address selection, daily count, reset delay. No FastAPI wiring. |
| `services/answer/tests/test_ratelimit.py` | Window expiry, address trust, daily boundary. |
| `apps/web/lib/basic-auth.ts` | `isAuthorized(header, expected)` — extracted so it is unit-testable outside the edge runtime. |
| `apps/web/lib/basic-auth.test.ts` | Credential comparison cases. |
| `apps/web/middleware.ts` | Applies `isAuthorized` to `/traces`. |

**Modified**

| path | change |
| --- | --- |
| `services/*/src/*/config.py` | `service_token`, `operator_token`; plus `corpus_root` (retrieval) and the two ask limits (answer). |
| `services/*/src/*/main.py` | Build guards at import; attach to routes per the policy table. |
| `services/retrieval/src/retrieval/main.py` | Also: confine the ingest path to `corpus_root`. |
| `services/answer/src/answer/retrieval_client.py` | Send `Authorization: Bearer`. |
| `services/evals/src/evals/answer_client.py` | Send `Authorization: Bearer`. |
| `services/*/tests/conftest.py` | Set token env vars before importing service modules. |
| `apps/web/lib/api.ts`, `apps/web/app/api/ask/route.ts` | Send tokens; forward the client address. |
| `docker-compose.yml`, `render.yaml`, `.env.example`, `.github/workflows/*.yml`, `README.md` | Configuration and documentation. |

## The policy being implemented

Copied from the spec so no task has to leave this document. **A route absent from this table has not been considered.**

| service | route | principal |
| --- | --- | --- |
| retrieval | `GET /health` | public |
| retrieval | `GET /documents` | service |
| retrieval | `POST /search` | service |
| retrieval | `POST /ingest` | operator, plus path confinement |
| answer | `GET /health` | public |
| answer | `POST /ask` | public, rate limited |
| answer | `POST /answer` | service |
| answer | `GET /traces`, `GET /traces/{trace_id}` | operator |
| evals | `GET /health` | public |
| evals | `POST /runs` | operator |
| evals | `GET /eval-runs`, `/eval-runs/diff`, `/eval-runs/{run_id}` | public |

---

## Task 1: The shared bearer guard

**Files:**
- Create: `packages/common/src/deflect_common/auth.py`
- Test: `packages/common/tests/test_auth.py`

**Dependency:** `packages/common` does not currently depend on `fastapi`, and `auth.py`
imports `Header` and `HTTPException` from it. Add `"fastapi>=0.115"` to the `dependencies`
list in `packages/common/pyproject.toml` — the same floor all three services already pin —
then `uv lock` in `packages/common` **and in all three services**. Each service resolves
`deflect-common` through its own independent lockfile, so changing common's dependencies
invalidates all three; CI runs a plain `uv sync`, which relocks silently rather than
failing, so a stale lockfile stops describing what CI installs without anything going red.

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `token_matches(expected: str, authorization: str | None) -> bool`
  - `bearer_guard(expected: str, principal: str) -> Callable[..., None]` — returns a FastAPI dependency taking `authorization: Annotated[str | None, Header()] = None` and returning `None`, raising `HTTPException(401)` otherwise. Raises `ValueError` at call time if `expected` is empty.

- [ ] **Step 1: Write the failing tests**

Create `packages/common/tests/test_auth.py`:

```python
import pytest
from deflect_common.auth import bearer_guard, token_matches
from fastapi import HTTPException


def test_a_correct_bearer_token_is_accepted():
    guard = bearer_guard("s3cret", "service")

    assert guard("Bearer s3cret") is None


@pytest.mark.parametrize(
    "header",
    [
        None,               # no header at all
        "",                 # empty header
        "Bearer wrong",     # right scheme, wrong token
        "Bearer ",          # right scheme, no token
        "s3cret",           # correct token, no scheme
        "Basic s3cret",     # correct token, wrong scheme
        "Bearer s3cret extra",  # trailing junk must not be trimmed into a match
    ],
)
def test_anything_other_than_the_exact_bearer_token_is_rejected(header):
    guard = bearer_guard("s3cret", "service")

    with pytest.raises(HTTPException) as raised:
        guard(header)

    assert raised.value.status_code == 401
    assert raised.value.headers["WWW-Authenticate"] == "Bearer"


def test_a_missing_credential_is_indistinguishable_from_a_wrong_one():
    """Telling a caller which of the two they got wrong is free information."""
    guard = bearer_guard("s3cret", "service")

    with pytest.raises(HTTPException) as missing:
        guard(None)
    with pytest.raises(HTTPException) as wrong:
        guard("Bearer wrong")

    assert missing.value.detail == wrong.value.detail
    assert missing.value.status_code == wrong.value.status_code


def test_the_principal_names_the_credential_that_was_expected():
    guard = bearer_guard("s3cret", "operator")

    with pytest.raises(HTTPException) as raised:
        guard(None)

    assert "operator" in raised.value.detail


def test_an_empty_expected_token_refuses_to_build_a_guard():
    """A service with an unset token must fail to start, not serve an open route."""
    with pytest.raises(ValueError, match="service"):
        bearer_guard("", "service")


def test_token_matches_is_exact():
    assert token_matches("s3cret", "Bearer s3cret") is True
    assert token_matches("s3cret", "Bearer s3cre") is False
    assert token_matches("s3cret", "bearer s3cret") is True  # scheme is case-insensitive
    assert token_matches("s3cret", None) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/common && uv run pytest tests/test_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deflect_common.auth'`

- [ ] **Step 3: Write auth.py**

Create `packages/common/src/deflect_common/auth.py`:

```python
"""Bearer-token guards, shared by every service.

Credentials arrive as arguments rather than from a settings singleton. This package is
imported by three services, and a library that reaches into one service's configuration
cannot be used by the other two -- the same rule llm/base.py states for provider keys.

The guard lives here rather than in each service because three copies of one
authorisation rule is exactly the drift this package exists to prevent.
"""

import hmac
from collections.abc import Callable
from typing import Annotated

from fastapi import Header, HTTPException


def token_matches(expected: str, authorization: str | None) -> bool:
    """Whether an Authorization header carries exactly `expected` as a bearer token."""
    if not authorization:
        return False

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False

    # compare_digest rather than == so a wrong token takes the same time to reject
    # regardless of how many leading characters were right.
    return hmac.compare_digest(token, expected)


def bearer_guard(expected: str, principal: str) -> Callable[..., None]:
    """Build a FastAPI dependency requiring `expected` as a bearer token.

    Raises on an empty `expected` at construction rather than at request time. Services
    build their guards at import, so an unset token aborts the import of main.py and the
    process exits before binding a port -- the same refuse-to-boot behaviour a missing
    provider key already has, and the reason a misconfigured deploy never takes traffic.

    `principal` names the expected credential in the 401 body, so a failing caller learns
    which token it should have sent without learning anything about the one it did.
    """
    if not expected:
        raise ValueError(f"the {principal} token is empty; refusing to build an open guard")

    def guard(authorization: Annotated[str | None, Header()] = None) -> None:
        if not token_matches(expected, authorization):
            raise HTTPException(
                status_code=401,
                detail=f"a {principal} credential is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return guard
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/common && uv run pytest tests/test_auth.py -q`
Expected: PASS — 12 passed (7 parametrized cases plus 5 others)

Then: `cd packages/common && uv run ruff check .` → `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add packages/common/src/deflect_common/auth.py packages/common/tests/test_auth.py
git commit -m "add a bearer guard that refuses to build without a token

Three services need one authorisation rule, so it lives beside the wire
schemas and the gate rather than in three copies that drift.

Building the guard raises on an empty token instead of deferring to
request time, so a service with an unset credential aborts at import and
never binds a port. Missing and wrong credentials return one response:
telling a caller which they got wrong is free information."
```

---

## Task 2: Authenticate the retrieval service

**Files:**
- Modify: `services/retrieval/src/retrieval/config.py`
- Modify: `services/retrieval/src/retrieval/main.py`
- Modify: `services/retrieval/tests/conftest.py`
- Test: `services/retrieval/tests/test_auth_routes.py` (create)

**Interfaces:**
- Consumes: `bearer_guard` from Task 1.
- Produces: module-level `require_service` and `require_operator` in `retrieval.main`, which tests override via `app.dependency_overrides`.

- [ ] **Step 1: Set tokens in conftest before the service imports**

`retrieval/config.py` is read at import by `db.py`, so the environment must carry tokens before any service module loads. Prepend to `services/retrieval/tests/conftest.py`, **above the existing imports**:

```python
import os

# Set before importing anything under retrieval.*: config is read at module import, and
# bearer_guard refuses to build on an empty token, so main.py would fail to import.
os.environ.setdefault("SERVICE_TOKEN", "test-service-token")
os.environ.setdefault("OPERATOR_TOKEN", "test-operator-token")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from retrieval.db import engine  # noqa: E402
```

Keep the existing `session` fixture below unchanged.

- [ ] **Step 2: Write the failing tests**

Create `services/retrieval/tests/test_auth_routes.py`:

```python
"""One case per protected route.

Guards are easy to remove by accident during a refactor. A test per route means a
deleted guard fails the build rather than silently opening an endpoint.
"""

from httpx import ASGITransport, AsyncClient

from retrieval.main import app

SERVICE = {"Authorization": "Bearer test-service-token"}
OPERATOR = {"Authorization": "Bearer test-operator-token"}


async def request(method: str, path: str, headers: dict | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers or {}, json={})


async def test_documents_requires_a_credential():
    assert (await request("GET", "/documents")).status_code == 401


async def test_search_requires_a_credential():
    assert (await request("POST", "/search")).status_code == 401


async def test_ingest_requires_a_credential():
    assert (await request("POST", "/ingest")).status_code == 401


async def test_health_stays_public_because_render_polls_it_unauthenticated():
    assert (await request("GET", "/health")).status_code == 200


async def test_the_operator_token_does_not_open_a_service_route():
    """The two principals are distinct; one credential is not a master key."""
    assert (await request("GET", "/documents", OPERATOR)).status_code == 401


async def test_the_service_token_does_not_open_ingest():
    assert (await request("POST", "/ingest", SERVICE)).status_code == 401


async def test_a_service_credential_reaches_the_documents_handler():
    assert (await request("GET", "/documents", SERVICE)).status_code == 200
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd services/retrieval && uv run pytest tests/test_auth_routes.py -q`
Expected: FAIL — the 401 assertions get 200 or 422 because no guard exists yet.

- [ ] **Step 4: Add the settings**

In `services/retrieval/src/retrieval/config.py`, add to `Settings` after `database_url`:

```python
    # Empty by default so a deployment that forgets them fails at import rather than
    # serving open routes. docker-compose supplies development values.
    service_token: str = ""
    operator_token: str = ""
```

- [ ] **Step 5: Attach the guards**

In `services/retrieval/src/retrieval/main.py`, add the import and build the guards immediately after `app = FastAPI(...)`:

```python
from deflect_common.auth import bearer_guard
from fastapi import Depends, FastAPI
```

```python
app = FastAPI(title="Deflect retrieval")

# Built at import, not in a lifespan: an unset token aborts this module and uvicorn
# exits before binding a port. Module-level names are also what dependency_overrides
# keys on when a test bypasses a guard.
_settings = get_settings()
require_service = bearer_guard(_settings.service_token, "service")
require_operator = bearer_guard(_settings.operator_token, "operator")
```

Add `from retrieval.config import get_settings` to the imports if absent.

Then attach one guard per route, leaving `/health` alone:

```python
@app.get("/documents", dependencies=[Depends(require_service)])
```
```python
@app.post("/search", dependencies=[Depends(require_service)])
```
```python
@app.post("/ingest", dependencies=[Depends(require_operator)])
```

- [ ] **Step 6: Run the whole retrieval suite**

Run:
```bash
cd services/retrieval
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_retrieval_test"
uv run pytest -q
```
Expected: PASS — 33 passed (26 existing plus 7 new).

Then `uv run ruff check .` → `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add services/retrieval/src/retrieval/config.py services/retrieval/src/retrieval/main.py \
        services/retrieval/tests/conftest.py services/retrieval/tests/test_auth_routes.py
git commit -m "require a credential on every retrieval route but health

The service holding the corpus now has no anonymous surface except the
health check Render polls, which makes private networking a bonus rather
than a load-bearing control.

Guards are built at import so an unset token aborts the module. Tests set
the tokens before importing anything under retrieval, because config is
read at import and an empty token refuses to build a guard.

Service and operator are separate principals: a test asserts one is not a
master key for the other's routes."
```

---

## Task 3: Confine the ingest path to the corpus root

**Files:**
- Modify: `services/retrieval/src/retrieval/config.py`
- Modify: `services/retrieval/src/retrieval/main.py`
- Test: `services/retrieval/tests/test_ingest_confinement.py` (create)

**Interfaces:**
- Consumes: `require_operator` from Task 2.
- Produces: `resolve_corpus_path(root: str, corpus_root: Path) -> Path` in `retrieval.main`, raising `HTTPException(400)` outside the root.

Authentication alone would leave the operator token a full container-filesystem read primitive. This is the defence-in-depth half of the finding.

- [ ] **Step 1: Write the failing tests**

Create `services/retrieval/tests/test_ingest_confinement.py`:

```python
from pathlib import Path

import pytest
from fastapi import HTTPException

from retrieval.main import resolve_corpus_path


def test_a_directory_inside_the_root_is_accepted(tmp_path):
    inside = tmp_path / "en" / "docs"
    inside.mkdir(parents=True)

    assert resolve_corpus_path(str(inside), tmp_path) == inside.resolve()


def test_the_root_itself_is_accepted(tmp_path):
    assert resolve_corpus_path(str(tmp_path), tmp_path) == tmp_path.resolve()


@pytest.mark.parametrize("escape", ["..", "../../etc", "sub/../.."])
def test_a_relative_path_climbing_out_of_the_root_is_rejected(tmp_path, escape):
    (tmp_path / "sub").mkdir()

    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path(str(tmp_path / escape), tmp_path)

    assert raised.value.status_code == 400


def test_an_absolute_path_outside_the_root_is_rejected(tmp_path):
    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path("/etc", tmp_path)

    assert raised.value.status_code == 400


def test_a_symlink_pointing_out_of_the_root_is_rejected(tmp_path):
    """Rejection happens after resolve(), so a symlink escape is caught too."""
    outside = tmp_path.parent / "outside-the-corpus"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "escape"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path(str(link), tmp_path)

    assert raised.value.status_code == 400


def test_the_rejection_never_echoes_the_path_back(tmp_path):
    """Echoing it would let the endpoint be used to map the container filesystem."""
    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path("/etc/passwd", tmp_path)

    assert "passwd" not in raised.value.detail
    assert "/etc" not in raised.value.detail


def test_a_prefix_collision_is_not_treated_as_containment(tmp_path):
    """/corpus-secrets must not pass because it starts with /corpus."""
    root = tmp_path / "corpus"
    root.mkdir()
    sibling = tmp_path / "corpus-secrets"
    sibling.mkdir()

    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path(str(sibling), root)

    assert raised.value.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/retrieval && uv run pytest tests/test_ingest_confinement.py -q`
Expected: FAIL — `ImportError: cannot import name 'resolve_corpus_path'`

- [ ] **Step 3: Add the corpus_root setting**

In `services/retrieval/src/retrieval/config.py`, add to `Settings`:

```python
    # Ingest resolves its requested root against this and refuses anything outside it.
    # An operator token that leaked would otherwise be a filesystem read primitive:
    # /ingest reads a directory and /search hands the contents back.
    corpus_root: Path = Path("/corpus")
```

Add `from pathlib import Path` to the file's imports.

- [ ] **Step 4: Write resolve_corpus_path and use it**

In `services/retrieval/src/retrieval/main.py`, add above the `/ingest` route:

```python
def resolve_corpus_path(root: str, corpus_root: Path) -> Path:
    """Resolve a requested ingest root, refusing anything outside `corpus_root`.

    Resolution happens before the check so a symlink pointing out of the corpus is
    caught, not only a literal `..`. is_relative_to compares path components rather
    than string prefixes, so a sibling named corpus-secrets does not pass as /corpus.
    """
    requested = Path(root).resolve()
    allowed = corpus_root.resolve()

    if requested != allowed and not requested.is_relative_to(allowed):
        # The rejected path is deliberately absent from the message: echoing it back
        # would turn this endpoint into a way to map the container filesystem.
        raise HTTPException(status_code=400, detail="ingest root is outside the corpus root")

    return requested
```

Add `HTTPException` to the `fastapi` import line.

Replace the `/ingest` handler body:

```python
@app.post("/ingest", dependencies=[Depends(require_operator)])
async def ingest(request: IngestRequest, session: SessionDep) -> IngestResponse:
    root = resolve_corpus_path(request.root, get_settings().corpus_root)
    count = await ingest_directory(session, root, request.commit_sha)
    await session.commit()
    return IngestResponse(chunks=count)
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
cd services/retrieval
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_retrieval_test"
uv run pytest -q && uv run ruff check .
```
Expected: PASS — 42 passed (26 existing, 7 from Task 2, 9 new). Ruff clean.

- [ ] **Step 6: Commit**

```bash
git add services/retrieval/src/retrieval/config.py services/retrieval/src/retrieval/main.py \
        services/retrieval/tests/test_ingest_confinement.py
git commit -m "confine ingest to a configured corpus root

A credential alone would leave the operator token a filesystem read
primitive: /ingest reads any directory and /search hands the contents
back. Ten lines of confinement means a leaked token still cannot read
/etc or the source tree.

The check runs after resolve() so a symlink out of the corpus is caught,
not only a literal '..', and uses is_relative_to rather than a string
prefix so a sibling named corpus-secrets does not pass as /corpus.

The error omits the rejected path: echoing it would turn the endpoint
into a way to map the container filesystem by probing."
```

---

## Task 4: Authenticate the answer service and its retrieval client

**Files:**
- Modify: `services/answer/src/answer/config.py`
- Modify: `services/answer/src/answer/main.py`
- Modify: `services/answer/src/answer/retrieval_client.py`
- Modify: `services/answer/tests/conftest.py`
- Test: `services/answer/tests/test_auth_routes.py` (create)

**Interfaces:**
- Consumes: `bearer_guard` from Task 1.
- Produces: `require_service`, `require_operator` in `answer.main`; `RetrievalClient(base_url: str, token: str, timeout: float = 30.0)`.

`POST /ask` is left open here; Task 7 adds its rate limiting.

- [ ] **Step 1: Set tokens in conftest**

Prepend to `services/answer/tests/conftest.py`, above the existing imports:

```python
import os

# Set before importing anything under answer.*: config is read at import and
# bearer_guard refuses to build on an empty token.
os.environ.setdefault("SERVICE_TOKEN", "test-service-token")
os.environ.setdefault("OPERATOR_TOKEN", "test-operator-token")
```

Add `# noqa: E402` to each existing import line beneath it.

- [ ] **Step 2: Write the failing tests**

Create `services/answer/tests/test_auth_routes.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd services/answer && uv run pytest tests/test_auth_routes.py -q`
Expected: FAIL — the 401 assertions get 422.

- [ ] **Step 4: Add the settings**

In `services/answer/src/answer/config.py`, add after `retrieval_url`:

```python
    # Empty by default so a deployment that forgets them fails at import rather than
    # serving open routes. docker-compose supplies development values.
    service_token: str = ""
    operator_token: str = ""
```

- [ ] **Step 5: Build the guards and attach them**

In `services/answer/src/answer/main.py`, add to the imports:

```python
from deflect_common.auth import bearer_guard
```

After `router = APIRouter()`, add:

```python
# Built at import, not in the lifespan: an unset token aborts this module and uvicorn
# exits before binding a port. Module-level names are what dependency_overrides keys on.
require_service = bearer_guard(get_settings().service_token, "service")
require_operator = bearer_guard(get_settings().operator_token, "operator")
```

Attach them:

```python
@router.post("/answer", dependencies=[Depends(require_service)])
```
```python
@router.get("/traces", dependencies=[Depends(require_operator)])
```
```python
@router.get("/traces/{trace_id}", dependencies=[Depends(require_operator)])
```

Leave `/health` and `/ask` undecorated.

- [ ] **Step 6: Send the token from RetrievalClient**

In `services/answer/src/answer/retrieval_client.py`, change the constructor and the call:

```python
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def search(self, request: SearchRequest) -> SearchResponse:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/search",
                    json=request.model_dump(),
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as cause:
            # A 401 from retrieval is a misconfigured deployment, not an outage. Reporting
            # it as 503 would tell an operator to wait for a service that will never
            # recover on its own.
            if cause.response.status_code == 401:
                raise HTTPException(
                    status_code=500, detail="retrieval rejected this service's credential"
                ) from cause
            raise HTTPException(
                status_code=503, detail=f"retrieval service unavailable: {cause}"
            ) from cause
        except httpx.HTTPError as cause:
            raise HTTPException(
                status_code=503, detail=f"retrieval service unavailable: {cause}"
            ) from cause
```

`httpx.HTTPStatusError` subclasses `httpx.HTTPError`, so the narrower clause must come first.

Update the construction site in `main.py`:

```python
def build_retrieval() -> RetrievalClient:
    settings = get_settings()
    return RetrievalClient(settings.retrieval_url, settings.service_token)
```

- [ ] **Step 7: Run the whole answer suite**

Run:
```bash
cd services/answer
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_answer_test"
export GEMINI_API_KEY=test-key
uv run pytest -q && uv run ruff check .
```
Expected: PASS — 27 passed (20 existing plus 7 new). Ruff clean.

If any existing test constructs `RetrievalClient` positionally with one argument, add the token argument there.

- [ ] **Step 8: Commit**

```bash
git add services/answer/src/answer/config.py services/answer/src/answer/main.py \
        services/answer/src/answer/retrieval_client.py services/answer/tests/conftest.py \
        services/answer/tests/test_auth_routes.py
git commit -m "require credentials on /answer and the traces surface

Traces record every visitor's question along with what it cost, so they
are operator-only on an open demo. /answer is service-only; evals is its
only caller.

/ask stays anonymous, and a test asserts so -- a 401 there would mean the
public surface had silently closed.

RetrievalClient now separates a 401 from an outage. Reporting a rejected
credential as 503 would tell an operator to wait for a service that will
never recover on its own."
```

---

## Task 5: Authenticate the evals service and its answer client

**Files:**
- Modify: `services/evals/src/evals/config.py`
- Modify: `services/evals/src/evals/main.py`
- Modify: `services/evals/src/evals/answer_client.py`
- Modify: `services/evals/tests/conftest.py`
- Test: `services/evals/tests/test_auth_routes.py` (create)

**Interfaces:**
- Consumes: `bearer_guard` from Task 1.
- Produces: `require_operator` in `evals.main`; `AnswerClient(base_url: str, token: str, timeout: float = 120.0)`.

The eval dashboard reads stay public — they are the surface worth showing off, and they contain no visitor data.

- [ ] **Step 1: Set tokens in conftest**

Prepend to `services/evals/tests/conftest.py`, above the existing imports:

```python
import os

os.environ.setdefault("SERVICE_TOKEN", "test-service-token")
os.environ.setdefault("OPERATOR_TOKEN", "test-operator-token")
```

Add `# noqa: E402` to each existing import line beneath it.

- [ ] **Step 2: Write the failing tests**

Create `services/evals/tests/test_auth_routes.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd services/evals && uv run pytest tests/test_auth_routes.py -q`
Expected: FAIL — `POST /runs` returns 422 rather than 401.

- [ ] **Step 4: Add the settings**

In `services/evals/src/evals/config.py`, add after `answer_url`:

```python
    # Empty by default so a deployment that forgets them fails at import rather than
    # serving open routes. docker-compose supplies development values.
    service_token: str = ""
    operator_token: str = ""
```

- [ ] **Step 5: Build the guard and attach it**

In `services/evals/src/evals/main.py`, add to the imports:

```python
from deflect_common.auth import bearer_guard
```

After `router = APIRouter()`, add:

```python
# Built at import so an unset token aborts this module rather than leaving the most
# expensive operation in the system reachable by anyone.
require_operator = bearer_guard(get_settings().operator_token, "operator")
```

Attach it to the one write route:

```python
@router.post("/runs", dependencies=[Depends(require_operator)])
```

Leave `/health` and every `/eval-runs*` route undecorated.

- [ ] **Step 6: Send the token from AnswerClient**

In `services/evals/src/evals/answer_client.py`:

```python
    def __init__(self, base_url: str, token: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def answer(self, request: AnswerRequest) -> AnswerResponse:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/answer",
                    json=request.model_dump(),
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as cause:
            # Matches RetrievalClient: a rejected credential is a misconfiguration and
            # will not heal, so it is not disguised as a transient outage.
            if cause.response.status_code == 401:
                raise HTTPException(
                    status_code=500, detail="answer rejected this service's credential"
                ) from cause
            raise HTTPException(
                status_code=503, detail=f"answer service unavailable: {cause}"
            ) from cause
        except httpx.HTTPError as cause:
            raise HTTPException(
                status_code=503, detail=f"answer service unavailable: {cause}"
            ) from cause
```

Update the construction site in `main.py`:

```python
def build_answer_client() -> AnswerClient:
    settings = get_settings()
    return AnswerClient(settings.answer_url, settings.service_token)
```

- [ ] **Step 7: Run the whole evals suite**

Run:
```bash
cd services/evals
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test"
export GEMINI_API_KEY=test-key
uv run pytest -q && uv run ruff check .
```
Expected: PASS — 44 passed (38 existing plus 6 new). Ruff clean.

If any existing test constructs `AnswerClient` with one positional argument, add the token.

- [ ] **Step 8: Commit**

```bash
git add services/evals/src/evals/config.py services/evals/src/evals/main.py \
        services/evals/src/evals/answer_client.py services/evals/tests/conftest.py \
        services/evals/tests/test_auth_routes.py
git commit -m "require an operator credential to start an eval run

An LLM-judged run over the golden dataset was the most expensive
operation in the system and the only unguarded write.

The eval-runs reads stay public: they hold no visitor data and the
dashboard is the part of this project most worth showing. A test asserts
each stays reachable, so closing one later is a deliberate act."
```

---

## Task 6: The sliding window limiter

**Files:**
- Create: `services/answer/src/answer/ratelimit.py`
- Test: `services/answer/tests/test_ratelimit.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SlidingWindowLimiter(limit: int, window_seconds: float)` with `.check(key: str, now: float) -> bool`
  - `client_address(request: Request, trust_forwarded: bool) -> str`
  - `seconds_until_utc_midnight(now: datetime) -> int`

Pure logic only. Task 7 does the wiring and the database count.

- [ ] **Step 1: Write the failing tests**

Create `services/answer/tests/test_ratelimit.py`:

```python
from datetime import UTC, datetime

from answer.ratelimit import SlidingWindowLimiter, client_address, seconds_until_utc_midnight


def test_requests_up_to_the_limit_are_allowed():
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)

    assert [limiter.check("ip", now=0.0) for _ in range(3)] == [True, True, True]


def test_the_request_past_the_limit_is_rejected():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    limiter.check("ip", now=0.0)
    limiter.check("ip", now=0.0)

    assert limiter.check("ip", now=0.0) is False


def test_the_allowance_returns_once_the_window_passes():
    """Driven by the injected clock, so the test does not sleep for a minute."""
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("ip", now=0.0)

    assert limiter.check("ip", now=59.0) is False
    assert limiter.check("ip", now=61.0) is True


def test_the_window_slides_rather_than_resetting_on_a_boundary():
    """A fixed bucket would allow 2x the limit across a boundary. This must not."""
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    limiter.check("ip", now=0.0)
    limiter.check("ip", now=59.0)

    assert limiter.check("ip", now=59.5) is False
    assert limiter.check("ip", now=60.5) is True   # the 0.0 entry has aged out
    assert limiter.check("ip", now=60.6) is False  # the 59.0 entry has not


def test_addresses_are_counted_separately():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("first", now=0.0)

    assert limiter.check("second", now=0.0) is True


def test_expired_entries_do_not_accumulate_forever():
    """Without eviction this dict is an unbounded memory leak on a public endpoint."""
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    for i in range(500):
        limiter.check(f"ip-{i}", now=float(i))

    assert limiter.tracked_keys(now=10_000.0) == 0


class FakeRequest:
    def __init__(self, headers: dict, host: str | None):
        self.headers = headers
        self.client = type("C", (), {"host": host})() if host else None


def test_an_authenticated_caller_is_trusted_about_the_forwarded_address():
    request = FakeRequest({"x-forwarded-for": "203.0.113.7, 70.41.3.18"}, "10.0.0.1")

    assert client_address(request, trust_forwarded=True) == "203.0.113.7"


def test_an_anonymous_caller_is_limited_on_its_own_socket_address():
    """Otherwise a direct caller mints a fresh address per request and evades the limit."""
    request = FakeRequest({"x-forwarded-for": "1.2.3.4"}, "10.0.0.1")

    assert client_address(request, trust_forwarded=False) == "10.0.0.1"


def test_a_trusted_caller_sending_no_forwarded_header_falls_back_to_the_socket():
    request = FakeRequest({}, "10.0.0.1")

    assert client_address(request, trust_forwarded=True) == "10.0.0.1"


def test_a_request_with_no_client_is_still_keyable():
    request = FakeRequest({}, None)

    assert client_address(request, trust_forwarded=False) == "unknown"


def test_seconds_until_midnight_counts_down_within_the_day():
    now = datetime(2026, 8, 5, 23, 59, 30, tzinfo=UTC)

    assert seconds_until_utc_midnight(now) == 30


def test_seconds_until_midnight_is_a_full_day_at_midnight():
    now = datetime(2026, 8, 5, 0, 0, 0, tzinfo=UTC)

    assert seconds_until_utc_midnight(now) == 86_400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/answer && uv run pytest tests/test_ratelimit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'answer.ratelimit'`

- [ ] **Step 3: Write ratelimit.py**

Create `services/answer/src/answer/ratelimit.py`:

```python
"""Abuse control for the one anonymous endpoint.

This lives in the answer service rather than packages/common because exactly one
service needs it. Three services share authentication; only this one takes public
traffic.

Two layers doing two different jobs. The per-address window stops one script. The
daily cap is the only thing that bounds the provider bill, because a botnet has many
real addresses. Conflating them would leave the deployment believing it is protected
when only half of it is.
"""

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from fastapi import Request


class SlidingWindowLimiter:
    """Allow `limit` events per key per `window_seconds`.

    In-memory and per-process, so the allowance resets on restart and each instance
    counts separately. Documented rather than solved: making a throttle survive a
    restart means running Redis, and the control that actually bounds cost -- the daily
    cap -- already survives because it counts rows in the database.

    `now` is a parameter rather than a call to the clock so window expiry is tested
    without sleeping.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, now: float) -> bool:
        """Record an event and report whether it was within the allowance."""
        self._evict(now)

        events = self._events[key]
        if len(events) >= self._limit:
            return False

        events.append(now)
        return True

    def tracked_keys(self, now: float) -> int:
        """How many keys still hold unexpired events. Exposed so a test can prove the
        dict does not grow without bound on a public endpoint."""
        self._evict(now)
        return len(self._events)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        for key in list(self._events):
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            # Drop the key entirely, not just its events: one entry per address ever
            # seen would be an unbounded leak on an endpoint open to the internet.
            if not events:
                del self._events[key]


def client_address(request: Request, trust_forwarded: bool) -> str:
    """The address to rate limit on.

    A forwarded address is trusted only from a caller that presented the service token.
    The web BFF sees the real visitor and forwards it; an anonymous caller reaching this
    service directly could otherwise mint a fresh address per request and make the
    per-address limit meaningless.
    """
    if trust_forwarded:
        forwarded = request.headers.get("x-forwarded-for", "")
        # Leftmost is the originating client; the rest are proxy hops.
        first = forwarded.split(",")[0].strip()
        if first:
            return first

    return request.client.host if request.client else "unknown"


def seconds_until_utc_midnight(now: datetime) -> int:
    """How long until the daily allowance resets, for a Retry-After header."""
    midnight = (now.astimezone(UTC) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((midnight - now.astimezone(UTC)).total_seconds())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/answer && uv run pytest tests/test_ratelimit.py -q`
Expected: PASS — 12 passed.

Then `uv run ruff check .` → `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add services/answer/src/answer/ratelimit.py services/answer/tests/test_ratelimit.py
git commit -m "add a sliding window limiter that evicts what it stops tracking

Lives in the answer service, not packages/common: three services share
authentication, one takes public traffic.

The window slides rather than bucketing, so a caller cannot spend twice
the allowance either side of a boundary. Keys are deleted once their
events expire -- one entry per address ever seen would be an unbounded
leak on an endpoint open to the internet.

now is injected, so window expiry is tested without sleeping.

A forwarded address is trusted only from a caller holding the service
token; an anonymous caller would otherwise mint a fresh address per
request and make the limit meaningless."
```

---

## Task 7: Enforce the limits on /ask

**Files:**
- Modify: `services/answer/src/answer/config.py`
- Modify: `services/answer/src/answer/ratelimit.py`
- Modify: `services/answer/src/answer/main.py`
- Test: `services/answer/tests/test_ask_limits.py` (create)

**Interfaces:**
- Consumes: `SlidingWindowLimiter`, `client_address`, `seconds_until_utc_midnight` from Task 6; `token_matches` from Task 1; the `session` and `make_app` fixtures from `services/answer/tests/conftest.py`.
- Produces: `questions_today(session: AsyncSession, now: datetime) -> int` in `answer.ratelimit`; `enforce_ask_limits` dependency in `answer.main`.

- [ ] **Step 1: Write the failing tests**

Create `services/answer/tests/test_ask_limits.py`:

```python
from datetime import UTC, datetime, timedelta

from doubles import FakeRetrieval
from httpx import ASGITransport, AsyncClient

from answer.models import Trace
from answer.ratelimit import questions_today


def _trace(created_at: datetime) -> Trace:
    return Trace(
        question="q",
        answer="a",
        escalated=False,
        reason=None,
        top_score=5.0,
        margin=1.0,
        retrieved=[],
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        model="fake",
        prompt_version="v1",
        latency_ms=1,
        min_top_score=2.0,
        min_margin=0.0,
        created_at=created_at,
    )


async def test_only_todays_questions_count_towards_the_cap(session):
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    session.add_all(
        [
            _trace(now),
            _trace(now - timedelta(hours=6)),           # earlier today
            _trace(now - timedelta(days=1)),            # yesterday
        ]
    )
    await session.flush()

    assert await questions_today(session, now) == 2


async def test_a_question_just_after_midnight_starts_a_fresh_day(session):
    now = datetime(2026, 8, 5, 0, 0, 1, tzinfo=UTC)
    session.add_all([_trace(now), _trace(now - timedelta(seconds=2))])
    await session.flush()

    assert await questions_today(session, now) == 1


async def test_the_daily_cap_rejects_once_the_budget_is_spent(
    session, make_app, hits, answer_payload, monkeypatch
):
    """Fails closed: the counter is a database query, and if that is broken the ask
    path is already broken, so refusing is honest."""
    monkeypatch.setenv("ASK_DAILY_LIMIT", "1")
    from answer.config import get_settings

    get_settings.cache_clear()

    app = make_app([answer_payload("Use Depends.", [1], True)] * 2, FakeRetrieval(hits))
    session.add(_trace(datetime.now(UTC)))
    await session.flush()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "anything"})

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    get_settings.cache_clear()


async def test_a_question_within_budget_is_still_answered(
    session, make_app, hits, answer_payload
):
    app = make_app([answer_payload("Use Depends.", [1], True)], FakeRetrieval(hits))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/ask", json={"question": "how do I declare one"})

    assert response.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/answer && uv run pytest tests/test_ask_limits.py -q`
Expected: FAIL — `ImportError: cannot import name 'questions_today'`

- [ ] **Step 3: Add the limit settings**

In `services/answer/src/answer/config.py`, add after `web_origin`:

```python
    # Derived together, not independently. At the gemini-2.0-flash prices in
    # telemetry.py a five-chunk question costs about $0.00055, so 500 a day caps a
    # fully abused day near $0.28 -- roughly $8.50 a month sustained, an order of
    # magnitude above real demo traffic.
    #
    # 20 an hour over 24 hours is 480, just under the daily ceiling, so no single
    # address can exhaust the budget in a day. Raising the hourly limit past 21 breaks
    # that property; re-derive both together if either changes.
    ask_rate_limit_per_hour: int = 20
    ask_daily_limit: int = 500
```

- [ ] **Step 4: Add questions_today to ratelimit.py**

Append to `services/answer/src/answer/ratelimit.py`:

```python
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from answer.models import Trace


async def questions_today(session: AsyncSession, now: datetime) -> int:
    """How many questions have been answered since UTC midnight.

    Counts trace rows rather than summing cost_usd. Summing would bound the bill more
    directly, but estimate_cost returns 0.0 for any model absent from PRICING, so
    pointing generation_model at an unpriced model would silently turn the cap into no
    cap at all -- a control that fails open on an ordinary configuration change. A row
    count cannot do that.

    No new table and no migration: the answer service already writes one row per
    question, so the day's counter already exists and survives a restart.
    """
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    statement = select(func.count()).select_from(Trace).where(Trace.created_at >= midnight)
    return (await session.execute(statement)).scalar_one()
```

Move the two new `sqlalchemy` imports and the `Trace` import to the top of the file with the others; ruff's `I` rule will otherwise fail.

- [ ] **Step 5: Wire the dependency onto /ask**

In `services/answer/src/answer/main.py`, add to the imports:

```python
import time
from datetime import UTC, datetime

from deflect_common.auth import bearer_guard, token_matches
from fastapi import Header

from answer.ratelimit import (
    SlidingWindowLimiter,
    client_address,
    questions_today,
    seconds_until_utc_midnight,
)
```

After the guards, add the limiter and the dependency:

```python
# One limiter for the process. Per-process state means each instance counts separately
# and a redeploy grants a fresh allowance; see ratelimit.py for why that is accepted.
_ask_limiter = SlidingWindowLimiter(
    limit=get_settings().ask_rate_limit_per_hour, window_seconds=3600
)


async def enforce_ask_limits(
    http: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Reject an abusive question before it reaches a model.

    Ordered cheapest first: the per-address window is a dict lookup, the daily cap is a
    database query.
    """
    settings = get_settings()
    trusted = token_matches(settings.service_token, authorization)
    address = client_address(http, trust_forwarded=trusted)

    if not _ask_limiter.check(address, time.monotonic()):
        raise HTTPException(
            status_code=429,
            detail="too many questions from this address",
            headers={"Retry-After": "3600"},
        )

    now = datetime.now(UTC)
    if await questions_today(session, now) >= settings.ask_daily_limit:
        raise HTTPException(
            status_code=429,
            detail="this demo's daily question budget is spent; it resets at UTC midnight",
            headers={"Retry-After": str(seconds_until_utc_midnight(now))},
        )
```

Attach it. **The body parameter is already named `request`, so the HTTP request must be named differently** — `http: Request`, distinguished by its type annotation:

```python
@router.post("/ask", dependencies=[Depends(enforce_ask_limits)])
async def ask(
    request: AnswerRequest,
    session: SessionDep,
    client: ClientDep,
    retrieval: RetrievalDep,
) -> StreamingResponse:
```

`Request` is already imported in `main.py` for `build_client`.

- [ ] **Step 6: Run the whole answer suite**

Run:
```bash
cd services/answer
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_answer_test"
export GEMINI_API_KEY=test-key
uv run pytest -q && uv run ruff check .
```
Expected: PASS — 43 passed (20 existing, 7 from Task 4, 12 from Task 6, 4 new). Ruff clean.

If `test_the_daily_cap_rejects_once_the_budget_is_spent` leaks its setting into later tests, confirm both `get_settings.cache_clear()` calls run; the second is deliberately outside any fixture teardown because the test asserts before it.

- [ ] **Step 7: Commit**

```bash
git add services/answer/src/answer/config.py services/answer/src/answer/ratelimit.py \
        services/answer/src/answer/main.py services/answer/tests/test_ask_limits.py
git commit -m "cap what an open /ask can spend in a day

The daily counter is a count of trace rows, not a sum of cost_usd.
Summing would bound the bill more directly, but estimate_cost returns 0.0
for models absent from PRICING, so changing generation_model would
silently turn the cap into no cap -- a control that fails open on an
ordinary configuration change.

No new table: one trace row per question already exists, so the counter
survives restarts for free.

The two limits are derived together. 20 an hour over 24 hours is 480,
just under the 500 daily ceiling, so no single address can exhaust the
budget in a day."
```

---

## Task 8: Carry the tokens from the web application

**Files:**
- Create: `apps/web/lib/basic-auth.ts`, `apps/web/lib/basic-auth.test.ts`, `apps/web/middleware.ts`
- Modify: `apps/web/lib/api.ts`, `apps/web/app/api/ask/route.ts`

**Interfaces:**
- Consumes: the running services from Tasks 2–7.
- Produces: `isAuthorized(header: string | null, expected: string): boolean` in `apps/web/lib/basic-auth.ts`.

- [ ] **Step 1: Write the failing tests**

Create `apps/web/lib/basic-auth.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { isAuthorized } from "./basic-auth";

const encode = (user: string, password: string) =>
  `Basic ${Buffer.from(`${user}:${password}`).toString("base64")}`;

describe("isAuthorized", () => {
  it("accepts the expected password regardless of username", () => {
    expect(isAuthorized(encode("operator", "s3cret"), "s3cret")).toBe(true);
    expect(isAuthorized(encode("", "s3cret"), "s3cret")).toBe(true);
  });

  it("rejects a wrong password", () => {
    expect(isAuthorized(encode("operator", "wrong"), "s3cret")).toBe(false);
  });

  it("rejects a missing header", () => {
    expect(isAuthorized(null, "s3cret")).toBe(false);
  });

  it("rejects a non-Basic scheme", () => {
    expect(isAuthorized("Bearer s3cret", "s3cret")).toBe(false);
  });

  it("rejects malformed base64 without throwing", () => {
    expect(isAuthorized("Basic !!!not-base64!!!", "s3cret")).toBe(false);
  });

  it("rejects a password containing the expected one as a prefix", () => {
    expect(isAuthorized(encode("operator", "s3cretplus"), "s3cret")).toBe(false);
  });

  it("keeps a colon in the password intact", () => {
    expect(isAuthorized(encode("operator", "a:b"), "a:b")).toBe(true);
  });

  it("never authorises against an empty expected password", () => {
    expect(isAuthorized(encode("operator", ""), "")).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/web && npx vitest run lib/basic-auth.test.ts`
Expected: FAIL — cannot resolve `./basic-auth`

- [ ] **Step 3: Write basic-auth.ts**

Create `apps/web/lib/basic-auth.ts`:

```ts
// Extracted from middleware.ts so it can be unit-tested outside the edge runtime.
// The traces surface records every visitor's question and what it cost, so it is the
// one page that must not be public on an open demo.

function constantTimeEquals(a: string, b: string): boolean {
  // Length is compared first and leaks, which is acceptable: the length of an
  // operator token is not the secret. The loop keeps the content comparison
  // independent of how many leading characters matched.
  if (a.length !== b.length) return false;

  let difference = 0;
  for (let i = 0; i < a.length; i++) {
    difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return difference === 0;
}

export function isAuthorized(header: string | null, expected: string): boolean {
  // An unset OPERATOR_TOKEN must never authorise anyone. Without this an empty
  // environment variable would make every empty password correct.
  if (!expected) return false;
  if (!header) return false;

  const [scheme, encoded] = header.split(" ");
  if (scheme !== "Basic" || !encoded) return false;

  let decoded: string;
  try {
    decoded = atob(encoded);
  } catch {
    return false;
  }

  // Only the first colon separates user from password; the rest belong to the password.
  const separator = decoded.indexOf(":");
  if (separator === -1) return false;

  return constantTimeEquals(decoded.slice(separator + 1), expected);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/web && npx vitest run lib/basic-auth.test.ts`
Expected: PASS — 8 passed.

- [ ] **Step 5: Write the middleware**

Create `apps/web/middleware.ts`:

```ts
import { NextResponse, type NextRequest } from "next/server";
import { isAuthorized } from "@/lib/basic-auth";

// Basic auth rather than a sign-in page: the browser renders the credential prompt
// itself, so there is no session store and no cookie to get wrong. A login flow for a
// single operator is machinery maintained forever to avoid one browser prompt.
export function middleware(request: NextRequest) {
  if (isAuthorized(request.headers.get("authorization"), process.env.OPERATOR_TOKEN ?? "")) {
    return NextResponse.next();
  }

  return new NextResponse("authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="deflect traces"' },
  });
}

// Both forms: the bare path is not covered by the :path* pattern.
export const config = { matcher: ["/traces", "/traces/:path*"] };
```

- [ ] **Step 6: Send the tokens upstream**

In `apps/web/lib/api.ts`, replace `getJSONFrom`, `getFromAnswer` and `getFromEvals`:

```ts
const ANSWER_URL = process.env.ANSWER_URL ?? "http://localhost:8002";
const EVALS_URL = process.env.EVALS_URL ?? "http://localhost:8003";

async function getJSONFrom<T>(base: string, path: string, token?: string): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    cache: "no-store",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}

// Traces are operator-only, so this server component carries the operator token. The
// browser never sees it.
export function getFromAnswer<T>(path: string): Promise<T> {
  return getJSONFrom<T>(ANSWER_URL, path, process.env.OPERATOR_TOKEN);
}

// The eval dashboard is public and needs no credential.
export function getFromEvals<T>(path: string): Promise<T> {
  return getJSONFrom<T>(EVALS_URL, path);
}
```

In `apps/web/app/api/ask/route.ts`:

```ts
const ANSWER_URL = process.env.ANSWER_URL ?? "http://localhost:8002";
const SERVICE_TOKEN = process.env.SERVICE_TOKEN ?? "";

// The browser never holds a provider key. The stream is proxied so the model is
// only ever reachable from the FastAPI service.
export async function POST(request: Request) {
  // The service token is not what authorises the question -- /ask is open. It is what
  // makes the forwarded address believable: without it the answer service would fall
  // back to this proxy's own address and rate limit every visitor as one caller.
  const forwardedFor = request.headers.get("x-forwarded-for");

  const upstream = await fetch(`${ANSWER_URL}/ask`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${SERVICE_TOKEN}`,
      ...(forwardedFor ? { "X-Forwarded-For": forwardedFor } : {}),
    },
    body: await request.text(),
  });

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
    },
  });
}
```

- [ ] **Step 7: Run the web checks**

Run:
```bash
cd apps/web
npm test && npm run lint && npm run build
```
Expected: 16 tests passed (8 existing plus 8 new). Lint clean. Build succeeds and lists `ƒ Middleware` in the route output.

- [ ] **Step 8: Commit**

```bash
git add apps/web/lib/basic-auth.ts apps/web/lib/basic-auth.test.ts apps/web/middleware.ts \
        apps/web/lib/api.ts apps/web/app/api/ask/route.ts
git commit -m "gate the traces page and forward the real client address

Basic auth on /traces: the browser renders the prompt itself, so there is
no session store and no cookie to get wrong. A sign-in flow for one
operator is machinery maintained forever to save a browser prompt.

The credential check is extracted from the middleware so it can be tested
outside the edge runtime, and an unset OPERATOR_TOKEN authorises nobody
rather than making every empty password correct.

The ask proxy sends the service token not to authorise the question --
/ask is open -- but to make its forwarded address believable. Without it
the answer service would rate limit every visitor as one caller."
```

---

## Task 9: Configuration, CI, and documentation

**Files:**
- Modify: `docker-compose.yml`, `render.yaml`, `.env.example`
- Modify: `.github/workflows/ci.yml`, `.github/workflows/nightly-evals.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: every setting introduced in Tasks 2–8.
- Produces: a stack that starts and a CI run that passes.

- [ ] **Step 1: Add the tokens to docker-compose.yml**

Add to **each** of the three service `environment:` blocks:

```yaml
      SERVICE_TOKEN: ${SERVICE_TOKEN:-dev-service-token}
      OPERATOR_TOKEN: ${OPERATOR_TOKEN:-dev-operator-token}
```

And to the `retrieval` block only:

```yaml
      CORPUS_ROOT: /corpus
```

Add a comment above the `db` service:

```yaml
# The token defaults are development values and exist so the stack starts with no .env.
# Render supplies real ones per service; nothing here is a production credential.
```

- [ ] **Step 2: Add the tokens to render.yaml**

Add to all three services' `envVars`:

```yaml
      # Both services on a call must carry the same SERVICE_TOKEN, so these are set
      # by hand rather than with generateValue, which would mint a different one each.
      - key: SERVICE_TOKEN
        sync: false
      - key: OPERATOR_TOKEN
        sync: false
```

And to `deflect-retrieval` only:

```yaml
      - key: CORPUS_ROOT
        sync: false
```

- [ ] **Step 3: Update .env.example**

```bash
DATABASE_URL=postgresql+asyncpg://deflect:deflect@localhost:5432/deflect
GEMINI_API_KEY=
LLM_PROVIDER=gemini
WEB_ORIGIN=http://localhost:3000

# Service-to-service and operator credentials. Both are required: every service
# refuses to start with either unset, so a misconfigured deploy never takes traffic.
# Generate with: openssl rand -hex 32
SERVICE_TOKEN=
OPERATOR_TOKEN=

# Ingest refuses any path outside this directory.
CORPUS_ROOT=/corpus
```

- [ ] **Step 4: Update ci.yml**

In the per-service matrix job's `env:` block, add:

```yaml
      SERVICE_TOKEN: ci-service-token
      OPERATOR_TOKEN: ci-operator-token
```

In the `contracts` job's `env:` block, add the same two.

In the integration job, add to the `docker compose up` step's environment and to every `curl` that now needs a credential:

```yaml
      - name: Bring up the whole stack
        env:
          SERVICE_TOKEN: ci-service-token
          OPERATOR_TOKEN: ci-operator-token
        run: docker compose up -d --build
```

The ingest call becomes:

```bash
curl -fsS -X POST localhost:8001/ingest \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ci-operator-token" \
  -d "{\"root\": \"/corpus\", \"commit_sha\": \"$(git -C /tmp/fastapi-src rev-parse HEAD)\"}"
```

The golden-dataset validation step gains `SERVICE_TOKEN: ci-service-token` in its env so its `/documents` call is credentialed.

The eval smoke step's `POST /runs` gains `-H "Authorization: Bearer ci-operator-token"`.

- [ ] **Step 5: Update nightly-evals.yml**

Apply the same three changes: tokens on the compose step, an operator header on the ingest call, and an operator header on the full-dataset `POST /runs`.

- [ ] **Step 6: Update the README**

Add a `## Security` section after `## Architecture`, containing the policy table from the top of this plan and these three paragraphs:

```markdown
Two static bearer tokens carried in the environment, enforced by one dependency in
`packages/common`. An API-key table with per-caller revocation would have needed either a
shared table or one duplicated into all three databases, trading the invariant the split
exists to demonstrate for machinery a single-operator deployment will not use.

Every service refuses to start with either token unset, so a misconfigured deploy never
takes traffic. `/ingest` additionally resolves its requested root and rejects anything
outside `CORPUS_ROOT`: a leaked operator token should not become a filesystem read
primitive.

`/ask` is open, because the demo is. A per-address sliding window stops one script, and a
daily cap counted from `traces` bounds the provider bill. Only the second of those is a
spend bound -- a botnet has many real addresses -- and the two are sized together: 20 an
hour over 24 hours is 480, just under the 500 daily ceiling, so no single address can
exhaust a day's budget.
```

Update the `## Running it` ingest command to include the header:

```bash
curl -X POST localhost:8001/ingest -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${OPERATOR_TOKEN:-dev-operator-token}" \
  -d "{\"root\": \"/corpus\", \"commit_sha\": \"$(git -C /tmp/fastapi-src rev-parse HEAD)\"}"
```

In `## Deploying`, add `SERVICE_TOKEN` and `OPERATOR_TOKEN` to the Render step's list of variables to set, note that the same `SERVICE_TOKEN` must be given to all three services, and add `OPERATOR_TOKEN` plus `SERVICE_TOKEN` to the Vercel step.

In `### Tests`, correct the counts to the measured totals: **129 service tests (42 retrieval, 43 answer, 44 evals), 24 for the shared contracts, and 16 component tests.** The previous "83 service tests" figure was already stale by one before this work — the suite measured 84.

- [ ] **Step 7: Verify the whole stack from cold**

```bash
docker compose down -v
docker compose up -d --build
for s in retrieval answer evals; do docker compose exec -T $s alembic upgrade head; done

# Every service reports healthy without a credential.
for p in 8001 8002 8003; do curl -fsS localhost:$p/health; echo; done

# Protected routes refuse anonymous callers.
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8001/search   # 401
curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/traces           # 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8003/runs     # 401

# Public routes still answer.
curl -s -o /dev/null -w '%{http_code}\n' localhost:8003/eval-runs        # 200

# Ingest refuses to leave the corpus root.
curl -s -X POST localhost:8001/ingest -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer dev-operator-token' \
  -d '{"root": "/etc", "commit_sha": "x"}'                               # 400
```

Expected: three `{"status": "ok", ...}` lines, then `401 401 401`, then `200`, then a 400 whose body does not contain `/etc`.

- [ ] **Step 8: Confirm a missing token stops the service**

```bash
SERVICE_TOKEN= docker compose up retrieval 2>&1 | tail -5
```
Expected: `ValueError: the service token is empty; refusing to build an open guard`, and the container exits rather than serving.

- [ ] **Step 9: Commit**

```bash
git add docker-compose.yml render.yaml .env.example .github/workflows/ci.yml \
        .github/workflows/nightly-evals.yml README.md
git commit -m "configure the tokens and document the policy they enforce

The compose defaults are development values so the stack still starts
with no .env; Render sets real ones per service.

SERVICE_TOKEN is set by hand rather than with generateValue: both ends of
a call must present the same token, and generateValue would mint a
different one for each service.

Records the route policy table in the README, where it doubles as the
review checklist -- the tests prove each guard works, not that every
route has one.

Corrects the test counts, which were stale by one before this work."
```

---

## Self-Review

**Spec coverage.** Every section of `2026-08-05-auth-design.md` maps to a task: the policy table to Tasks 2, 4, 5; `auth.py` to Task 1; `ratelimit.py` to Tasks 6 and 7; configuration to Tasks 2–5 and 9; clients to Tasks 4 and 5; the web application to Task 8; client-IP trust to Tasks 6–8; errors to Tasks 1, 3, 7; failure modes to Tasks 6, 7; testing to every task; out-of-scope items correctly absent.

**Type consistency.** `bearer_guard` and `token_matches` keep the signatures from Task 1 wherever used. `RetrievalClient(base_url, token, timeout=30.0)` and `AnswerClient(base_url, token, timeout=120.0)` are constructed in Tasks 4 and 5 exactly as declared. `SlidingWindowLimiter.check(key, now)` is called with `time.monotonic()` in Task 7 and floats in Task 6. `client_address(request, trust_forwarded=...)` is keyword-called in both. `questions_today(session, now)` takes an aware `datetime`, matching `Trace.created_at` being `DateTime(timezone=True)` set Python-side to `datetime.now(UTC)`.

**Known gaps, recorded rather than hidden:**

- The suites prove each guard works, not that every route has one. A route added later is not covered automatically; the policy table is the mitigation, which is why Task 9 puts it in the README rather than leaving it in a spec nobody rereads.
- Per-address limiting is per-process and resets on restart. Accepted in the spec.
- Task 7's daily-cap test mutates a cached setting and clears the cache twice. If the suite is ever parallelised, that test needs isolating.
- Test counts in Task 9 assume every new test passes; if a count differs, correct the README to what the suite actually reports rather than to this number. The expected totals were derived by counting the test functions written into this plan (parametrized cases expanded), not estimated: 42 retrieval, 43 answer, 44 evals, 24 common, 16 web.
- Task 3's confinement tests build every escape path from `tmp_path`, so none of them depend on the working directory pytest happens to run in.
