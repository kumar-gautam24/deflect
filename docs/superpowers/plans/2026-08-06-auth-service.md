# Admin Auth Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Deflect admin accounts with opaque sessions and two roles, so an ingest or an eval run can be attributed to a person rather than to a shared secret.

**Architecture:** A fourth service owning `admin_users` and `sessions` in its own database, mirroring live sessions into Redis. Other services read only Redis, so none depends on auth being reachable to serve its own data. One `Authorization: Bearer` header carries three credential kinds — service token, operator token, or session — and a shared resolver decides which.

**Tech Stack:** Python 3.12, FastAPI, argon2-cffi, SQLAlchemy 2 async, Alembic, Redis, pytest with `asyncio_mode = "auto"`, Next.js 16, ruff.

**Spec:** `docs/superpowers/specs/2026-08-06-auth-service-design.md`

## Global Constraints

- Python `>=3.12`. Ruff `line-length = 100`, rules `["E", "F", "I", "UP", "B"]`. Every task ends ruff-clean.
- **No shared tables and no cross-service joins.** `admin_users` and `sessions` live only in the auth database. No other service queries them — they read Redis.
- **`packages/common` receives credentials and connection strings as arguments, never from a settings singleton.**
- Something two or more services need goes in `packages/common`; something one service needs stays in that service.
- **Every test runs with no Redis, no network and no provider key.** `SessionStore` has an in-memory fake, exactly as `JobQueue` does.
- **Nothing CI does may change.** `OPERATOR_TOKEN` must still satisfy every route it satisfied before. A task that breaks the build has the wrong shape.
- **Never log a password, a session token, or a password hash.** The only form that may appear in a log is a user id.
- **Commit messages carry no attribution trailers.** Zero exist across the repository's history; this is enforced. Lowercase imperative summary, body explaining *why* and what was rejected.
- Comments explain the reasoning and the rejected alternative, not the mechanics.
- **Never run `docker compose down -v`** — the database holds an ingested corpus and completed eval runs.

## File Structure

**Created**

| path | responsibility |
| --- | --- |
| `packages/common/src/deflect_common/sessions.py` | `SessionStore` protocol, `RedisSessionStore`, `FakeSessionStore`. |
| `services/auth/` | The service: `config.py`, `db.py`, `models.py`, `policy.py`, `passwords.py`, `service.py`, `main.py`, `cli.py`, migrations, Dockerfile, pyproject. |
| `apps/web/app/login/page.tsx` | The login form. |
| `apps/web/app/api/auth/login/route.ts`, `.../logout/route.ts` | Cookie handlers. |

**Modified**

| path | change |
| --- | --- |
| `packages/common/src/deflect_common/auth.py` | `Principal`, `resolve_principal`, `principal_guard`. |
| `packages/common/src/deflect_common/ratelimit.py` | Moved from `services/answer`, so login can use it. |
| `services/answer/src/answer/ratelimit.py` | Keeps only `questions_today`; the rest is imported. |
| `services/{retrieval,answer,evals}/src/*/main.py` | Guards widened to the new principals. |
| `apps/web/proxy.ts`, `apps/web/lib/basic-auth.ts` | **Deleted.** |
| `apps/web/lib/api.ts`, `apps/web/app/traces/page.tsx` | Forward the session cookie instead of the operator token. |
| `docker-compose.yml`, `render.yaml`, `.env.example`, `.github/workflows/*`, `README.md` | Configuration and documentation. |

## The principal table this plan implements

Copied from the spec so no task has to leave this document.

| required | satisfied by |
| --- | --- |
| `service` | `SERVICE_TOKEN` only |
| `operator` | `OPERATOR_TOKEN`, or a session whose role is `admin` |
| `viewer` | `OPERATOR_TOKEN`, or any valid session |

A service token does **not** satisfy `operator`. Machine-to-machine callers and operators are different principals, and collapsing them would let any service trigger spend.

---

## Task 1: Sessions and principals in the shared package

**Files:**
- Create: `packages/common/src/deflect_common/sessions.py`, `packages/common/tests/test_sessions.py`
- Modify: `packages/common/src/deflect_common/auth.py`, `packages/common/tests/test_auth.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SessionStore` protocol with `get(token_hash) -> tuple[str, str] | None`, `put(token_hash, user_id, role, ttl_seconds)`, `delete(token_hash)`, `delete_user(user_id)`; `RedisSessionStore(url)`; `FakeSessionStore()`; `Principal(kind, role, user_id)`; `hash_token(token) -> str`; `resolve_principal(authorization, service_token, operator_token, sessions) -> Principal | None`; `satisfies(principal, required) -> bool`; `principal_guard(required, service_token, operator_token, sessions_dep)` — note the **dependency callable**, not a store.

- [ ] **Step 1: Write the failing session-store tests**

Create `packages/common/tests/test_sessions.py`:

```python
from deflect_common.sessions import FakeSessionStore


async def test_a_stored_session_resolves_to_its_user_and_role():
    store = FakeSessionStore()
    await store.put("hash-a", user_id="u1", role="admin", ttl_seconds=300)

    assert await store.get("hash-a") == ("u1", "admin")


async def test_an_unknown_hash_resolves_to_nothing():
    assert await FakeSessionStore().get("nope") is None


async def test_deleting_a_session_revokes_it():
    store = FakeSessionStore()
    await store.put("hash-a", user_id="u1", role="admin", ttl_seconds=300)

    await store.delete("hash-a")

    assert await store.get("hash-a") is None


async def test_deleting_a_user_revokes_every_session_they_hold():
    """Logging out everywhere has to reach sessions this process never issued."""
    store = FakeSessionStore()
    await store.put("hash-a", user_id="u1", role="admin", ttl_seconds=300)
    await store.put("hash-b", user_id="u1", role="admin", ttl_seconds=300)
    await store.put("hash-c", user_id="u2", role="viewer", ttl_seconds=300)

    await store.delete_user("u1")

    assert await store.get("hash-a") is None
    assert await store.get("hash-b") is None
    assert await store.get("hash-c") == ("u2", "viewer")


async def test_an_expired_session_resolves_to_nothing():
    """The TTL is the ceiling on how long a revoked session survives a missed delete, so
    expiry has to be real rather than advisory."""
    store = FakeSessionStore()
    await store.put("hash-a", user_id="u1", role="admin", ttl_seconds=0)

    assert await store.get("hash-a") is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd packages/common && uv run pytest tests/test_sessions.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deflect_common.sessions'`

- [ ] **Step 3: Write sessions.py**

