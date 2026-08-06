# Event-Driven Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move ingest and eval runs off synchronous HTTP onto a Redis Streams queue with per-service workers, so a two-hour run survives a client disconnect, a container restart, and a failed item.

**Architecture:** Redis Streams carries job envelopes containing only a job id; every piece of job state lives in the owning service's own Postgres. Each service runs a worker as a second command on its existing image. An eval run fans out into one job per item and is finalised by whichever item finishes last, under a row lock.

**Tech Stack:** Python 3.12, FastAPI, `redis[asyncio]`, SQLAlchemy 2 async, Alembic, pytest with `asyncio_mode = "auto"`, ruff.

**Spec:** `docs/superpowers/specs/2026-08-06-event-driven-jobs-design.md`

## Global Constraints

- Python `>=3.12`. Ruff `line-length = 100`, rules `["E", "F", "I", "UP", "B"]`. Every task ends ruff-clean.
- **No shared tables and no cross-service joins.** `eval_item_jobs` lives only in the evals database; `ingest_jobs` only in retrieval's. There is no jobs table in `packages/common`.
- **`packages/common` receives credentials and connection strings as arguments, never from a settings singleton** — the rule `llm/base.py` states.
- **Every test runs with no Redis, no network and no API key.** The queue sits behind a Protocol with an in-memory fake, exactly as `FakeRetrieval` and `FakeAnswer` already stand in for services.
- **Every new route gets a principal**, recorded in the README's policy table. `/jobs/*` is operator; the `/eval-runs/*` additions are public.
- **Commit messages carry no attribution trailers.** Zero exist across the repository's history; this is enforced. Lowercase imperative summary, body explaining *why* and what was rejected.
- Comments explain the reasoning and the rejected alternative, not the mechanics.
- **Never run `docker compose down -v`** — it destroys the volume holding 155 ingested documents and two completed eval runs.

## File Structure

**Created**

| path | responsibility |
| --- | --- |
| `packages/common/src/deflect_common/jobs.py` | `Delivery`, `JobQueue` protocol, `RedisJobQueue`, `FakeJobQueue`, stream names. |
| `packages/common/tests/test_jobs.py` | Fake-queue semantics and the Redis client's argument shapes. |
| `services/retrieval/migrations/versions/0002_ingest_jobs.py` | `ingest_jobs`. |
| `services/retrieval/src/retrieval/worker.py` | Ingest worker entrypoint. |
| `services/evals/migrations/versions/0002_run_status_and_item_jobs.py` | Run status, `items_total`, `eval_item_jobs`, the results unique constraint. |
| `services/evals/src/evals/worker.py` | Eval item worker entrypoint. |
| `services/evals/src/evals/finalise.py` | The fan-in: row lock, completion count, aggregates. |

**Modified**

| path | change |
| --- | --- |
| `services/{retrieval,evals}/src/*/config.py` | `redis_url`. |
| `services/retrieval/src/retrieval/models.py`, `services/evals/src/evals/models.py` | Job models, run status. |
| `services/retrieval/src/retrieval/main.py` | `/ingest` returns 202; `/jobs/{id}`; `/jobs/{id}/events`. |
| `services/evals/src/evals/main.py` | `/runs` returns 202; run status and progress; `/eval-runs/{id}/events`. |
| `services/evals/src/evals/runner.py` | `score_item` extracted; `run_evals` retired. |
| `docker-compose.yml`, `render.yaml`, `.env.example`, `.github/workflows/*.yml`, `README.md` | Redis, workers, docs. |

---

## Task 1: The shared job queue

**Files:**
- Create: `packages/common/src/deflect_common/jobs.py`, `packages/common/tests/test_jobs.py`
- Modify: `packages/common/pyproject.toml` (add `redis`), then `uv lock` in `packages/common` **and all three services**

**Interfaces:**
- Consumes: nothing.
- Produces: `INGEST_STREAM`, `EVAL_ITEM_STREAM`, `CONSUMER_GROUP`; `Delivery(message_id: str, job_id: int)`; `JobQueue` protocol with `enqueue`, `claim`, `acknowledge`, `reclaim_stale`; `RedisJobQueue(url: str)`; `FakeJobQueue()`.

- [ ] **Step 1: Write the failing tests**

Create `packages/common/tests/test_jobs.py`:

```python
import pytest
from deflect_common.jobs import EVAL_ITEM_STREAM, Delivery, FakeJobQueue


async def test_a_claimed_job_carries_its_id():
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, 42)

    claimed = await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert [d.job_id for d in claimed] == [42]


async def test_an_unacknowledged_job_stays_pending():
    """Acknowledging after the work rather than on receipt is the whole reason for
    choosing streams: a worker that dies mid-job must not lose it."""
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert await queue.pending_count(EVAL_ITEM_STREAM) == 1


async def test_acknowledging_clears_the_pending_entry():
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    claimed = await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    await queue.acknowledge(EVAL_ITEM_STREAM, claimed[0].message_id)

    assert await queue.pending_count(EVAL_ITEM_STREAM) == 0


async def test_a_claimed_job_is_not_handed_to_a_second_consumer():
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert await queue.claim(EVAL_ITEM_STREAM, consumer="w2", count=10) == []


async def test_a_stale_job_can_be_reclaimed_by_another_consumer():
    """A worker that dies leaves its message pending; reclaiming is what turns a crash
    into a retry rather than a lost job."""
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    reclaimed = await queue.reclaim_stale(EVAL_ITEM_STREAM, consumer="w2", min_idle_ms=0)

    assert [d.job_id for d in reclaimed] == [42]


async def test_reclaiming_leaves_a_fresh_job_alone():
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, 42)
    await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert await queue.reclaim_stale(EVAL_ITEM_STREAM, consumer="w2", min_idle_ms=60_000) == []


async def test_claiming_an_empty_stream_returns_nothing():
    assert await FakeJobQueue().claim(EVAL_ITEM_STREAM, consumer="w1", count=10) == []


async def test_jobs_are_delivered_in_order():
    queue = FakeJobQueue()
    for job_id in (1, 2, 3):
        await queue.enqueue(EVAL_ITEM_STREAM, job_id)

    claimed = await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10)

    assert [d.job_id for d in claimed] == [1, 2, 3]


async def test_streams_are_independent():
    from deflect_common.jobs import INGEST_STREAM

    queue = FakeJobQueue()
    await queue.enqueue(INGEST_STREAM, 1)

    assert await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=10) == []


def test_a_delivery_is_hashable_and_comparable():
    """Workers deduplicate deliveries in a set when a reclaim overlaps a read."""
    assert Delivery("1-0", 7) == Delivery("1-0", 7)
    assert len({Delivery("1-0", 7), Delivery("1-0", 7)}) == 1


async def test_enqueue_rejects_a_non_integer_job_id():
    """The envelope carries only an id; anything else means payload leaked into the
    message, where it could disagree with the row it refers to."""
    with pytest.raises((TypeError, ValueError)):
        await FakeJobQueue().enqueue(EVAL_ITEM_STREAM, "not-an-id")  # type: ignore[arg-type]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/common && uv run pytest tests/test_jobs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deflect_common.jobs'`

- [ ] **Step 3: Add the dependency**

Add `"redis>=5.2"` to `dependencies` in `packages/common/pyproject.toml`, then run `uv lock` in `packages/common` **and in each of the three services**. Each service resolves `deflect-common` through its own lockfile, and CI's plain `uv sync` relocks silently rather than failing, so a stale lockfile stops describing what CI installs without anything going red.

- [ ] **Step 4: Write jobs.py**

Create `packages/common/src/deflect_common/jobs.py`:

```python
"""Job transport shared by every service that enqueues work.

Redis carries work; Postgres carries truth. The envelope holds only a job id, so a message
can never disagree with the row it refers to, and losing the Redis volume costs in-flight
delivery rather than history.

The queue sits behind a Protocol with an in-memory fake so the whole suite runs without
Redis, the same way FakeRetrieval and FakeAnswer already stand in for services.
"""

from dataclasses import dataclass
from typing import Protocol

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

INGEST_STREAM = "deflect:ingest"
EVAL_ITEM_STREAM = "deflect:eval-items"

# One group per stream. A second worker joins the same group and shares the work rather
# than receiving its own copy, which is the difference between scaling out and duplicating.
CONSUMER_GROUP = "workers"

_FIELD = "job_id"


@dataclass(frozen=True)
class Delivery:
    """A claimed message: the job id, and the handle needed to acknowledge it."""

    message_id: str
    job_id: int


class JobQueue(Protocol):
    async def ensure_group(self, stream: str) -> None: ...

    async def enqueue(self, stream: str, job_id: int) -> None: ...

    async def claim(self, stream: str, consumer: str, count: int) -> list[Delivery]: ...

    async def acknowledge(self, stream: str, message_id: str) -> None: ...

    async def reclaim_stale(
        self, stream: str, consumer: str, min_idle_ms: int
    ) -> list[Delivery]: ...


def _check_id(job_id: int) -> None:
    # bool is an int subclass and would enqueue True as job 1.
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        raise TypeError(f"job id must be an int, got {type(job_id).__name__}")


class RedisJobQueue:
    """Redis Streams behind the JobQueue protocol.

    The URL arrives as an argument rather than from settings: this package is imported by
    three services, and a library that reaches into one service's configuration cannot be
    used by the others.
    """

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("redis url is empty; refusing to build a queue that cannot connect")
        self._redis = aioredis.from_url(url, decode_responses=True)

    async def ensure_group(self, stream: str) -> None:
        """Create the consumer group, tolerating one that already exists.

        MKSTREAM matters: without it the first worker to start before any producer fails
        on a stream that does not exist yet, which is the normal order on a cold deploy.
        """
        try:
            await self._redis.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
        except ResponseError as cause:
            if "BUSYGROUP" not in str(cause):
                raise

    async def enqueue(self, stream: str, job_id: int) -> None:
        _check_id(job_id)
        await self._redis.xadd(stream, {_FIELD: job_id})

    async def claim(self, stream: str, consumer: str, count: int) -> list[Delivery]:
        response = await self._redis.xreadgroup(
            CONSUMER_GROUP, consumer, {stream: ">"}, count=count, block=2000
        )
        return [
            Delivery(message_id, int(fields[_FIELD]))
            for _, entries in response or []
            for message_id, fields in entries
        ]

    async def acknowledge(self, stream: str, message_id: str) -> None:
        await self._redis.xack(stream, CONSUMER_GROUP, message_id)

    async def reclaim_stale(
        self, stream: str, consumer: str, min_idle_ms: int
    ) -> list[Delivery]:
        _, entries, _ = await self._redis.xautoclaim(
            stream, CONSUMER_GROUP, consumer, min_idle_time=min_idle_ms, count=10
        )
        return [Delivery(message_id, int(fields[_FIELD])) for message_id, fields in entries]


class FakeJobQueue:
    """In-memory queue with the same delivery semantics, for tests.

    It models the one property that matters and is easy to get wrong: a claimed message
    stays pending until acknowledged, so a crash redelivers rather than losing the job.
    """

    def __init__(self) -> None:
        self._ready: dict[str, list[Delivery]] = {}
        self._pending: dict[str, list[tuple[Delivery, int]]] = {}
        self._groups: set[str] = set()
        self._next = 0

    async def ensure_group(self, stream: str) -> None:
        self._groups.add(stream)

    async def enqueue(self, stream: str, job_id: int) -> None:
        _check_id(job_id)
        self._next += 1
        self._ready.setdefault(stream, []).append(Delivery(f"{self._next}-0", job_id))

    async def claim(self, stream: str, consumer: str, count: int) -> list[Delivery]:
        # Real Redis answers NOGROUP if XGROUP CREATE never ran, so a worker that forgets
        # ensure_group would pass every test against a permissive fake and then crash on
        # its first message in production. The fake models the failure, not just the
        # success -- a fake that only agrees where the real thing agrees is a trap.
        if stream not in self._groups:
            raise RuntimeError(f"no consumer group on {stream}; call ensure_group first")

        ready = self._ready.get(stream, [])
        taken, self._ready[stream] = ready[:count], ready[count:]
        self._pending.setdefault(stream, []).extend((d, 0) for d in taken)
        return taken

    async def acknowledge(self, stream: str, message_id: str) -> None:
        self._pending[stream] = [
            entry for entry in self._pending.get(stream, []) if entry[0].message_id != message_id
        ]

    async def reclaim_stale(
        self, stream: str, consumer: str, min_idle_ms: int
    ) -> list[Delivery]:
        # Idle time is zero in the fake, so a positive threshold reclaims nothing. That is
        # what lets a test assert a fresh job is left alone.
        if min_idle_ms > 0:
            return []
        return [delivery for delivery, _ in self._pending.get(stream, [])]

    async def pending_count(self, stream: str) -> int:
        return len(self._pending.get(stream, []))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd packages/common && uv run pytest -q && uv run ruff check .`
Expected: PASS — 61 passed (50 existing plus 11 new). Ruff clean.

- [ ] **Step 6: Commit**

```bash
git add packages/common/src/deflect_common/jobs.py packages/common/tests/test_jobs.py \
        packages/common/pyproject.toml packages/common/uv.lock services/*/uv.lock
git commit -m "add a job queue whose envelope carries only an id

Redis carries work and Postgres carries truth, so a message can never
disagree with the row it describes and losing the Redis volume costs
in-flight delivery rather than history.

Acknowledging happens after the work, not on receipt: that is the whole
reason for streams over a list, and it is what turns a worker crash into
a retry instead of a lost job.

The queue sits behind a Protocol with an in-memory fake, so the suite
still runs with no Redis at all."
```

---

## Task 2: Ingest jobs and the async endpoint

**Files:**
- Create: `services/retrieval/migrations/versions/0002_ingest_jobs.py`
- Modify: `services/retrieval/src/retrieval/models.py`, `config.py`, `main.py`
- Test: `services/retrieval/tests/test_ingest_jobs.py` (create)

**Interfaces:**
- Consumes: `INGEST_STREAM`, `JobQueue`, `FakeJobQueue` from Task 1.
- Produces: `IngestJob` model; `build_queue()` dependency in `retrieval.main`; `POST /ingest` returning `202 {"job_id": int}`; `GET /jobs/{job_id}`; `GET /jobs/{job_id}/events`.

- [ ] **Step 1: Write the failing tests**

Create `services/retrieval/tests/test_ingest_jobs.py`:

```python
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
```

Add to `services/retrieval/tests/conftest.py`, below the existing fixtures:

```python
@pytest_asyncio.fixture
async def queue(session):
    """Binds the app to the test transaction and an in-memory queue, so no test needs
    Redis and none leaves a row behind."""
    from deflect_common.jobs import FakeJobQueue

    from retrieval.db import get_session
    from retrieval.main import app, build_queue

    fake = FakeJobQueue()
    session.commit = session.flush
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[build_queue] = lambda: fake
    yield fake
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd services/retrieval && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_retrieval_test" uv run pytest tests/test_ingest_jobs.py -q`
Expected: FAIL — `ImportError: cannot import name 'IngestJob'`

- [ ] **Step 3: Add the model and setting**

In `services/retrieval/src/retrieval/models.py`, add:

```python
class IngestJob(Base):
    """One ingest request, and everything known about how it went.

    The row is the source of truth; the Redis message carries only this id, so status
    survives a lost Redis volume and answers even while the broker is down.
    """

    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    root: Mapped[str] = mapped_column(String(1024))
    commit_sha: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    chunks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
```

Import whatever of `Integer`, `String`, `Text`, `DateTime`, `Mapped`, `mapped_column`, `datetime` the file does not already import, and add a `_now` helper matching the one in the answer service's `models.py` if absent.

In `services/retrieval/src/retrieval/config.py`, add to `Settings`:

```python
    redis_url: str = "redis://localhost:6379/0"
```

- [ ] **Step 4: Write the migration**