```python
"""Session storage shared by every service that checks one.

Postgres is the record and this is the working copy. Services read here rather than
querying the auth database, so no service depends on auth being reachable to serve its own
data -- you simply cannot log in while it is down.

The cost is that revocation is bounded rather than instant: if a delete is missed, a
session stays usable until its entry expires. That is why the TTL is short.
"""

import json
import time
from typing import Protocol

import redis.asyncio as aioredis

_PREFIX = "session:"
_USER_PREFIX = "session-user:"


class SessionStore(Protocol):
    async def get(self, token_hash: str) -> tuple[str, str] | None: ...

    async def put(
        self, token_hash: str, user_id: str, role: str, ttl_seconds: int
    ) -> None: ...

    async def delete(self, token_hash: str) -> None: ...

    async def delete_user(self, user_id: str) -> None: ...


class RedisSessionStore:
    """Redis behind the SessionStore protocol.

    The URL arrives as an argument rather than from settings: this package is imported by
    four services, and a library that reaches into one service's configuration cannot be
    used by the others.
    """

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("redis url is empty; refusing to build a session store")
        self._redis = aioredis.from_url(url, decode_responses=True)

    async def get(self, token_hash: str) -> tuple[str, str] | None:
        raw = await self._redis.get(_PREFIX + token_hash)
        if raw is None:
            return None
        record = json.loads(raw)
        return record["user_id"], record["role"]

    async def put(self, token_hash: str, user_id: str, role: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return

        record = json.dumps({"user_id": user_id, "role": role})
        # The set of a user's live tokens is kept alongside, so logging out everywhere can
        # reach sessions issued by a different process. Without it, "log out everywhere"
        # would only reach whatever this instance happened to remember.
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(_PREFIX + token_hash, record, ex=ttl_seconds)
            pipe.sadd(_USER_PREFIX + user_id, token_hash)
            pipe.expire(_USER_PREFIX + user_id, ttl_seconds)
            await pipe.execute()

    async def delete(self, token_hash: str) -> None:
        await self._redis.delete(_PREFIX + token_hash)

    async def delete_user(self, user_id: str) -> None:
        hashes = await self._redis.smembers(_USER_PREFIX + user_id)
        if hashes:
            await self._redis.delete(*(_PREFIX + h for h in hashes))
        await self._redis.delete(_USER_PREFIX + user_id)


class FakeSessionStore:
    """In-memory store with the same semantics, for tests.

    Expiry is modelled rather than ignored: the TTL is the ceiling on how long a revoked
    session survives a missed delete, so a fake that never expired anything would hide the
    one property that bound matters for.
    """

    def __init__(self) -> None:
        self._records: dict[str, tuple[str, str, float]] = {}

    async def get(self, token_hash: str) -> tuple[str, str] | None:
        record = self._records.get(token_hash)
        if record is None:
            return None

        user_id, role, expires_at = record
        if expires_at <= time.monotonic():
            del self._records[token_hash]
            return None
        return user_id, role

    async def put(self, token_hash: str, user_id: str, role: str, ttl_seconds: int) -> None:
        self._records[token_hash] = (user_id, role, time.monotonic() + ttl_seconds)

    async def delete(self, token_hash: str) -> None:
        self._records.pop(token_hash, None)

    async def delete_user(self, user_id: str) -> None:
        for token_hash in [h for h, r in self._records.items() if r[0] == user_id]:
            del self._records[token_hash]
```

- [ ] **Step 4: Write the failing principal tests**

Append to `packages/common/tests/test_auth.py`:

```python
from deflect_common.auth import Principal, hash_token, resolve_principal
from deflect_common.sessions import FakeSessionStore


async def _store_with(token: str, user_id: str = "u1", role: str = "admin") -> FakeSessionStore:
    store = FakeSessionStore()
    await store.put(hash_token(token), user_id=user_id, role=role, ttl_seconds=300)
    return store


async def test_the_service_token_resolves_to_the_service_principal():
    found = await resolve_principal(
        "Bearer svc", service_token="svc", operator_token="op", sessions=FakeSessionStore()
    )

    assert found == Principal(kind="service")


async def test_the_operator_token_resolves_to_the_operator_principal():
    found = await resolve_principal(
        "Bearer op", service_token="svc", operator_token="op", sessions=FakeSessionStore()
    )

    assert found == Principal(kind="operator")


async def test_a_session_resolves_to_its_role_and_user():
    store = await _store_with("sess-abc", user_id="u7", role="viewer")

    found = await resolve_principal(
        "Bearer sess-abc", service_token="svc", operator_token="op", sessions=store
    )

    assert found == Principal(kind="session", role="viewer", user_id="u7")


async def test_an_unknown_token_resolves_to_nothing():
    found = await resolve_principal(
        "Bearer nope", service_token="svc", operator_token="op", sessions=FakeSessionStore()
    )

    assert found is None


async def test_a_missing_header_resolves_to_nothing():
    found = await resolve_principal(
        None, service_token="svc", operator_token="op", sessions=FakeSessionStore()
    )

    assert found is None


async def test_a_machine_token_never_costs_a_session_lookup():
    """The two comparisons short-circuit, so a service-to-service call does no I/O."""

    class ExplodingStore(FakeSessionStore):
        async def get(self, token_hash: str):
            raise AssertionError("a machine token must not reach the session store")

    assert await resolve_principal(
        "Bearer svc", service_token="svc", operator_token="op", sessions=ExplodingStore()
    ) == Principal(kind="service")


async def test_only_the_service_token_satisfies_service():
    """A logged-in human must never reach a machine-to-machine route."""
    from deflect_common.auth import satisfies

    assert satisfies(Principal(kind="service"), "service") is True
    assert satisfies(Principal(kind="operator"), "service") is False
    assert satisfies(Principal(kind="session", role="admin", user_id="u1"), "service") is False


async def test_operator_accepts_the_operator_token_or_an_admin_session():
    from deflect_common.auth import satisfies

    assert satisfies(Principal(kind="operator"), "operator") is True
    assert satisfies(Principal(kind="session", role="admin", user_id="u1"), "operator") is True
    assert satisfies(Principal(kind="session", role="viewer", user_id="u1"), "operator") is False
    # A service token is a machine, not an operator: collapsing them would let any
    # service trigger spend.
    assert satisfies(Principal(kind="service"), "operator") is False


async def test_viewer_accepts_any_valid_session_or_the_operator_token():
    from deflect_common.auth import satisfies

    assert satisfies(Principal(kind="session", role="viewer", user_id="u1"), "viewer") is True
    assert satisfies(Principal(kind="session", role="admin", user_id="u1"), "viewer") is True
    assert satisfies(Principal(kind="operator"), "viewer") is True
    assert satisfies(Principal(kind="service"), "viewer") is False


def test_hashing_a_token_is_stable_and_not_the_token():
    assert hash_token("abc") == hash_token("abc")
    assert "abc" not in hash_token("abc")
```

- [ ] **Step 5: Run them to verify they fail**

Run: `cd packages/common && uv run pytest tests/test_auth.py -q`
Expected: FAIL — `ImportError: cannot import name 'Principal'`

- [ ] **Step 6: Extend auth.py**

Add to `packages/common/src/deflect_common/auth.py`:

```python
import hashlib
from dataclasses import dataclass

from deflect_common.sessions import SessionStore

# Which credential kinds satisfy which requirement. A service token deliberately does not
# satisfy "operator": machines and operators are different principals, and collapsing them
# would let any service trigger spend.
_SATISFIES: dict[str, set[str]] = {
    "service": {"service"},
    "operator": {"operator", "session:admin"},
    "viewer": {"operator", "session:admin", "session:viewer"},
}


@dataclass(frozen=True)
class Principal:
    """Who is calling, and enough to decide what they may do.

    `role` and `user_id` are set only for a session. A token has no person behind it,
    which is the whole reason accounts exist.
    """

    kind: str
    role: str | None = None
    user_id: str | None = None


def hash_token(token: str) -> str:
    """The form a session token is stored and looked up by.

    Only this ever reaches a database or Redis, so a dump of either yields nothing that
    can be replayed.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def satisfies(principal: Principal, required: str) -> bool:
    key = f"session:{principal.role}" if principal.kind == "session" else principal.kind
    return key in _SATISFIES[required]


async def resolve_principal(
    authorization: str | None,
    service_token: str,
    operator_token: str,
    sessions: SessionStore,
) -> Principal | None:
    """Which credential kind was presented, and for a session, its role.

    The two token comparisons run first and short-circuit, so a service-to-service call
    never pays for a Redis round trip.
    """
    if not authorization:
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    if service_token and hmac.compare_digest(token, service_token):
        return Principal(kind="service")
    if operator_token and hmac.compare_digest(token, operator_token):
        return Principal(kind="operator")

    found = await sessions.get(hash_token(token))
    if found is None:
        return None

    user_id, role = found
    return Principal(kind="session", role=role, user_id=user_id)


def principal_guard(
    required: str,
    service_token: str,
    operator_token: str,
    sessions_dep: Callable[..., SessionStore],
) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency requiring at least `required`, returning who called.

    `sessions_dep` is a FastAPI dependency callable, not a store. That matters: a store
    captured in this closure could never be replaced by dependency_overrides, so every
    session test would silently run against real Redis and pass for the wrong reason.
    Taking the dependency instead keeps the store swappable the same way every other
    dependency in this codebase is.

    Returning the principal rather than a bare pass is the point: a handler can attribute
    what it does to a person, which a shared token could never support.
    """
    if required not in _SATISFIES:
        raise ValueError(f"unknown principal {required!r}")
    if not service_token or not operator_token:
        raise ValueError("service and operator tokens must both be set")

    async def guard(
        sessions: Annotated[SessionStore, Depends(sessions_dep)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        principal = await resolve_principal(
            authorization, service_token, operator_token, sessions
        )
        if principal is None or not satisfies(principal, required):
            raise HTTPException(
                status_code=401,
                detail=f"a {required} credential is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal

    return guard
```

Add `from collections.abc import Awaitable, Callable` and `Depends` from `fastapi` to the imports.

- [ ] **Step 7: Move the limiter into the shared package**

`git mv services/answer/src/answer/ratelimit.py packages/common/src/deflect_common/ratelimit.py`, then in the moved file **delete** `questions_today` and its `Trace`/SQLAlchemy imports — that function is the answer service's own, and moving it would make `packages/common` import a service's model.

Recreate `services/answer/src/answer/ratelimit.py` containing only `questions_today`, importing nothing from the moved module. Update `services/answer/src/answer/main.py` to import `SlidingWindowLimiter`, `client_address` and `seconds_until_utc_midnight` from `deflect_common.ratelimit` and `questions_today` from `answer.ratelimit`.

Move the corresponding tests: the `SlidingWindowLimiter`, `client_address` and `seconds_until_utc_midnight` cases go to `packages/common/tests/test_ratelimit.py`; the `questions_today` cases stay in `services/answer/tests/test_ratelimit.py`.

Add this comment at the top of the moved module:

```python
"""Abuse control shared by every endpoint that is reachable without a credential.

It began in the answer service when only /ask needed it. Login makes it the second caller,
and what two services need lives here -- an unauthenticated endpoint that performs an
argon2id hash is a denial-of-service target exactly as one that calls a model is.
"""
```

- [ ] **Step 8: Run everything**

```bash
cd packages/common && uv run pytest -q && uv run ruff check .
cd ../../services/answer && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_answer_test" uv run pytest -q && uv run ruff check .
```
Expected: `packages/common` gains the 5 session tests, 10 principal tests, and the 12 moved limiter tests. The answer service loses the 12 moved tests and keeps its 4 `questions_today` ones. Report both real counts.

- [ ] **Step 9: Commit**

```bash
git add packages/common services/answer
git commit -m "resolve three credential kinds behind one header

A service token, an operator token and a session all arrive as a bearer
credential, and one resolver decides which. The two token comparisons
short-circuit, so a service-to-service call never pays for a Redis read.

A service token deliberately does not satisfy operator: machines and
operators are different principals, and collapsing them would let any
service trigger spend.

The guard returns the principal rather than a bare pass, because
attributing an action to a person is the whole reason accounts exist and
a shared token could never support it.

The sliding-window limiter moves to the shared package: login makes it
the second caller, and an unauthenticated endpoint that performs an
argon2id hash is a denial-of-service target exactly as one that calls a
model is."
```

---

## Task 2: The auth service, its schema, and password hashing

**Files:**
- Create: `services/auth/pyproject.toml`, `Dockerfile`, `alembic.ini`, `migrations/`, `src/auth/{__init__,config,db,models,policy,passwords}.py`
- Test: `services/auth/tests/{conftest.py,test_passwords.py,test_models.py}`

**Interfaces:**
- Consumes: nothing from Task 1 yet.
- Produces: `AdminUser`, `Session` models; `Policy`; `hash_password(plain) -> str`, `verify_password(plain, hashed) -> bool`; `Settings` with `database_url`, `redis_url`, `service_token`, `operator_token`, `env`.

- [ ] **Step 1: Scaffold the service**

Copy `services/evals/pyproject.toml` to `services/auth/pyproject.toml` and change: `name = "deflect-auth"`, the `packages` entry to `["src/auth"]`, the `env` entry to `deflect_auth_test`, drop `pyyaml`, and add `"argon2-cffi>=23.1"`. Copy `services/evals/alembic.ini`, `migrations/env.py` and `migrations/script.py.mako`, changing every `evals` to `auth`.

Create `services/auth/src/auth/config.py` mirroring the other services:

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_auth"
    redis_url: str = "redis://localhost:6379/0"

    service_token: str = ""
    operator_token: str = ""

    # production disables the interactive API docs.
    env: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

Create `services/auth/src/auth/db.py` as an exact copy of `services/evals/src/evals/db.py` with `evals` replaced by `auth`.

Create the databases:

```bash
docker compose exec -T db psql -U deflect -d postgres -c "CREATE DATABASE deflect_auth"
docker compose exec -T db psql -U deflect -d postgres -c "CREATE DATABASE deflect_auth_test"
```

- [ ] **Step 2: Write the failing password tests**

Create `services/auth/tests/test_passwords.py`:

```python
import pytest

from auth.passwords import hash_password, needs_rehash, verify_password


def test_a_password_verifies_against_its_own_hash():
    hashed = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", hashed) is True


def test_a_wrong_password_does_not_verify():
    assert verify_password("wrong", hash_password("right")) is False


def test_the_hash_does_not_contain_the_password():
    """The obvious property, asserted because getting it wrong is catastrophic and
    silent."""
    assert "hunter2" not in hash_password("hunter2")


def test_two_hashes_of_one_password_differ():
    """Per-hash salt. Identical hashes would let anyone see which accounts share a
    password."""
    assert hash_password("same") != hash_password("same")


def test_the_hash_names_argon2id():
    assert hash_password("x").startswith("$argon2id$")


def test_verifying_against_a_malformed_hash_is_false_rather_than_an_error():
    """A corrupted row must fail the login, not crash the endpoint."""
    assert verify_password("x", "not-a-hash") is False


@pytest.mark.parametrize("empty", ["", None])
def test_verifying_without_a_stored_hash_is_false(empty):
    """An account with no password set must never authenticate."""
    assert verify_password("x", empty) is False


def test_a_fresh_hash_does_not_need_rehashing():
    assert needs_rehash(hash_password("x")) is False


def test_a_malformed_hash_is_treated_as_needing_one():
    """Login calls this after verifying. Reporting False for an unreadable hash would
    leave a broken row broken forever; True lets the next successful login replace it."""
    assert needs_rehash("not-a-hash") is True
```

Note the parametrized case collects as **two** items, so this file is **10 tests**, not the
eight functions it appears to declare.

- [ ] **Step 3: Run them to verify they fail**

Run: `cd services/auth && uv run pytest tests/test_passwords.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'auth.passwords'`

- [ ] **Step 4: Write passwords.py**

```python
"""Password hashing.

argon2id rather than bcrypt: it is memory-hard, so an attacker with GPUs gains far less
than they would against a purely CPU-bound function. The parameters are the library's
defaults, which track current guidance -- pinning our own numbers here would mean they
silently rot as hardware improves.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str | None) -> bool:
    """Whether the password matches, false for anything unusable.

    A malformed or absent hash returns False rather than raising: a corrupted row must
    fail one login, not take the endpoint down. An account with no password set can never
    authenticate, which is what makes a future SSO-only account safe to add.
    """
    if not hashed:
        return False

    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """Whether this hash was made with weaker parameters than the current defaults.

    Called after a successful login so a password migrates to stronger settings the next
    time its owner signs in, without anyone having to reset anything.
    """
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True
```

- [ ] **Step 5: Write the models and Policy**

Create `services/auth/src/auth/policy.py`:

```python
"""Auth constants in one place, each with the reason it has that value.

Constants scattered across modules are how the reasoning gets lost -- six months later
nobody remembers whether 300 was chosen or defaulted.
"""


class Policy:
    # Long enough for a working day, short enough that a forgotten session on a shared
    # machine is not a standing invitation.
    SESSION_HOURS = 12

    # The ceiling on how long a revoked session survives a missed cache delete. Not a
    # performance number: this is the security bound, and it is why it is minutes rather
    # than hours.
    CACHE_TTL_SECONDS = 300

    # Five wrong passwords is a person misremembering; more is someone guessing.
    LOCK_AFTER_FAILURES = 5
    LOCK_SECONDS = 15 * 60

    # Login performs an argon2id hash, which is deliberately expensive, so the endpoint
    # is a denial-of-service target without a limit in front of it.
    LOGIN_ATTEMPTS_PER_HOUR = 20
```

Create `services/auth/src/auth/models.py`:

```python
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(16))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    # Persisted rather than held in memory, so a restart does not clear a lockout -- the
    # same reasoning that put the ask limiter's daily cap in the database.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Session(Base):
    """A live login.

    Only the SHA-256 of the token is stored, so a dump of this table yields nothing that
    can be replayed. The role is denormalised here so a validating service needs one Redis
    read and no join; changing a user's role therefore takes effect at their next login,
    not retroactively.
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
```

- [ ] **Step 6: Write the migration**

Create `services/auth/migrations/versions/0001_admin_users_and_sessions.py`:

```python
"""admin users and sessions

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("failed_login_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Case-insensitive uniqueness: the UNIQUE above is case-sensitive, so without this
    # You@x.com and you@x.com could both exist and one of them would never be able to log
    # in reliably.
    op.create_index(
        "admin_users_email_lower_idx", "admin_users", [sa.text("lower(email)")], unique=True
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("admin_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
    )
    # "every live session for this user" -- the logout-everywhere query.
    op.create_index(
        "sessions_user_live_idx",
        "sessions",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("sessions_user_live_idx", "sessions")
    op.drop_table("sessions")
    op.drop_index("admin_users_email_lower_idx", "admin_users")
    op.drop_table("admin_users")
```

- [ ] **Step 7: Write the conftest and model test**

Create `services/auth/tests/conftest.py` mirroring the evals one:

```python
import os

os.environ["SERVICE_TOKEN"] = "test-service-token"
os.environ["OPERATOR_TOKEN"] = "test-operator-token"

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from auth.db import engine  # noqa: E402


@pytest_asyncio.fixture
async def session():
    """Each test runs in a transaction that is rolled back, so tests never share state."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:
            yield db
        await transaction.rollback()
```

Create `services/auth/tests/test_models.py`:

```python
import pytest
from sqlalchemy.exc import IntegrityError

from auth.models import AdminUser


def _user(email: str = "a@x.com", role: str = "admin") -> AdminUser:
    return AdminUser(email=email, password_hash="$argon2id$fake", role=role)


async def test_a_user_starts_unlocked_with_no_failures(session):
    user = _user()
    session.add(user)
    await session.flush()

    assert user.failed_login_count == 0
    assert user.locked_until is None


async def test_two_accounts_cannot_share_an_email(session):
    session.add(_user("a@x.com"))
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(_user("a@x.com"))


async def test_emails_differing_only_in_case_cannot_both_exist(session):
    """Otherwise one of the two can never log in reliably, and which one wins depends on
    how the lookup happens to be written."""
    session.add(_user("Gautam@x.com"))
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(_user("gautam@x.com"))
```

- [ ] **Step 8: Migrate and run**

```bash
cd services/auth
uv sync
DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_auth_test" uv run alembic upgrade head
DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_auth" uv run alembic upgrade head
uv run pytest -q && uv run ruff check .
```
Expected: 13 passed — 10 from `test_passwords.py` (8 functions, one of them parametrized into two) and 3 from `test_models.py`.

- [ ] **Step 9: Commit**

```bash
git add services/auth
git commit -m "add an auth service with argon2id passwords

argon2id rather than bcrypt: memory-hard, so an attacker with GPUs gains
far less. The library's default parameters are used rather than pinned
numbers of our own, which would silently rot as hardware improves.

verify_password returns false for a malformed or absent hash rather than
raising, so a corrupted row fails one login instead of taking the
endpoint down, and an account with no password can never authenticate.

The lockout lives in a column, not in memory, so a restart does not clear
it -- the same reasoning that put the ask limiter's daily cap in the
database. Email uniqueness is case-insensitive, because otherwise two
accounts differing only in case could both exist and one of them could
never log in reliably."
```

---

## Task 3: Login, sessions and the endpoints

**Files:**
- Create: `services/auth/src/auth/service.py`, `services/auth/src/auth/main.py`
- Test: `services/auth/tests/test_login.py`, `services/auth/tests/test_routes.py`

**Interfaces:**
- Consumes: `Policy`, `hash_password`, `verify_password`, `needs_rehash` from Task 2; `SessionStore`, `FakeSessionStore`, `hash_token`, `principal_guard` from Task 1.
- Produces: `login(session, sessions, email, password, now, ip, user_agent) -> tuple[str, Session]`; `logout(...)`, `logout_all(...)`; routes `POST /auth/login`, `POST /auth/logout`, `POST /auth/logout-all`, `GET /auth/me`, plus `/health`, `/ready`, `/metrics`.

`login` raises `LoginFailed` for bad credentials and `AccountLocked` when locked; the route maps them to `401` and `423`.

- [ ] **Step 1: Write the failing login tests**