Create `services/retrieval/migrations/versions/0002_ingest_jobs.py`, with `down_revision` set to the existing head (check `0001_*`'s `revision` value):

```python
"""ingest jobs

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("root", sa.String(1024), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("chunks", sa.Integer),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The worker's only query: find work that is not finished.
    op.create_index("ingest_jobs_status_idx", "ingest_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ingest_jobs_status_idx", "ingest_jobs")
    op.drop_table("ingest_jobs")
```

- [ ] **Step 5: Rewrite the endpoint and add the job routes**

In `services/retrieval/src/retrieval/main.py`:

```python
def build_queue() -> JobQueue:
    return RedisJobQueue(get_settings().redis_url)


QueueDep = Annotated[JobQueue, Depends(build_queue)]
```

```python
@app.post("/ingest", status_code=202, dependencies=[Depends(require_operator)])
async def ingest(request: IngestRequest, session: SessionDep, queue: QueueDep) -> dict:
    """Accept the work and return. Embedding 2,370 chunks behind a held-open connection
    is what this endpoint used to do, and a client disconnect lost all of it.

    The path is validated before a row exists: rejecting afterwards would leave a queued
    job that can only ever fail.
    """
    root = resolve_corpus_path(request.root, get_settings().corpus_root)

    job = IngestJob(root=str(root), commit_sha=request.commit_sha)
    session.add(job)
    await session.flush()

    # Row first, then the message, both inside this transaction. A failed enqueue rolls
    # the row back, so a message never refers to a job that does not exist.
    await queue.enqueue(INGEST_STREAM, job.id)
    await session.commit()

    return {"job_id": job.id}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_operator)])
async def job_status(job_id: int, session: SessionDep) -> dict:
    job = await session.get(IngestJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return _job_payload(job)


@app.get("/jobs/{job_id}/events", dependencies=[Depends(require_operator)])
async def job_events(job_id: int, session: SessionDep) -> StreamingResponse:
    """Progress as SSE, closing once the job reaches a terminal state.

    Polled from the database rather than subscribed from Redis: the database is the
    source of truth, and a stream fed from the broker would disagree with /jobs/{id}
    the moment a message was redelivered.
    """

    async def stream() -> AsyncIterator[str]:
        while True:
            await session.refresh(job) if (job := await session.get(IngestJob, job_id)) else None
            if job is None:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                return
            yield f"data: {json.dumps(_job_payload(job))}\n\n"
            if job.status in ("done", "failed"):
                return
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")


def _job_payload(job: IngestJob) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "attempts": job.attempts,
        "chunks": job.chunks,
        "error": job.error,
    }
```

Add the imports each of these needs: `asyncio`, `json`, `AsyncIterator`, `Annotated`, `StreamingResponse`, `JobQueue`, `RedisJobQueue`, `INGEST_STREAM`, `IngestJob`.

- [ ] **Step 6: Migrate and run the suite**

```bash
cd services/retrieval
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_retrieval_test"
uv run alembic upgrade head
DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_retrieval" uv run alembic upgrade head
uv run pytest -q && uv run ruff check .
```
Expected: 56 passed (49 existing plus 7 new). Ruff clean.

- [ ] **Step 7: Commit**

```bash
git add services/retrieval/src/retrieval services/retrieval/migrations services/retrieval/tests
git commit -m "accept ingest work instead of doing it behind the request

Embedding 2,370 chunks while a connection waits meant a client
disconnect lost the whole ingest. It now returns 202 with a job id.

The row is written before the message and inside the same transaction,
so a failed enqueue rolls it back and a message never refers to a job
that does not exist. Path confinement runs before either, because
rejecting afterwards would leave a queued job that can only ever fail.

Status is polled from the database rather than subscribed from Redis:
the database is the source of truth, and a broker-fed stream would
disagree with /jobs/{id} the moment a message was redelivered."
```

---

## Task 3: The ingest worker

**Files:**
- Create: `services/retrieval/src/retrieval/worker.py`
- Test: `services/retrieval/tests/test_ingest_worker.py` (create)

**Interfaces:**
- Consumes: `IngestJob`, `INGEST_STREAM`, `FakeJobQueue`, `ingest_directory`.
- Produces: `process_one(session, queue, delivery, ingest) -> None`; `run_worker(...)` loop entrypoint.

- [ ] **Step 1: Write the failing tests**

Create `services/retrieval/tests/test_ingest_worker.py`:

```python
from pathlib import Path

from deflect_common.jobs import INGEST_STREAM, Delivery, FakeJobQueue

from retrieval.models import IngestJob
from retrieval.worker import MAX_ATTEMPTS, process_one


async def _queued(session, root: str = "/corpus") -> IngestJob:
    job = IngestJob(root=root, commit_sha="abc")
    session.add(job)
    await session.flush()
    return job


async def test_a_successful_job_records_its_chunk_count(session):
    job = await _queued(session)
    queue = FakeJobQueue()

    async def ingest(db, root: Path, sha: str) -> int:
        return 2370

    await process_one(session, queue, Delivery("1-0", job.id), ingest)

    assert job.status == "done"
    assert job.chunks == 2370


async def test_a_successful_job_is_acknowledged(session):
    job = await _queued(session)
    queue = FakeJobQueue()
    await queue.enqueue(INGEST_STREAM, job.id)
    claimed = (await queue.claim(INGEST_STREAM, "w1", 10))[0]

    async def ingest(db, root, sha) -> int:
        return 1

    await process_one(session, queue, claimed, ingest)

    assert await queue.pending_count(INGEST_STREAM) == 0


async def test_a_failing_job_is_left_pending_for_redelivery(session):
    """Not acknowledging is what makes the retry happen. Acknowledging a failure would
    silently drop the work."""
    job = await _queued(session)
    queue = FakeJobQueue()
    await queue.enqueue(INGEST_STREAM, job.id)
    claimed = (await queue.claim(INGEST_STREAM, "w1", 10))[0]

    async def ingest(db, root, sha) -> int:
        raise RuntimeError("disk fell over")

    await process_one(session, queue, claimed, ingest)

    assert job.status == "queued"
    assert job.attempts == 1
    assert await queue.pending_count(INGEST_STREAM) == 1


async def test_a_job_that_exhausts_its_attempts_fails_and_is_acknowledged(session):
    """A bounded retry is what stops a poisoned job redelivering forever."""
    job = await _queued(session)
    job.attempts = MAX_ATTEMPTS - 1
    queue = FakeJobQueue()
    await queue.enqueue(INGEST_STREAM, job.id)
    claimed = (await queue.claim(INGEST_STREAM, "w1", 10))[0]

    async def ingest(db, root, sha) -> int:
        raise RuntimeError("disk fell over")

    await process_one(session, queue, claimed, ingest)

    assert job.status == "failed"
    assert "disk fell over" in job.error
    assert await queue.pending_count(INGEST_STREAM) == 0


async def test_a_message_whose_job_row_is_missing_is_acknowledged(session):
    """Enqueue can succeed and its commit still fail. Without this, one unlucky crash
    leaves a message redelivering forever against a row that will never exist."""
    queue = FakeJobQueue()
    await queue.enqueue(INGEST_STREAM, 999999)
    claimed = (await queue.claim(INGEST_STREAM, "w1", 10))[0]

    async def ingest(db, root, sha) -> int:
        raise AssertionError("must not run")

    await process_one(session, queue, claimed, ingest)

    assert await queue.pending_count(INGEST_STREAM) == 0


async def test_an_already_finished_job_is_not_redone(session):
    """Redelivery after a successful run must not re-embed the corpus."""
    job = await _queued(session)
    job.status = "done"
    queue = FakeJobQueue()
    await queue.enqueue(INGEST_STREAM, job.id)
    claimed = (await queue.claim(INGEST_STREAM, "w1", 10))[0]

    async def ingest(db, root, sha) -> int:
        raise AssertionError("must not run")

    await process_one(session, queue, claimed, ingest)

    assert await queue.pending_count(INGEST_STREAM) == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd services/retrieval && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_retrieval_test" uv run pytest tests/test_ingest_worker.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'retrieval.worker'`

- [ ] **Step 3: Write worker.py**

Create `services/retrieval/src/retrieval/worker.py`:

```python
"""Ingest worker.

Runs from the same image as the service, as a different command. It needs this service's
database and embedder, so a shared generic worker would have to carry every service's
dependencies -- the coupling database-per-service exists to prevent.
"""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

from deflect_common.jobs import INGEST_STREAM, Delivery, JobQueue, RedisJobQueue
from sqlalchemy.ext.asyncio import AsyncSession

from retrieval.config import get_settings
from retrieval.db import SessionFactory
from retrieval.ingest.pipeline import ingest_directory
from retrieval.models import IngestJob

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# Long enough that a slow ingest is not reclaimed from a worker still doing it.
STALE_AFTER_MS = 30 * 60 * 1000

IngestFn = Callable[[AsyncSession, Path, str], Awaitable[int]]


async def process_one(
    session: AsyncSession,
    queue: JobQueue,
    delivery: Delivery,
    ingest: IngestFn = ingest_directory,
) -> None:
    """Run one job, then decide whether to acknowledge it.

    Acknowledging is the decision that matters. A success or a permanent failure is
    acknowledged; a retryable failure is deliberately not, so the visibility timeout
    redelivers it.
    """
    job = await session.get(IngestJob, delivery.job_id)

    if job is None:
        # Enqueue can succeed and its commit still fail. Without acknowledging here, one
        # unlucky crash leaves a message redelivering forever against a row that will
        # never exist.
        logger.warning("ingest job %s has no row; acknowledging", delivery.job_id)
        await queue.acknowledge(INGEST_STREAM, delivery.message_id)
        return

    if job.status in ("done", "failed"):
        # Redelivery after a finished run must not re-embed the corpus.
        await queue.acknowledge(INGEST_STREAM, delivery.message_id)
        return

    job.status = "running"
    job.attempts += 1
    await session.flush()

    try:
        job.chunks = await ingest(session, Path(job.root), job.commit_sha)
    except Exception as cause:  # noqa: BLE001 - the failure is recorded, not swallowed
        job.error = str(cause)[:1000]
        if job.attempts >= MAX_ATTEMPTS:
            job.status = "failed"
            await session.commit()
            await queue.acknowledge(INGEST_STREAM, delivery.message_id)
            logger.error("ingest job %s failed permanently: %s", job.id, cause)
            return

        # Left unacknowledged on purpose: that is what makes the retry happen.
        job.status = "queued"
        await session.commit()
        logger.warning("ingest job %s failed, attempt %s: %s", job.id, job.attempts, cause)
        return

    job.status = "done"
    job.error = None
    await session.commit()
    await queue.acknowledge(INGEST_STREAM, delivery.message_id)


async def run_worker() -> None:
    settings = get_settings()
    queue = RedisJobQueue(settings.redis_url)
    await queue.ensure_group(INGEST_STREAM)
    consumer = socket.gethostname()

    logger.info("ingest worker %s consuming %s", consumer, INGEST_STREAM)
    while True:
        deliveries = await queue.reclaim_stale(INGEST_STREAM, consumer, STALE_AFTER_MS)
        deliveries += await queue.claim(INGEST_STREAM, consumer, count=1)

        for delivery in deliveries:
            async with SessionFactory() as session:
                await process_one(session, queue, delivery)


def main() -> None:
    from deflect_common.logging import configure_logging

    configure_logging()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the suite**

```bash
cd services/retrieval
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_retrieval_test"
uv run pytest -q && uv run ruff check .
```
Expected: 62 passed (56 plus 6 new). Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add services/retrieval/src/retrieval/worker.py services/retrieval/tests/test_ingest_worker.py
git commit -m "consume ingest work in a worker on the same image

The worker needs this service's database and embedder, so it runs from
the image already built rather than a shared generic worker that would
have to carry every service's dependencies.

Acknowledging is the decision that matters. Success and permanent failure
acknowledge; a retryable failure deliberately does not, so the visibility
timeout redelivers it. A message whose row is missing acknowledges too --
enqueue can succeed and its commit still fail, and without that one
unlucky crash redelivers forever against a row that will never exist."
```

---

## Task 4: Evals schema for status, totals and item jobs

**Files:**
- Create: `services/evals/migrations/versions/0002_run_status_and_item_jobs.py`
- Modify: `services/evals/src/evals/models.py`, `services/evals/src/evals/config.py`
- Test: `services/evals/tests/test_models.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `EvalItemJob` model; `EvalRun.status`, `EvalRun.items_total`; a unique constraint on `eval_results (run_id, item_id)`; `Settings.redis_url`.

- [ ] **Step 1: Write the failing test**

Create `services/evals/tests/test_models.py`:

```python
import pytest
from sqlalchemy.exc import IntegrityError

from evals.models import EvalItemJob, EvalResult, EvalRun


def _run() -> EvalRun:
    return EvalRun(
        git_sha="abc", prompt_version="v1", judge_version="v1", model="m",
        retrieval_config={}, thresholds={}, item_count=0, metrics={},
        items_total=2, status="running",
    )


def _result(run_id: int, item_id: str) -> EvalResult:
    return EvalResult(
        run_id=run_id, item_id=item_id, question="q", answer="a", escalated=False,
        expected_escalate=False, retrieved_sources=[], hit_at_5=1.0, mrr=1.0,
    )


async def test_a_run_starts_running_with_a_target(session):
    run = _run()
    session.add(run)
    await session.flush()

    assert run.status == "running"
    assert run.items_total == 2


async def test_the_same_item_cannot_be_scored_twice_for_one_run(session):
    """At-least-once delivery means a worker that died after writing but before
    acknowledging sees the item again. Without this the score is counted twice and the
    metrics are quietly wrong."""
    run = _run()
    session.add(run)
    await session.flush()
    session.add(_result(run.id, "q1"))
    await session.flush()

    session.add(_result(run.id, "q1"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_the_same_item_may_appear_in_different_runs(session):
    first, second = _run(), _run()
    session.add_all([first, second])
    await session.flush()

    session.add_all([_result(first.id, "q1"), _result(second.id, "q1")])
    await session.flush()  # must not raise


async def test_an_item_job_tracks_its_own_attempts(session):
    run = _run()
    session.add(run)
    await session.flush()

    job = EvalItemJob(run_id=run.id, item_id="q1")
    session.add(job)
    await session.flush()

    assert job.status == "queued"
    assert job.attempts == 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/evals && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test" uv run pytest tests/test_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'EvalItemJob'`

- [ ] **Step 3: Extend the models**

In `services/evals/src/evals/models.py`, add to `EvalRun`:

```python
    # running until every item is accounted for, then complete. A run is created the
    # moment it is requested so its progress is observable from the first second.
    status: Mapped[str] = mapped_column(String(16), default="running")
    # What was asked for. item_count stays what was actually scored, so a run that lost
    # items to provider failures says so rather than claiming full coverage.
    items_total: Mapped[int] = mapped_column(Integer, default=0)
```

Add to `EvalResult`:

```python
    __table_args__ = (UniqueConstraint("run_id", "item_id", name="eval_results_run_item_key"),)
```

Add the new model:

```python
class EvalItemJob(Base):
    """One item of one run.

    The completion signal for a run, deliberately not the result row: an item that fails
    permanently never writes a result, and counting results would leave the run stalled
    at 79/80 forever, looking like work still in progress.
    """

    __tablename__ = "eval_item_jobs"
    __table_args__ = (UniqueConstraint("run_id", "item_id", name="eval_item_jobs_run_item_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"))
    item_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
```

Import `UniqueConstraint` from `sqlalchemy`.

In `services/evals/src/evals/config.py`, add `redis_url: str = "redis://localhost:6379/0"`.

- [ ] **Step 4: Write the migration**

Create `services/evals/migrations/versions/0002_run_status_and_item_jobs.py`:

```python
"""run status, item totals and per-item jobs

Revision ID: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing runs finished under the synchronous runner, so they are complete by
    # definition and their item_count is also their target.
    op.add_column(
        "eval_runs",
        sa.Column("status", sa.String(16), nullable=False, server_default="complete"),
    )
    op.add_column("eval_runs", sa.Column("items_total", sa.Integer, nullable=False, server_default="0"))
    op.execute("UPDATE eval_runs SET items_total = item_count")

    # A duplicate would make the constraint fail to build. None should exist -- the old
    # runner wrote each item once -- but a migration that dies on real data is worse than
    # one that says what it removed.
    op.execute(
        """
        DELETE FROM eval_results a
        USING eval_results b
        WHERE a.run_id = b.run_id AND a.item_id = b.item_id AND a.id > b.id
        """
    )
    op.create_unique_constraint(
        "eval_results_run_item_key", "eval_results", ["run_id", "item_id"]
    )

    op.create_table(
        "eval_item_jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "item_id", name="eval_item_jobs_run_item_key"),
    )
    # The fan-in query: how many of this run's items are finished.
    op.create_index("eval_item_jobs_run_status_idx", "eval_item_jobs", ["run_id", "status"])


def downgrade() -> None:
    op.drop_index("eval_item_jobs_run_status_idx", "eval_item_jobs")
    op.drop_table("eval_item_jobs")
    op.drop_constraint("eval_results_run_item_key", "eval_results", type_="unique")
    op.drop_column("eval_runs", "items_total")
    op.drop_column("eval_runs", "status")
```

- [ ] **Step 5: Migrate both databases and run the suite**

```bash
cd services/evals
DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test" uv run alembic upgrade head
DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals" uv run alembic upgrade head
DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test" uv run pytest -q && uv run ruff check .
```
Expected: 55 passed 1 skipped (51 plus 4 new). Ruff clean. The real database migration must also succeed — it carries two completed runs and 100 result rows, and is the only place the dedupe and backfill are exercised against real data.

- [ ] **Step 6: Commit**

```bash
git add services/evals/src/evals/models.py services/evals/src/evals/config.py \
        services/evals/migrations services/evals/tests/test_models.py
git commit -m "give a run a status, a target, and per-item jobs

A run is now created the moment it is requested, so its progress is
observable from the first second rather than only once it finishes.

items_total is what was asked for; item_count stays what was actually
scored, so a run that lost items to provider failures says so instead of
claiming full coverage.

eval_item_jobs is the completion signal, deliberately not the result row:
an item that fails permanently never writes a result, and counting
results would leave a run stalled at 79 of 80 forever, looking like work
still in progress.

The unique constraint on (run_id, item_id) makes redelivery a no-op.
Without it a worker that died after writing but before acknowledging
counts its score twice and the metrics are quietly wrong."
```

---

## Task 5: Scoring one item, and enqueueing a run

**Files:**
- Modify: `services/evals/src/evals/runner.py`, `services/evals/src/evals/main.py`
- Test: `services/evals/tests/test_runner.py` (modify), `services/evals/tests/test_run_endpoint.py` (create)

**Interfaces:**
- Consumes: `EvalItemJob`, `EvalRun.status`, `EvalRun.items_total` from Task 4; `EVAL_ITEM_STREAM`, `JobQueue` from Task 1.
- Produces: `score_item(item, answer_client, judge_client, search) -> EvalResult` (unattached, no session); `POST /runs` returning `202 {"run_id": int}`.

`run_evals` is retired in this task: its loop body becomes `score_item`, its aggregation moves to Task 6's finaliser, and its persistence moves to the worker.

- [ ] **Step 1: Write the failing tests**

Create `services/evals/tests/test_run_endpoint.py`:

```python
from deflect_common.jobs import EVAL_ITEM_STREAM
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

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

    claimed = await queue.claim(EVAL_ITEM_STREAM, consumer="w1", count=100)

    assert len(claimed) == 3


async def test_starting_a_run_still_requires_an_operator_credential(session, queue, dataset_of_three):
    assert (await post("/runs", None, {"limit": None})).status_code == 401
```

Add to `services/evals/tests/conftest.py`:

```python
@pytest_asyncio.fixture
async def queue(session):
    """Binds the app to the test transaction and an in-memory queue, so no test needs
    Redis and none leaves a row behind."""
    from deflect_common.jobs import FakeJobQueue

    from evals.db import get_session
    from evals.main import app, build_queue

    fake = FakeJobQueue()
    session.commit = session.flush
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[build_queue] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def dataset_of_three(tmp_path, monkeypatch):
    """A three-item dataset on disk, so /runs enqueues a known number of jobs."""
    path = tmp_path / "golden.yaml"
    path.write_text(
        "".join(
            f"- id: q{n}\n"
            f"  question: question {n}\n"
            f"  ideal_answer: answer {n}\n"
            f"  expected_sources: [deps.md]\n"
            f"  should_escalate: false\n"
            for n in (1, 2, 3)
        )
    )
    from evals.config import get_settings

    monkeypatch.setenv("DATASET_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd services/evals && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test" uv run pytest tests/test_run_endpoint.py -q`
Expected: FAIL — `POST /runs` returns 200 and blocks, and `build_queue` does not exist.

- [ ] **Step 3: Extract score_item**

In `services/evals/src/evals/runner.py`, replace `run_evals` with:

```python
async def score_item(
    item: GoldenItem,
    answer_client: AnswerClient,
    judge_client: LLMClient,
    search: SearchRequest | None,
) -> EvalResult:
    """Answer and score one item, returning an unattached row.

    It takes no session on purpose: the worker owns persistence, and a function that both
    scores and writes could not be retried without deciding what to do about a half-written
    result.
    """
    request = AnswerRequest(
        question=item.question,
        search=search.model_copy(update={"query": item.question}) if search else None,
    )
    outcome = await answer_client.answer(request)

    # Judging a refusal wastes tokens: there is no answer to score, and the escalation
    # metrics already capture whether refusing was correct.
    scores = (
        None
        if outcome.escalated
        else await judge_answer(judge_client, item, outcome.answer, outcome.hits)
    )

    return _result_row(item, outcome, scores)
```

Move the body that built an `EvalResult` from the old loop into a `_result_row(item, outcome, scores) -> EvalResult` helper, unchanged except that it sets no `run_id` — the worker does that. Keep `_aggregate` and `_mean` exactly as they are; Task 6 uses them.

- [ ] **Step 4: Make /runs enqueue**

In `services/evals/src/evals/main.py`:

```python
def build_queue() -> JobQueue:
    return RedisJobQueue(get_settings().redis_url)


QueueDep = Annotated[JobQueue, Depends(build_queue)]
```

```python
@router.post("/runs", status_code=202, dependencies=[Depends(require_operator)])
async def create_run(
    request: RunEvalsRequest, session: SessionDep, queue: QueueDep
) -> dict:
    """Accept the run and return its id.

    This used to execute eighty items inline, roughly two hours against a free-tier
    quota, held open by one HTTP request -- which meant a client timeout or a container
    restart destroyed the whole run.
    """
    items = _smoke_subset(load_dataset(get_settings().dataset_path), request.limit)
    if not items:
        raise HTTPException(status_code=422, detail="cannot run evals over an empty dataset")

    run = EvalRun(
        git_sha=_git_sha(),
        prompt_version="",
        judge_version=JUDGE_VERSION,
        model=get_settings().judge_model,
        retrieval_config=(request.search.model_dump() if request.search else {}),
        thresholds={},
        item_count=0,
        metrics={},
        items_total=len(items),
        status="running",
    )
    session.add(run)
    await session.flush()

    for item in items:
        session.add(EvalItemJob(run_id=run.id, item_id=item.id))
    await session.flush()

    # Rows first, then messages, all in one transaction: a failed enqueue rolls the run
    # back rather than leaving items no worker will ever be told about.
    jobs = (
        await session.execute(select(EvalItemJob).where(EvalItemJob.run_id == run.id))
    ).scalars().all()
    for job in jobs:
        await queue.enqueue(EVAL_ITEM_STREAM, job.id)

    await session.commit()
    return {"run_id": run.id}
```

`prompt_version` is filled in at finalisation from the first answer, since no answer exists yet. `fail_under` moves to the finaliser in Task 6.

- [ ] **Step 5: Update the runner tests**

`services/evals/tests/test_runner.py` tests `run_evals`, which no longer exists. Rewrite each to call `score_item` and assert on the returned row — the behaviours worth keeping are: an escalated item is not judged; a search variant is forwarded with the item's question; each item is sent as its own question. Delete the tests that only asserted persistence or aggregation; Task 6 covers those. Report the resulting count.

- [ ] **Step 6: Run the suite**

```bash
cd services/evals
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test"
uv run pytest -q && uv run ruff check .
```
Expected: the exact count depends on how many runner tests survive Step 5. Report it.

- [ ] **Step 7: Commit**

```bash
git add services/evals/src/evals services/evals/tests
git commit -m "accept an eval run as eighty jobs instead of executing it inline

POST /runs executed eighty items behind one HTTP request -- about two
hours against a free-tier quota -- so a client timeout or a container
restart destroyed the whole run. It now creates the run, one job per
item, and returns 202.

score_item takes no session on purpose: the worker owns persistence, and
a function that both scores and writes could not be retried without
deciding what to do about a half-written result.

Rows are written before messages and in one transaction, so a failed
enqueue rolls the run back rather than leaving items no worker will ever
be told about."
```

---

## Task 6: The eval worker and the fan-in

**Files:**
- Create: `services/evals/src/evals/finalise.py`, `services/evals/src/evals/worker.py`
- Test: `services/evals/tests/test_finalise.py`, `services/evals/tests/test_eval_worker.py` (create)

**Interfaces:**
- Consumes: `score_item` from Task 5; `EvalItemJob`, `EvalRun` from Task 4; `_aggregate` from `runner.py`.
- Produces: `finalise_if_complete(session, run_id) -> bool`; `process_one(session, queue, delivery, score) -> None`; `run_worker()`.

This is the task where the design's three hard problems land.

- [ ] **Step 1: Write the failing finaliser tests**

Create `services/evals/tests/test_finalise.py`:

```python
from evals.finalise import finalise_if_complete
from evals.models import EvalItemJob, EvalResult, EvalRun


async def _run(session, items_total: int = 2) -> EvalRun:
    run = EvalRun(
        git_sha="abc", prompt_version="", judge_version="v1", model="m",
        retrieval_config={}, thresholds={}, item_count=0, metrics={},
        items_total=items_total, status="running",
    )
    session.add(run)
    await session.flush()
    return run


async def _job(session, run_id: int, item_id: str, status: str) -> None:
    session.add(EvalItemJob(run_id=run_id, item_id=item_id, status=status))
    await session.flush()


async def _result(session, run_id: int, item_id: str) -> None:
    session.add(
        EvalResult(
            run_id=run_id, item_id=item_id, question="q", answer="a", escalated=False,
            expected_escalate=False, retrieved_sources=[], hit_at_5=1.0, mrr=1.0,
            faithfulness=1.0, answer_relevance=1.0, context_relevance=1.0,
        )
    )
    await session.flush()


async def test_an_incomplete_run_is_left_alone(session):
    run = await _run(session)
    await _job(session, run.id, "q1", "done")

    assert await finalise_if_complete(session, run.id) is False
    assert run.status == "running"


async def test_a_complete_run_gets_its_aggregates(session):
    run = await _run(session)
    for item in ("q1", "q2"):
        await _job(session, run.id, item, "done")
        await _result(session, run.id, item)

    assert await finalise_if_complete(session, run.id) is True
    assert run.status == "complete"
    assert run.item_count == 2
    assert run.metrics["faithfulness"] == 1.0


async def test_a_failed_item_still_completes_the_run(session):
    """Counting results instead of jobs would leave this run stalled at one of two
    forever, looking like work still in progress."""
    run = await _run(session)
    await _job(session, run.id, "q1", "done")
    await _result(session, run.id, "q1")
    await _job(session, run.id, "q2", "failed")

    assert await finalise_if_complete(session, run.id) is True
    assert run.status == "complete"
    # item_count reports what was scored, so the loss is visible.
    assert run.item_count == 1


async def test_finalising_twice_is_a_no_op(session):
    """Two workers can finish simultaneously. Exactly one may write aggregates."""
    run = await _run(session)
    for item in ("q1", "q2"):
        await _job(session, run.id, item, "done")
        await _result(session, run.id, item)

    assert await finalise_if_complete(session, run.id) is True
    assert await finalise_if_complete(session, run.id) is False


async def test_a_run_with_no_scores_completes_rather_than_hanging(session):
    run = await _run(session)
    for item in ("q1", "q2"):
        await _job(session, run.id, item, "failed")

    assert await finalise_if_complete(session, run.id) is True
    assert run.item_count == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd services/evals && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test" uv run pytest tests/test_finalise.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'evals.finalise'`

- [ ] **Step 3: Write finalise.py**

```python
"""Turning eighty finished items into one scored run."""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from evals.models import EvalItemJob, EvalResult, EvalRun
from evals.runner import _aggregate

logger = logging.getLogger(__name__)

FINISHED = ("done", "failed")


async def finalise_if_complete(session: AsyncSession, run_id: int) -> bool:
    """Score the run if every item is accounted for. True if this call finalised it.

    The row lock is what makes "last one out" safe. Two workers finishing simultaneously
    would otherwise both see a complete count and both write aggregates; here one takes
    the lock and finalises, and the other blocks, then finds the run already complete.

    Completion is counted from job rows, not result rows. An item that fails permanently
    never writes a result, so counting results would leave the run stalled forever at one
    short, looking like work still in progress.
    """
    run = (
        await session.execute(select(EvalRun).where(EvalRun.id == run_id).with_for_update())
    ).scalar_one_or_none()

    if run is None or run.status != "running":
        return False

    finished = (
        await session.execute(
            select(func.count())
            .select_from(EvalItemJob)
            .where(EvalItemJob.run_id == run_id, EvalItemJob.status.in_(FINISHED))
        )
    ).scalar_one()

    if finished < run.items_total:
        return False

    results = list(
        (
            await session.execute(select(EvalResult).where(EvalResult.run_id == run_id))
        ).scalars().all()
    )

    run.metrics = _aggregate(results)
    # What was actually scored, not what was asked for: a run that lost items to provider
    # failures must not claim to have covered the whole dataset.
    run.item_count = len(results)
    run.status = "complete"

    logger.info("run %s complete: %s of %s items scored", run_id, len(results), run.items_total)
    return True
```

- [ ] **Step 4: Write the worker tests**

Create `services/evals/tests/test_eval_worker.py`:

```python
from deflect_common.jobs import EVAL_ITEM_STREAM, Delivery, FakeJobQueue
from sqlalchemy import func, select

from evals.models import EvalItemJob, EvalResult, EvalRun
from evals.worker import MAX_ATTEMPTS, process_one


async def _run_with_one_item(session) -> tuple[EvalRun, EvalItemJob]:
    run = EvalRun(
        git_sha="abc", prompt_version="", judge_version="v1", model="m",
        retrieval_config={}, thresholds={}, item_count=0, metrics={},
        items_total=1, status="running",
    )
    session.add(run)
    await session.flush()
    job = EvalItemJob(run_id=run.id, item_id="q1")
    session.add(job)
    await session.flush()
    return run, job


def _row(item_id: str = "q1") -> EvalResult:
    return EvalResult(
        item_id=item_id, question="q", answer="a", escalated=False,
        expected_escalate=False, retrieved_sources=[], hit_at_5=1.0, mrr=1.0,
        faithfulness=1.0, answer_relevance=1.0, context_relevance=1.0,
    )


async def test_a_scored_item_is_persisted_and_acknowledged(session):
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, job.id)
    claimed = (await queue.claim(EVAL_ITEM_STREAM, "w1", 10))[0]

    async def score(item_id: str) -> EvalResult:
        return _row(item_id)

    await process_one(session, queue, claimed, score)

    assert job.status == "done"
    assert await queue.pending_count(EVAL_ITEM_STREAM) == 0


async def test_the_last_item_finalises_its_run(session):
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()

    async def score(item_id: str) -> EvalResult:
        return _row(item_id)

    await process_one(session, queue, Delivery("1-0", job.id), score)

    assert run.status == "complete"
    assert run.item_count == 1


async def test_a_redelivered_item_does_not_score_twice(session):
    """The unique constraint makes at-least-once delivery harmless. Without it the score
    is counted twice and the metrics are quietly wrong."""
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()

    async def score(item_id: str) -> EvalResult:
        return _row(item_id)

    await process_one(session, queue, Delivery("1-0", job.id), score)
    await process_one(session, queue, Delivery("1-0", job.id), score)

    count = (
        await session.execute(
            select(func.count()).select_from(EvalResult).where(EvalResult.run_id == run.id)
        )
    ).scalar_one()
    assert count == 1


async def test_a_failing_item_is_retried_before_it_is_failed(session):
    run, job = await _run_with_one_item(session)
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, job.id)
    claimed = (await queue.claim(EVAL_ITEM_STREAM, "w1", 10))[0]

    async def score(item_id: str) -> EvalResult:
        raise RuntimeError("provider timed out")

    await process_one(session, queue, claimed, score)

    assert job.status == "queued"
    assert job.attempts == 1
    assert await queue.pending_count(EVAL_ITEM_STREAM) == 1
    assert run.status == "running"


async def test_an_exhausted_item_fails_and_lets_the_run_finish(session):
    run, job = await _run_with_one_item(session)
    job.attempts = MAX_ATTEMPTS - 1
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, job.id)
    claimed = (await queue.claim(EVAL_ITEM_STREAM, "w1", 10))[0]

    async def score(item_id: str) -> EvalResult:
        raise RuntimeError("provider timed out")

    await process_one(session, queue, claimed, score)

    assert job.status == "failed"
    assert run.status == "complete"
    assert run.item_count == 0
    assert await queue.pending_count(EVAL_ITEM_STREAM) == 0


async def test_a_message_whose_job_row_is_missing_is_acknowledged(session):
    queue = FakeJobQueue()
    await queue.enqueue(EVAL_ITEM_STREAM, 999999)
    claimed = (await queue.claim(EVAL_ITEM_STREAM, "w1", 10))[0]

    async def score(item_id: str) -> EvalResult:
        raise AssertionError("must not run")

    await process_one(session, queue, claimed, score)

    assert await queue.pending_count(EVAL_ITEM_STREAM) == 0
```

- [ ] **Step 5: Write worker.py**

```python
"""Eval item worker.

Runs from the evals image as a different command: it needs this service's database and
judge client, so a shared generic worker would have to carry every service's dependencies.
"""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable

from deflect_common.jobs import EVAL_ITEM_STREAM, Delivery, JobQueue, RedisJobQueue
from deflect_common.llm.base import get_client
from sqlalchemy.ext.asyncio import AsyncSession

from evals.answer_client import AnswerClient
from evals.config import get_settings
from evals.db import SessionFactory
from evals.finalise import finalise_if_complete
from evals.models import EvalItemJob, EvalResult
from evals.runner import score_item

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
# Comfortably longer than one item, which is roughly ninety seconds against a free tier.
STALE_AFTER_MS = 15 * 60 * 1000

ScoreFn = Callable[[str], Awaitable[EvalResult]]


async def process_one(
    session: AsyncSession, queue: JobQueue, delivery: Delivery, score: ScoreFn
) -> None:
    """Score one item, persist it, and finalise the run if this was the last one."""
    job = await session.get(EvalItemJob, delivery.job_id)

    if job is None:
        logger.warning("eval item job %s has no row; acknowledging", delivery.job_id)
        await queue.acknowledge(EVAL_ITEM_STREAM, delivery.message_id)
        return

    if job.status in ("done", "failed"):
        await queue.acknowledge(EVAL_ITEM_STREAM, delivery.message_id)
        return

    job.status = "running"
    job.attempts += 1
    await session.flush()

    try:
        result = await score(job.item_id)
    except Exception as cause:  # noqa: BLE001 - the failure is recorded, not swallowed
        job.error = str(cause)[:1000]
        if job.attempts >= MAX_ATTEMPTS:
            job.status = "failed"
            # A permanently failed item must still let the run finish, or it stalls one
            # short forever looking like work in progress.
            await finalise_if_complete(session, job.run_id)
            await session.commit()
            await queue.acknowledge(EVAL_ITEM_STREAM, delivery.message_id)
            return

        job.status = "queued"
        await session.commit()
        logger.warning("eval item %s failed, attempt %s: %s", job.item_id, job.attempts, cause)
        return

    result.run_id = job.run_id
    session.add(result)
    job.status = "done"
    job.error = None

    await finalise_if_complete(session, job.run_id)
    await session.commit()
    await queue.acknowledge(EVAL_ITEM_STREAM, delivery.message_id)


async def run_worker() -> None:
    settings = get_settings()
    queue = RedisJobQueue(settings.redis_url)
    await queue.ensure_group(EVAL_ITEM_STREAM)
    consumer = socket.gethostname()

    answer_client = AnswerClient(settings.answer_url, settings.service_token)
    judge_client = get_client(
        provider=settings.llm_provider,
        model=settings.judge_model,
        api_key=settings.provider_api_key,
        base_url=settings.ollama_base_url,
    )

    logger.info("eval worker %s consuming %s", consumer, EVAL_ITEM_STREAM)
    while True:
        deliveries = await queue.reclaim_stale(EVAL_ITEM_STREAM, consumer, STALE_AFTER_MS)
        deliveries += await queue.claim(EVAL_ITEM_STREAM, consumer, count=1)

        for delivery in deliveries:
            async with SessionFactory() as session:
                await process_one(
                    session,
                    queue,
                    delivery,
                    lambda item_id: _score(session, item_id, answer_client, judge_client),
                )


async def _score(session: AsyncSession, item_id: str, answer_client, judge_client) -> EvalResult:
    """Look the item up in the dataset by id and score it.

    The job row carries only the id, not the question: the dataset on disk is the source
    of truth for what an item says, and copying it into the row would let the two drift.
    """
    from evals.dataset import load_dataset

    items = {i.id: i for i in load_dataset(get_settings().dataset_path)}
    return await score_item(items[item_id], answer_client, judge_client, None)


def main() -> None:
    from deflect_common.logging import configure_logging

    configure_logging()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run the suite**

```bash
cd services/evals
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test"
uv run pytest -q && uv run ruff check .
```
Expected: the count from Task 5 plus 11 new. Report the actual number.

- [ ] **Step 7: Commit**

```bash
git add services/evals/src/evals/finalise.py services/evals/src/evals/worker.py services/evals/tests
git commit -m "finalise a run under a row lock, counting jobs not results

Two workers finishing simultaneously would both see a complete count and
both write aggregates. The finalising transaction takes a row lock first,
so one finalises and the other finds the run already complete.

Completion counts job rows reaching done or failed, deliberately not
result rows: an item that fails permanently never writes a result, and
counting results would stall the run one short forever, looking like work
still in progress.

The worker looks an item up in the dataset by id rather than reading a
question off the job row, so the file on disk stays the single source of
truth for what an item says."
```

---

## Task 7: Run status and progress on the public routes

**Files:**
- Modify: `services/evals/src/evals/main.py`
- Test: `services/evals/tests/test_run_progress.py` (create)

**Interfaces:**
- Consumes: `EvalRun.status`, `EvalRun.items_total`, `EvalItemJob` from Task 4.
- Produces: `status` and `progress` on `GET /eval-runs` and `GET /eval-runs/{run_id}`; `GET /eval-runs/{run_id}/events`.

- [ ] **Step 1: Write the failing tests**

Create `services/evals/tests/test_run_progress.py`:

```python
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
            EvalItemJob(
                run_id=run.id, item_id=f"q{n}", status="done" if n < done else "queued"
            )
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


async def test_run_progress_stays_public(session, queue):
    """Watching an eval run is the most interesting thing this project does; putting it
    behind a credential would hide the demo."""
    run = await _running_run(session, done=1, total=2)

    assert (await get(f"/eval-runs/{run.id}")).status_code == 200
    assert (await get(f"/eval-runs/{run.id}/events")).status_code == 200
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd services/evals && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test" uv run pytest tests/test_run_progress.py -q`
Expected: FAIL — `KeyError: 'status'`

- [ ] **Step 3: Add status, progress and the event stream**

In `services/evals/src/evals/main.py`, extend `_run_summary` with `"status": run.status` and `"items_total": run.items_total`, add a progress helper, and add the SSE route:

```python
async def _progress(session: AsyncSession, run_id: int) -> dict:
    """How many of a run's items are finished.

    Failed items count as finished. Otherwise progress would stick below 100% on a run
    that is genuinely over, which reads as a stall.
    """
    finished = (
        await session.execute(
            select(func.count())
            .select_from(EvalItemJob)
            .where(EvalItemJob.run_id == run_id, EvalItemJob.status.in_(("done", "failed")))
        )
    ).scalar_one()
    total = (
        await session.execute(select(EvalRun.items_total).where(EvalRun.id == run_id))
    ).scalar_one()
    return {"finished": finished, "total": total}
```

```python
@router.get("/eval-runs/{run_id}/events")
async def run_events(run_id: int, session: SessionDep) -> StreamingResponse:
    """Progress as SSE, closing when the run finalises.

    Public, like the run itself: a run's progress is the same class of information as its
    results, which the dashboard already shows to anyone.
    """

    async def stream() -> AsyncIterator[str]:
        while True:
            run = await session.get(EvalRun, run_id)
            if run is None:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                return

            frame = {"status": run.status, "progress": await _progress(session, run_id)}
            yield f"data: {json.dumps(frame)}\n\n"
            if run.status != "running":
                return
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")
```

Add `progress` to the `get_run` payload. Declare `/eval-runs/{run_id}/events` **before** `/eval-runs/{run_id}` is not required — the literal `events` segment follows the id — but keep `/eval-runs/diff` declared before `/eval-runs/{run_id}` as it already is.

- [ ] **Step 4: Run the suite**

```bash
cd services/evals
export DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test"
uv run pytest -q && uv run ruff check .
```
Expected: previous count plus 4. Report the actual number.

- [ ] **Step 5: Commit**

```bash
git add services/evals/src/evals/main.py services/evals/tests/test_run_progress.py
git commit -m "surface run status and progress on the routes that are already public

A run's progress is the same class of information as its results, which
the dashboard already shows to anyone, so this extends public routes
rather than adding gated twins. Watching an eval run is the most
interesting thing this project does.

Failed items count as finished, or progress would stick below 100% on a
run that is genuinely over and read as a stall."
```

---

## Task 8: Redis, workers, and the paragraph that changes

**Files:**
- Modify: `docker-compose.yml`, `render.yaml`, `.env.example`, `.github/workflows/ci.yml`, `.github/workflows/nightly-evals.yml`, `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a stack where `docker compose up` runs Redis and two workers.

- [ ] **Step 1: Add Redis and the workers to compose**

```yaml
  redis:
    image: redis:7-alpine
    ports: ["${REDIS_PORT:-6379}:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 10
    volumes:
      - redisdata:/data
```

Add `REDIS_URL: redis://redis:6379/0` to the `retrieval` and `evals` environment blocks, and `redis: {condition: service_healthy}` to their `depends_on`.

Add two worker services running the images already built:

```yaml
  retrieval-worker:
    build:
      context: .
      dockerfile: services/retrieval/Dockerfile
    command: ["python", "-m", "retrieval.worker"]
    environment:
      DATABASE_URL: postgresql+asyncpg://deflect:deflect@db:5432/deflect_retrieval
      REDIS_URL: redis://redis:6379/0
      SERVICE_TOKEN: ${SERVICE_TOKEN:-dev-service-token}
      OPERATOR_TOKEN: ${OPERATOR_TOKEN:-dev-operator-token}
      CORPUS_ROOT: /corpus
    depends_on:
      db: {condition: service_healthy}
      redis: {condition: service_healthy}

  evals-worker:
    build:
      context: .
      dockerfile: services/evals/Dockerfile
    command: ["python", "-m", "evals.worker"]
    environment:
      DATABASE_URL: postgresql+asyncpg://deflect:deflect@db:5432/deflect_evals
      REDIS_URL: redis://redis:6379/0
      ANSWER_URL: http://answer:8002
      GROQ_API_KEY: ${GROQ_API_KEY:-}
      LLM_PROVIDER: ${LLM_PROVIDER:-groq}
      DATASET_PATH: /app/evals/golden.yaml
      SERVICE_TOKEN: ${SERVICE_TOKEN:-dev-service-token}
      OPERATOR_TOKEN: ${OPERATOR_TOKEN:-dev-operator-token}
    depends_on:
      db: {condition: service_healthy}
      redis: {condition: service_healthy}
```

Add `redisdata:` to the `volumes:` block.

- [ ] **Step 2: Render and .env.example**

In `render.yaml`, add a `redis` service (Render key-value), add `REDIS_URL` with `sync: false` to retrieval and evals, and add two worker services of `type: worker` using the same `dockerfilePath` with `dockerCommand` set to the module invocations above.

In `.env.example`, add:

```bash
# Job transport. Redis carries work; Postgres carries truth, so losing this costs
# in-flight delivery rather than history.
REDIS_URL=redis://localhost:6379/0
```

- [ ] **Step 3: CI**

Add a `redis:7-alpine` service to the `eval-smoke` job's `services:` block if it uses one, or rely on compose bringing it up — the job already runs `docker compose up -d --build`, so adding Redis to compose covers it. The ingest step becomes:

```bash
JOB=$(curl -fsS -X POST localhost:8001/ingest -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ci-operator-token" \
  -d "{\"root\": \"/corpus\", \"commit_sha\": \"$(git -C /tmp/fastapi-src rev-parse HEAD)\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')

for _ in $(seq 1 120); do
  STATUS=$(curl -fsS localhost:8001/jobs/$JOB -H "Authorization: Bearer ci-operator-token" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  [ "$STATUS" = "done" ] && break
  [ "$STATUS" = "failed" ] && { echo "ingest failed"; exit 1; }
  sleep 10
done
[ "$STATUS" = "done" ] || { echo "ingest did not finish"; exit 1; }
```

Apply the same shape to the eval smoke step, polling `/eval-runs/{run_id}` until `status` is `complete`, then asserting `faithfulness` against `fail_under`. `nightly-evals.yml` gets the same two changes.

- [ ] **Step 4: Rewrite the README paragraph**

Replace:

```markdown
**What it did not need.** No service mesh, no Kubernetes, no message broker, no
distributed tracing backend. Adding infrastructure the system has no use for would
obscure the parts worth understanding.
```

with:

```markdown
**What it needed, and why.** A message broker, eventually. Ingest and eval runs were
synchronous: a full eval run is about two hours against a free-tier quota, held open by
one HTTP request, and during this project's own development that run was destroyed twice
— once by a container rebuild, once by a client timeout. Redis Streams now carries both
as jobs. Redis carries work and Postgres carries truth, so job status survives a lost
broker and there is still no shared table between services.

Parallel workers do not make a run faster: the provider's rate limit is the ceiling, not
worker count. What the queue buys is retry granularity, visible progress, and survival
across a restart.

**What it still does not need.** No service mesh, no Kubernetes, no distributed tracing
backend. Adding infrastructure the system has no use for would obscure the parts worth
understanding — and the broker earned its place only after the synchronous version
failed twice in practice.
```

Add the new routes to the policy table: `POST /ingest` now `202`, `GET /jobs/{job_id}` and `GET /jobs/{job_id}/events` operator, `GET /eval-runs/{run_id}/events` public. Update "Running it" so the ingest command polls the job, and correct the test counts to what the suites report.

- [ ] **Step 5: Verify the whole stack**

```bash
cd /Users/gautam/Downloads/Projects/deflect
docker compose up -d --build
for s in retrieval answer evals; do docker compose exec -T $s alembic upgrade head; done

set -a && . ./.env && set +a
JOB=$(curl -fsS -X POST localhost:8001/ingest -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $OPERATOR_TOKEN" -d '{"root":"/corpus","commit_sha":"test"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')
echo "job $JOB accepted immediately"
curl -fsS localhost:8001/jobs/$JOB -H "Authorization: Bearer $OPERATOR_TOKEN"

RUN=$(curl -fsS -X POST localhost:8003/runs -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $OPERATOR_TOKEN" -d '{"limit": 4}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["run_id"])')
curl -fsS localhost:8003/eval-runs/$RUN
```

Expected: both calls return within a second rather than blocking; `/jobs/{id}` shows the ingest progressing; `/eval-runs/{id}` shows `status: running` with progress climbing, then `complete`. **Never run `docker compose down -v`.**

- [ ] **Step 6: Prove a restart does not lose the run**

Start a run, then restart the worker mid-flight:

```bash
RUN=$(curl -fsS -X POST localhost:8003/runs -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $OPERATOR_TOKEN" -d '{"limit": 6}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["run_id"])')
sleep 60
docker compose restart evals-worker
curl -fsS localhost:8003/eval-runs/$RUN
```

Expected: the run still reaches `complete`. This is the behaviour the whole sub-project exists for, and the only step that demonstrates it.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml render.yaml .env.example .github/workflows README.md
git commit -m "run redis and two workers, and move the broker paragraph

The README argued the system needed no message broker. That was true
until a two-hour eval run behind one HTTP request was destroyed twice
during this project's own development. The paragraph splits: the broker
moves to what it needed and why, with the measured reason, and the mesh,
Kubernetes and tracing backend stay in what it still does not need.

Workers run the images already built with a different command, because
each needs its own service's database and dependencies.

CI polls the job instead of waiting on the request, which is the point."
```

---

## Self-Review

**Spec coverage.** Redis-carries-work/Postgres-carries-truth → Tasks 1–4. Workers as a second command → Tasks 3, 6, 8. `jobs.py` with a fake → Task 1. Enqueue atomicity → Tasks 2, 5. Fan-in row lock → Task 6. Failed-item stall → Tasks 4, 6, 7. Idempotency → Tasks 4, 6. Schema → Tasks 2, 4. API surface → Tasks 2, 5, 7. Errors → Tasks 3, 6. Testing → every task. Deployment and the README paragraph → Task 8. Out-of-scope items correctly absent.

**Type consistency.** `Delivery(message_id, job_id)` is constructed identically in Tasks 1, 3 and 6. `JobQueue.claim(stream, consumer, count)` drops the `block_ms` argument the first draft of Task 1's tests used — the tests call it with three arguments and the protocol declares three. `process_one` exists in both workers with the same shape but different fourth argument (`IngestFn` vs `ScoreFn`), which is why they live in separate modules. `finalise_if_complete(session, run_id) -> bool` is called from Task 6's worker exactly as Task 6 defines it. `_aggregate` is reused from `runner.py` rather than reimplemented.

**Known gaps, recorded rather than hidden:**

- Task 5 retires `run_evals` and rewrites `test_runner.py`, so its final test count cannot be predicted here. The plan says to report the real number rather than assert one.
- Task 6's `run_worker` reloads the dataset per item. That is a file read per item against a two-hour run, so it is not worth caching, but it is a deliberate choice rather than an oversight.
- The `_score` closure in Task 6 captures `session` from the enclosing loop. An implementer refactoring that loop must keep the session per-delivery, or two items will share a transaction.
- Task 8's Render worker configuration cannot be verified locally; only compose is exercised.
- No test covers two workers finalising the same run concurrently at the database level — `test_finalising_twice_is_a_no_op` covers the logic sequentially. Proving the lock under real concurrency needs two connections and is left out deliberately.