Create `services/auth/tests/test_login.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest
from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore

from auth.models import AdminUser
from auth.passwords import hash_password
from auth.policy import Policy
from auth.service import AccountLocked, LoginFailed, login, logout, logout_all

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


async def _user(session, email="a@x.com", password="pw", role="admin") -> AdminUser:
    user = AdminUser(email=email, password_hash=hash_password(password), role=role)
    session.add(user)
    await session.flush()
    return user


async def test_a_correct_password_issues_a_session(session):
    await _user(session)
    store = FakeSessionStore()

    token, row = await login(session, store, "a@x.com", "pw", now=NOW)

    assert row.expires_at == NOW + timedelta(hours=Policy.SESSION_HOURS)
    assert await store.get(hash_token(token)) == (str(row.user_id), "admin")


async def test_only_the_hash_of_the_token_is_stored(session):
    """A dump of this table must yield nothing that can be replayed."""
    await _user(session)

    token, row = await login(session, FakeSessionStore(), "a@x.com", "pw", now=NOW)

    assert row.token_hash == hash_token(token)
    assert token not in row.token_hash


async def test_a_wrong_password_is_rejected(session):
    await _user(session)

    with pytest.raises(LoginFailed):
        await login(session, FakeSessionStore(), "a@x.com", "nope", now=NOW)


async def test_an_unknown_email_is_rejected_the_same_way(session):
    """Same exception, so the route cannot accidentally tell the two apart."""
    with pytest.raises(LoginFailed):
        await login(session, FakeSessionStore(), "nobody@x.com", "pw", now=NOW)


async def test_an_unknown_email_still_performs_a_hash(session, monkeypatch):
    """Otherwise the response time tells an attacker which addresses exist."""
    calls = {"n": 0}
    import auth.service as service_module

    real = service_module.verify_password

    def counting(plain, hashed):
        calls["n"] += 1
        return real(plain, hashed)

    monkeypatch.setattr(service_module, "verify_password", counting)

    with pytest.raises(LoginFailed):
        await login(session, FakeSessionStore(), "nobody@x.com", "pw", now=NOW)

    assert calls["n"] == 1


async def test_the_fifth_failure_locks_the_account(session):
    user = await _user(session)

    for _ in range(Policy.LOCK_AFTER_FAILURES):
        with pytest.raises(LoginFailed):
            await login(session, FakeSessionStore(), "a@x.com", "nope", now=NOW)

    assert user.locked_until == NOW + timedelta(seconds=Policy.LOCK_SECONDS)


async def test_a_locked_account_rejects_even_the_right_password(session):
    user = await _user(session)
    user.locked_until = NOW + timedelta(minutes=5)
    await session.flush()

    with pytest.raises(AccountLocked):
        await login(session, FakeSessionStore(), "a@x.com", "pw", now=NOW)


async def test_the_lock_releases_once_its_window_passes(session):
    user = await _user(session)
    user.locked_until = NOW - timedelta(seconds=1)
    await session.flush()

    token, _ = await login(session, FakeSessionStore(), "a@x.com", "pw", now=NOW)

    assert token


async def test_a_successful_login_clears_the_failure_count(session):
    user = await _user(session)
    user.failed_login_count = 3
    await session.flush()

    await login(session, FakeSessionStore(), "a@x.com", "pw", now=NOW)

    assert user.failed_login_count == 0


async def test_logging_out_revokes_this_session_only(session):
    user = await _user(session)
    store = FakeSessionStore()
    first, first_row = await login(session, store, "a@x.com", "pw", now=NOW)
    second, _ = await login(session, store, "a@x.com", "pw", now=NOW)

    await logout(session, store, hash_token(first), now=NOW)

    assert await store.get(hash_token(first)) is None
    assert await store.get(hash_token(second)) is not None
    assert first_row.revoked_at == NOW
    assert user.id


async def test_logging_out_everywhere_revokes_every_session(session):
    user = await _user(session)
    store = FakeSessionStore()
    first, _ = await login(session, store, "a@x.com", "pw", now=NOW)
    second, _ = await login(session, store, "a@x.com", "pw", now=NOW)

    await logout_all(session, store, user.id, now=NOW)

    assert await store.get(hash_token(first)) is None
    assert await store.get(hash_token(second)) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd services/auth && uv run pytest tests/test_login.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'auth.service'`

- [ ] **Step 3: Write service.py**

```python
"""Login, logout, and the decisions around them.

`now` is a parameter rather than a call to the clock, so lockout windows are tested
without sleeping.
"""

import secrets
from datetime import datetime, timedelta

from deflect_common.auth import hash_token
from deflect_common.sessions import SessionStore
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.models import AdminUser, Session
from auth.passwords import hash_password, needs_rehash, verify_password
from auth.policy import Policy

# A dummy hash to verify against when no account exists, so an unknown address costs the
# same work as a known one and cannot be distinguished by response time.
_ABSENT_USER_HASH = hash_password(secrets.token_urlsafe(16))


class LoginFailed(Exception):
    """Wrong password, or no such account. Deliberately one exception for both."""


class AccountLocked(Exception):
    def __init__(self, seconds_remaining: int) -> None:
        super().__init__(f"locked for {seconds_remaining} more seconds")
        self.seconds_remaining = seconds_remaining


async def login(
    session: AsyncSession,
    sessions: SessionStore,
    email: str,
    password: str,
    now: datetime,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, Session]:
    """Authenticate and issue a session. Returns the raw token and its row.

    The raw token is returned exactly once, here. Nothing stores it.
    """
    user = (
        await session.execute(
            select(AdminUser).where(func.lower(AdminUser.email) == email.lower())
        )
    ).scalar_one_or_none()

    if user is not None and user.locked_until and user.locked_until > now:
        raise AccountLocked(int((user.locked_until - now).total_seconds()))

    # The hash runs whether or not the account exists. Skipping it for an unknown address
    # would make the response measurably faster and turn login into an account oracle.
    stored = user.password_hash if user is not None else _ABSENT_USER_HASH
    if not verify_password(password, stored) or user is None:
        if user is not None:
            user.failed_login_count += 1
            if user.failed_login_count >= Policy.LOCK_AFTER_FAILURES:
                user.locked_until = now + timedelta(seconds=Policy.LOCK_SECONDS)
            await session.flush()
        raise LoginFailed

    if needs_rehash(user.password_hash):
        # Migrated on the way past, so a password strengthens without anyone resetting it.
        user.password_hash = hash_password(password)

    user.failed_login_count = 0
    user.locked_until = None

    token = secrets.token_urlsafe(32)
    row = Session(
        token_hash=hash_token(token),
        user_id=user.id,
        role=user.role,
        issued_at=now,
        expires_at=now + timedelta(hours=Policy.SESSION_HOURS),
        ip=ip,
        user_agent=(user_agent or "")[:512] or None,
    )
    session.add(row)
    await session.flush()

    await sessions.put(
        row.token_hash, str(user.id), user.role, ttl_seconds=Policy.CACHE_TTL_SECONDS
    )
    return token, row


async def logout(
    session: AsyncSession, sessions: SessionStore, token_hash: str, now: datetime
) -> None:
    row = (
        await session.execute(select(Session).where(Session.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = now
        await session.flush()

    # The cache delete is what makes revocation immediate. If it fails, the entry expires
    # on its own, which is why the TTL is minutes.
    await sessions.delete(token_hash)


async def logout_all(
    session: AsyncSession, sessions: SessionStore, user_id: int, now: datetime
) -> None:
    rows = (
        await session.execute(
            select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
        )
    ).scalars().all()
    for row in rows:
        row.revoked_at = now
    await session.flush()

    await sessions.delete_user(str(user_id))
```

- [ ] **Step 4: Write main.py**

Mirror the evals service's structure — module-level guards, `configure_logging()`, `RequestIdMiddleware`, the docs gating, `/health`, `/ready`, `/metrics` — and add:

```python
_sessions = RedisSessionStore(get_settings().redis_url)
require_service = bearer_guard(get_settings().service_token, "service")
_login_limiter = SlidingWindowLimiter(
    limit=Policy.LOGIN_ATTEMPTS_PER_HOUR, window_seconds=3600
)


async def current_session(
    session: SessionDep, authorization: Annotated[str | None, Header()] = None
) -> Session:
    """The Session row behind the presented token, or 401.

    Resolved from the database rather than the cache: logout has to stamp revoked_at on a
    real row, and /auth/me should report the authoritative expiry rather than a copy.
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "a session is required", headers={"WWW-Authenticate": "Bearer"})

    row = (
        await session.execute(
            select(SessionRow).where(SessionRow.token_hash == hash_token(token))
        )
    ).scalar_one_or_none()

    if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
        raise HTTPException(401, "a session is required", headers={"WWW-Authenticate": "Bearer"})
    return row


@app.post("/auth/login")
async def login_route(request: LoginRequest, http: Request, session: SessionDep) -> dict:
    address = client_address(http, trust_forwarded=False)
    if not _login_limiter.check(address, time.monotonic()):
        raise HTTPException(429, "too many login attempts", headers={"Retry-After": "3600"})

    try:
        token, row = await login(
            session,
            _sessions,
            request.email,
            request.password,
            now=datetime.now(UTC),
            ip=address,
            user_agent=http.headers.get("user-agent"),
        )
    except AccountLocked as locked:
        await session.commit()
        raise HTTPException(
            423,
            "account temporarily locked",
            headers={"Retry-After": str(locked.seconds_remaining)},
        ) from locked
    except LoginFailed as failed:
        # Committed so the failure count survives, then one message for both a wrong
        # password and an unknown address.
        await session.commit()
        raise HTTPException(401, "invalid email or password") from failed

    await session.commit()
    return {"token": token, "role": row.role, "expires_at": row.expires_at.isoformat()}
```

`/auth/logout` and `/auth/logout-all` take `current_session` and call the service functions; `/auth/me` returns the user's email, role and the session expiry. `LoginRequest` is a two-field pydantic model in `packages/common/schemas.py` alongside the others.

**`client_address` is called with `trust_forwarded=False`.** Login is reached from the web app, which does hold the service token — but trusting a forwarded address here would let anyone with that token spread login attempts across fabricated addresses and defeat the limiter.

- [ ] **Step 5: Write the route tests**

Create `services/auth/tests/test_routes.py` covering: a correct login returns 200 with a token; a wrong password returns 401 with the same body as an unknown email; a locked account returns 423 with `Retry-After`; `/auth/me` returns the role for a valid session and 401 without one; logout makes the session stop working; `/metrics` is 401 without the service token; `/health` and `/ready` are public. Use `httpx.ASGITransport` and override `build_sessions` with a `FakeSessionStore`, exactly as the other services override their queues.

- [ ] **Step 6: Run everything**

```bash
cd services/auth && uv run pytest -q && uv run ruff check .
```
Expected: 13 from Task 2, 11 login, and the route tests. Report the real count.

- [ ] **Step 7: Commit**

```bash
git add services/auth packages/common
git commit -m "issue opaque sessions and refuse to leak which emails exist

An unknown address is verified against a dummy hash so it costs the same
work as a real one. Skipping that would make the response measurably
faster for addresses that do not exist and turn login into an account
oracle.

A wrong password and an unknown address raise the same exception and
return the same body, so the route cannot accidentally tell them apart.

The raw token is returned exactly once and never stored; only its SHA-256
reaches the database and the cache. Login is rate limited on the socket
address rather than a forwarded one: the web app holds the service token,
and trusting its forwarded address would let anyone with that token
spread attempts across fabricated addresses."
```

---

## Task 4: The bootstrap CLI

**Files:**
- Create: `services/auth/src/auth/cli.py`, `services/auth/tests/test_cli.py`

**Interfaces:**
- Consumes: `AdminUser`, `hash_password` from Task 2.
- Produces: `python -m auth.cli create-admin --email … --role …`, prompting for the password.

- [ ] **Step 1: Write the failing tests**

Create `services/auth/tests/test_cli.py`:

```python
import pytest
from sqlalchemy import select

from auth.cli import create_admin
from auth.models import AdminUser
from auth.passwords import verify_password


async def test_creating_an_admin_stores_a_hashed_password(session):
    await create_admin(session, email="a@x.com", password="pw", role="admin")

    user = (await session.execute(select(AdminUser))).scalars().one()

    assert user.email == "a@x.com"
    assert user.role == "admin"
    assert verify_password("pw", user.password_hash)
    assert user.password_hash != "pw"


async def test_a_duplicate_email_is_refused_with_a_clear_message(session):
    await create_admin(session, email="a@x.com", password="pw", role="admin")

    with pytest.raises(ValueError, match="already exists"):
        await create_admin(session, email="a@x.com", password="pw2", role="viewer")


async def test_a_duplicate_differing_only_in_case_is_refused(session):
    await create_admin(session, email="Gautam@x.com", password="pw", role="admin")

    with pytest.raises(ValueError, match="already exists"):
        await create_admin(session, email="gautam@x.com", password="pw", role="admin")


async def test_an_unknown_role_is_refused(session):
    """A typo would otherwise create an account that satisfies no principal at all and
    fails only at the first request."""
    with pytest.raises(ValueError, match="role"):
        await create_admin(session, email="a@x.com", password="pw", role="superuser")


async def test_an_empty_password_is_refused(session):
    with pytest.raises(ValueError, match="password"):
        await create_admin(session, email="a@x.com", password="", role="admin")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd services/auth && uv run pytest tests/test_cli.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'auth.cli'`

- [ ] **Step 3: Write cli.py**

```python
"""Account creation.

There is no signup route, deliberately: this system has operators, not users, and an
endpoint that creates privileged accounts is a liability with no upside. Accounts are made
here, deliberately, by someone with database access.
"""

import argparse
import asyncio
import getpass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.db import SessionFactory
from auth.models import AdminUser
from auth.passwords import hash_password

ROLES = ("admin", "viewer")


async def create_admin(session: AsyncSession, email: str, password: str, role: str) -> AdminUser:
    if role not in ROLES:
        # Caught here rather than at the first request: an account with an unknown role
        # satisfies no principal and would fail in a way nothing explains.
        raise ValueError(f"role must be one of {', '.join(ROLES)}, got {role!r}")
    if not password:
        raise ValueError("password must not be empty")

    existing = (
        await session.execute(
            select(AdminUser).where(func.lower(AdminUser.email) == email.lower())
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"an account for {email} already exists")

    user = AdminUser(email=email, password_hash=hash_password(password), role=role)
    session.add(user)
    await session.flush()
    return user


async def _run(email: str, role: str) -> None:
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm: "):
        raise SystemExit("passwords did not match")

    async with SessionFactory() as session:
        user = await create_admin(session, email, password, role)
        await session.commit()
        print(f"created {user.email} as {user.role}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="auth.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="create an account")
    create.add_argument("--email", required=True)
    create.add_argument("--role", default="admin", choices=ROLES)

    args = parser.parse_args()
    # Prompted rather than taken as an argument: a password in argv is visible in the
    # process list and lands in shell history.
    asyncio.run(_run(args.email, args.role))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run and commit**

```bash
cd services/auth && uv run pytest -q && uv run ruff check .
git add services/auth
git commit -m "create accounts from a command rather than an endpoint

There is no signup route, deliberately: this system has operators, not
users, and an endpoint that creates privileged accounts is a liability
with no upside.

The password is prompted rather than taken as an argument, because a
password in argv is visible in the process list and lands in shell
history. An unknown role is refused at creation: an account with one
satisfies no principal and would fail at its first request in a way
nothing explains."
```

---

## Task 5: Widen the guards in the three existing services

**Files:**
- Modify: `services/{retrieval,answer,evals}/src/*/{config,main}.py`
- Test: one file per service, extending the existing route tests

**Interfaces:**
- Consumes: `principal_guard`, `Principal`, `RedisSessionStore`, `FakeSessionStore` from Task 1.
- Produces: `require_operator` and `require_viewer` in each service now accept sessions; `require_service` unchanged.

- [ ] **Step 1: Write the failing tests**

For each service, add a file `tests/test_principals.py` following this shape (shown for evals; adapt the routes for the others):

```python
from deflect_common.auth import hash_token
from deflect_common.sessions import FakeSessionStore
from httpx import ASGITransport, AsyncClient

from evals.main import app, build_sessions

OPERATOR = {"Authorization": "Bearer test-operator-token"}
SERVICE = {"Authorization": "Bearer test-service-token"}


async def request(method: str, path: str, headers=None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers or {}, json={})


def _with_session(role: str) -> tuple[dict, FakeSessionStore]:
    store = FakeSessionStore()
    app.dependency_overrides[build_sessions] = lambda: store
    return {"Authorization": "Bearer sess-token"}, store


async def test_the_operator_token_still_opens_every_route_it_did(session, queue):
    """CI holds this token and nothing about CI may change."""
    assert (await request("POST", "/runs", OPERATOR)).status_code != 401


async def test_an_admin_session_can_start_a_run(session, queue):
    headers, store = _with_session("admin")
    await store.put(hash_token("sess-token"), user_id="1", role="admin", ttl_seconds=300)
    try:
        assert (await request("POST", "/runs", headers)).status_code != 401
    finally:
        app.dependency_overrides.pop(build_sessions, None)


async def test_a_viewer_session_cannot_start_a_run(session, queue):
    """Reading a trace costs nothing; a run spends two hours of quota. That is the line
    the two roles exist to draw."""
    headers, store = _with_session("viewer")
    await store.put(hash_token("sess-token"), user_id="1", role="viewer", ttl_seconds=300)
    try:
        assert (await request("POST", "/runs", headers)).status_code == 401
    finally:
        app.dependency_overrides.pop(build_sessions, None)


async def test_an_anonymous_caller_still_cannot_start_a_run(session, queue):
    assert (await request("POST", "/runs")).status_code == 401
```

For the answer service add the equivalent against `GET /traces` with `viewer` — where a viewer session **must** be accepted. For retrieval, add one asserting a session is **rejected** by `POST /search`, which is `service`-only.

- [ ] **Step 2: Run them to verify they fail**

Each fails on `ImportError: cannot import name 'build_sessions'`.

- [ ] **Step 3: Wire the sessions store and swap the guards**

In each service's `config.py` add `redis_url: str = "redis://localhost:6379/0"` if absent. In each `main.py`:

```python
# Built once and cached, so a request does not open a Redis client per call, but exposed
# as a dependency so tests can replace it. Passing the store itself to principal_guard
# would close over it and make dependency_overrides silently ineffective.
@lru_cache
def build_sessions() -> SessionStore:
    return RedisSessionStore(get_settings().redis_url)


require_service = principal_guard(
    "service", _settings.service_token, _settings.operator_token, build_sessions
)
require_operator = principal_guard(
    "operator", _settings.service_token, _settings.operator_token, build_sessions
)
require_viewer = principal_guard(
    "viewer", _settings.service_token, _settings.operator_token, build_sessions
)
```

Replace `Depends(require_operator)` on `/traces` and `/traces/{trace_id}` with `Depends(require_viewer)`. Every other route keeps the guard it has; `/ingest`, `POST /runs` and `/jobs/*` are already `require_operator` and now accept an admin session for free.

Retrieval and evals do not currently import `RedisSessionStore` — add it and `SessionStore` from `deflect_common.sessions`.

- [ ] **Step 4: Run every suite**

```bash
for s in retrieval answer evals auth; do
  cd /Users/gautam/Downloads/Projects/deflect/services/$s
  DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_${s}_test" uv run pytest -q
  uv run ruff check .
done
```
Report the real counts. **Every pre-existing test must still pass** — if one that used `OPERATOR_TOKEN` now fails, that is a regression in the widening, not a test to update.

- [ ] **Step 5: Commit**

```bash
git add services
git commit -m "let a session satisfy the routes a person should reach

The operator token still opens everything it opened before, so nothing
CI does changes. What is new is that an admin session opens the same
routes, and a viewer session opens the traces surface but cannot start an
eval run.

That line is the reason there are two roles: reading a trace costs
nothing, and starting a run spends two hours of provider quota.

A session is still refused by the service-only routes. Machines and
people are different principals, and collapsing them would let a
logged-in human reach service-to-service endpoints."
```

---

## Task 6: The login page and the cookie

**Files:**
- Create: `apps/web/app/login/page.tsx`, `apps/web/app/api/auth/login/route.ts`, `apps/web/app/api/auth/logout/route.ts`, `apps/web/lib/session.ts`, `apps/web/lib/session.test.ts`
- Delete: `apps/web/proxy.ts`, `apps/web/lib/basic-auth.ts`, `apps/web/lib/basic-auth.test.ts`
- Modify: `apps/web/lib/api.ts`, `apps/web/app/traces/page.tsx`, `apps/web/components/nav.tsx`

**Interfaces:**
- Consumes: the auth service's `POST /auth/login` and `POST /auth/logout`.
- Produces: `COOKIE_NAME`, `sessionToken()` in `apps/web/lib/session.ts`; `getFromAnswer` forwards the session.

- [ ] **Step 1: Write the failing test**

Create `apps/web/lib/session.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { cookieOptions } from "./session";

describe("cookieOptions", () => {
  it("keeps the cookie away from scripts", () => {
    // The session token is a bearer credential. Readable by script, one XSS is a
    // full account takeover rather than a defaced page.
    expect(cookieOptions(3600).httpOnly).toBe(true);
  });

  it("does not send the cookie on cross-site requests", () => {
    expect(cookieOptions(3600).sameSite).toBe("lax");
  });

  it("expires with the session rather than outliving it", () => {
    expect(cookieOptions(3600).maxAge).toBe(3600);
  });

  it("is secure outside development", () => {
    expect(cookieOptions(3600, "production").secure).toBe(true);
  });

  it("is not secure in development, so http://localhost still works", () => {
    expect(cookieOptions(3600, "development").secure).toBe(false);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/web && npx vitest run lib/session.test.ts`
Expected: FAIL — cannot resolve `./session`

- [ ] **Step 3: Write session.ts**

```ts
export const COOKIE_NAME = "deflect_session";

// The session token is a bearer credential: anything holding it is the user. httpOnly
// keeps it out of reach of scripts, so one XSS is a defaced page rather than a full
// account takeover. Lax rather than Strict because the login redirect is a top-level
// navigation, which Strict would block.
export function cookieOptions(maxAgeSeconds: number, env = process.env.NODE_ENV) {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    secure: env === "production",
    path: "/",
    maxAge: maxAgeSeconds,
  };
}
```

- [ ] **Step 4: Write the route handlers and the page**

`app/api/auth/login/route.ts` posts `{email, password}` to `${AUTH_URL}/auth/login`, and on success sets the cookie from the returned token with `maxAge` derived from `expires_at`. It returns the upstream status on failure so the form can show 401 and 423 differently — the second is "locked, try again in N minutes", which a user needs to be told rather than left guessing.

`app/api/auth/logout/route.ts` forwards the cookie to `${AUTH_URL}/auth/logout` and clears it locally regardless of the upstream result: a user who clicks log out must end up logged out of this browser even if the service is unreachable.

`app/login/page.tsx` is a client component with an email field, a password field and a submit button, posting to `/api/auth/login` and redirecting to `/traces` on success.

Add a log-out control to `components/nav.tsx` that posts to `/api/auth/logout` and redirects to `/login`.

- [ ] **Step 5: Forward the session instead of the operator token**

In `apps/web/lib/api.ts`, `getFromAnswer` takes the session token from the cookie rather than reading `OPERATOR_TOKEN`:

```ts
import { cookies } from "next/headers";

// The traces surface shows data because the person asking is allowed to see it, not
// because the server holds a master key. Forwarding the caller's own credential is what
// makes the audit trail meaningful.
export async function getFromAnswer<T>(path: string): Promise<T> {
  const token = (await cookies()).get(COOKIE_NAME)?.value;
  return getJSONFrom<T>(ANSWER_URL, path, token);
}
```

In `app/traces/page.tsx`, catch a 401 from that call and redirect to `/login` — with `proxy.ts` gone, this is what stops an anonymous visitor seeing a stack trace instead of a login form.

Delete `proxy.ts`, `lib/basic-auth.ts` and `lib/basic-auth.test.ts`.

- [ ] **Step 6: Run the web checks**

```bash
cd apps/web && npm test && npm run lint && npm run build
```
Expected: the 8 basic-auth tests are gone and 5 session tests replace them. Report the real count. The build must show **no** Proxy entry, since `proxy.ts` is deleted.

- [ ] **Step 7: Commit**

```bash
git add apps/web
git commit -m "replace the browser credential prompt with a login page

Basic auth was the right call when the alternative was building a session
store for one operator. Now that sessions exist, keeping it would mean
two authentication systems where one will do.

The traces page forwards the caller's own session rather than the
server's operator token, so it shows data because that person is allowed
to see it, not because the server holds a master key.

The cookie is httpOnly: the session token is a bearer credential, and one
XSS with it readable is a full account takeover rather than a defaced
page. Logging out clears it locally even if the auth service is
unreachable, because a user who clicks log out must end up logged out."
```

---

## Task 7: Configuration, CI and documentation

**Files:**
- Modify: `docker-compose.yml`, `render.yaml`, `.env.example`, `.github/workflows/ci.yml`, `README.md`

- [ ] **Step 1: Compose**

Add an `auth` service mirroring `evals` — same build context, `DATABASE_URL` pointing at `deflect_auth`, `REDIS_URL`, both tokens, `ENV`, port `8004` — and add `deflect_auth` to `scripts/create-databases.sql`. Add `AUTH_URL: http://auth:8004` to the web app's environment if one is defined there.

- [ ] **Step 2: Render**

Add a `deflect-auth` web service with `healthCheckPath: /ready` and `sync: false` entries for `DATABASE_URL`, `REDIS_URL`, `SERVICE_TOKEN`, `OPERATOR_TOKEN` and `ENV`. Add `AUTH_URL` to the Vercel notes in the README's deploying section.

- [ ] **Step 3: CI**

Add `auth` to the per-service matrix in `.github/workflows/ci.yml` — it needs `deflect_auth` and `deflect_auth_test` created and migrated exactly as the others are. **No other CI change is needed**, and that is the point: `OPERATOR_TOKEN` still satisfies every route the workflows touch.

- [ ] **Step 4: README**

Add the auth rows to the policy table:

```markdown
| auth | `POST /auth/login` | public, rate limited |
| auth | `POST /auth/logout`, `/auth/logout-all`, `GET /auth/me` | valid session |
```

Change `/traces` from `operator` to `viewer`, and add a short section after the Security one:

```markdown
### Who did this

Two shared tokens answer whether a caller is allowed. They cannot answer who, which is the
question that matters as soon as more than one person can trigger an ingest or an eval run.

An `auth` service issues opaque sessions — 32 random bytes, stored only as a SHA-256, so a
dump of its database yields nothing replayable. Services read those sessions from Redis
rather than calling auth, so none of them depends on auth being reachable to serve its own
data; the cost is that revocation is bounded by the cache TTL rather than instant, which is
why that TTL is five minutes.

Two roles draw the line that exists in this system: a **viewer** can read traces and eval
runs, an **admin** can also spend two hours of provider quota by starting a run.

Accounts are created with `python -m auth.cli create-admin --email you@example.com`. There
is no signup route: this system has operators, not users.
```

Correct the test counts to what the suites report.

- [ ] **Step 5: Verify the stack end to end**

```bash
cd /Users/gautam/Downloads/Projects/deflect
docker compose up -d --build
docker compose exec -T auth alembic upgrade head
docker compose exec -T auth python -m auth.cli create-admin --email demo@example.com --role admin

set -a && . ./.env && set +a
TOKEN=$(curl -fsS -X POST localhost:8004/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com","password":"<the password you set>"}' | jq -r .token)

# The session opens the traces surface.
curl -s -o /dev/null -w 'traces with session: %{http_code}\n' \
  localhost:8002/traces -H "Authorization: Bearer $TOKEN"

# And is refused by a service-only route.
curl -s -o /dev/null -w 'search with session: %{http_code}\n' \
  -X POST localhost:8001/search -H "Authorization: Bearer $TOKEN"

# The operator token still works, which is what keeps CI green.
curl -s -o /dev/null -w 'traces with operator token: %{http_code}\n' \
  localhost:8002/traces -H "Authorization: Bearer $OPERATOR_TOKEN"
```

Expected: `200`, `401`, `200`. **Never run `docker compose down -v`.**

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml render.yaml .env.example .github/workflows/ci.yml README.md scripts/
git commit -m "run the auth service and document who it lets in

CI needs one change and only one: the new service joins the per-service
matrix. Nothing else moves, because the operator token still satisfies
every route the workflows touch -- an auth change that broke the build
would have been the wrong shape.

The README gains the question the tokens could never answer. Two shared
secrets say whether a caller is allowed; they cannot say who, and that is
what the accounts buy."
```

---

## Self-Review

**Spec coverage.** Principals and the three credential kinds → Task 1. `SessionStore` with a fake → Task 1. Limiter move → Task 1. Service scaffold, schema, argon2id, `Policy` → Task 2. Sessions, lockout, enumeration resistance, login rate limit → Task 3. CLI bootstrap → Task 4. Widened guards across three services → Task 5. Login page, cookie, deleting basic auth, forwarding the session → Task 6. Compose, Render, CI, README → Task 7. Out-of-scope items correctly absent.

**Type consistency.** `SessionStore.get` returns `tuple[str, str] | None` and is consumed that way in `resolve_principal`. `user_id` is a **string** everywhere in the store and in `Principal`, and an **int** in the database — the conversion happens once, in `login` and `logout_all`, which is why both call `str(user.id)`. `hash_token` is defined in Task 1 and used in Tasks 3, 4 and 5. `principal_guard(required, service_token, operator_token, sessions)` is constructed identically in all three services in Task 5. `Policy.CACHE_TTL_SECONDS` is the TTL passed to `sessions.put` in Task 3.

**Known gaps, recorded rather than hidden:**

- Task 3's `current_session` reads the database rather than the cache, so the auth service alone does not benefit from the cache. That is deliberate — logout must stamp a real row — but it means auth is the one service that cannot serve while its database is down.
- The `_ABSENT_USER_HASH` is computed once at import. Its verification cost is representative rather than identical to a real user's, so timing is *comparable*, not constant. Making it truly constant-time would need a fixed-cost comparison the library does not expose.
- Task 6's redirect-on-401 is untested; `next/headers` and server-component redirects need a Next test harness this project does not have. Called out rather than faked.
- Session rows are never deleted. A sweeper for expired rows is out of scope; the table grows by one row per login, which for this system is negligible for years.
