# Event-driven ingest and eval runs

Sub-project B of Deflect's next phase. Sub-project A (Groq plus production hardening) is
merged. C (auth service) and D (API gateway) follow; E (Kubernetes) is optional.

## Why this exists, concretely

`POST /runs` executes 80 items synchronously. Measured on 2026-08-06 against Groq's free
tier: **roughly two hours**, held open by a single HTTP request. During this project's own
session that run was destroyed twice — once by rebuilding the containers mid-flight, once
by a client-side timeout — losing 45 minutes each time.

`POST /ingest` has the same shape: it embeds 2,370 chunks while a connection waits.

Neither is a hypothetical. A two-hour operation behind a synchronous request is a design
that does not work, and that is the whole justification for a broker here. Nothing about
this is architecture for its own sake.

## Decisions

| decision | choice | why not the alternative |
| --- | --- | --- |
| transport | Redis Streams | Postgres `SKIP LOCKED` needs no new container and is the YAGNI answer, but gives no consumer groups or redelivery. RabbitMQ's routing model has nothing to route: two job types, one consumer each. |
| job grain | one job per eval item | One job per run puts checkpointing back inside the worker, which is re-implementing the queue there. |
| status transport | poll plus SSE | Webhooks would mean services making outbound calls to arbitrary URLs, a new security surface on a public deployment. |
| compatibility | none | One operator, no external consumers. A `?wait=true` shim would preserve the exact failure mode this removes. |

**Parallel workers will not make an eval run faster.** Groq's free tier allows 8,000
tokens a minute and that ceiling is unchanged by concurrency. What per-item jobs buy is
retry granularity, visible progress, and survival across a restart. Stating this here so
nobody later concludes the queue underdelivered on a promise it never made.

## Architecture

**Redis carries work. Postgres carries truth.**

Redis Streams delivers job envelopes and nothing else. Every piece of job *state* lives in
the owning service's own database. This keeps database-per-service intact — no shared job
table anywhere — means losing the Redis volume costs in-flight delivery rather than
history, and means `GET /jobs/{id}` still answers when the broker is down.

**Workers are a second command on the same image.** `services/retrieval` gains a worker
consuming `deflect:ingest`; `services/evals` gains one consuming `deflect:eval-items`. The
ingest worker needs retrieval's database and embedder; the eval worker needs evals'
database and judge client. One generic worker would need both, which is the coupling the
split exists to prevent.

```
POST /runs ──> evals ──(row, then XADD × 80)──> deflect:eval-items
                                                        │
                                          evals-worker (consumer group)
                                                        │
                                    /answer ──> judge ──> EvalResult per item
                                                        │
                                          last item finalises the run
```

### `packages/common/src/deflect_common/jobs.py`

The job envelope schema, the stream names, and a thin client over `XADD`, `XREADGROUP`,
`XACK` and `XAUTOCLAIM`. Both services need identical semantics, which is what this package
is for. The Redis URL arrives as an argument, never from a settings singleton — the rule
`llm/base.py` states for provider keys.

The envelope carries **only the job id**. Payload lives in the owning database, so a
message can never disagree with the row it refers to.

The client sits behind a small interface with an in-memory fake, the same way
`FakeRetrieval` and `FakeAnswer` already stand in for services, so the whole suite runs
with no Redis.

### Lifecycle

1. Enqueue is one transaction: insert the job row (`status=queued`), `XADD`, commit. A
   failed `XADD` rolls the row back, so the caller gets a 503 with nothing created.
2. The worker claims the message, sets `status=running`, does the work, writes results,
   sets `status=done` or `failed`, and **then** acknowledges.
3. A worker that dies leaves the message pending. `XAUTOCLAIM` reclaims it after the
   visibility timeout, so a crash costs a retry rather than a lost job.

Acknowledging after the work rather than on receipt is the entire reason for choosing
Streams over a list.

## The three problems per-item jobs create

### Finalising the run is a race

The last item to finish computes aggregates. Two workers completing simultaneously could
both see "all done" and both write them. The finalising transaction therefore takes a row
lock — `SELECT ... FROM eval_runs WHERE id = :id FOR UPDATE` — *before* counting. One
transaction counts and finalises; the other blocks, then finds the run already complete and
does nothing.

### A permanently failed item must not stall the run

This is the trap. If completion is counted from `eval_results`, a job that exhausts its
retries never writes one and the run hangs at 79/80 **forever**, looking like work still in
progress.

So the completion signal is not the result row. Each item has an `eval_item_jobs` row whose
status reaches `done` **or** `failed`, and the run finalises when
`count(status in ('done','failed')) == items_total`. A failed item ends the run honestly
with fewer scores rather than blocking it.

That is also the dead-letter behaviour, expressed as a queryable row carrying its error
rather than a message in a queue nobody monitors.

### At-least-once delivery double-counts scores

A worker that finished the work but died before `XACK` sees the message again. Unprotected,
that writes a second `EvalResult` and quietly corrupts the metrics. A unique constraint on
`(run_id, item_id)` plus an upsert makes redelivery a no-op.

## Schema

Evals database only:

- `eval_runs` gains `status` (`running` | `complete` | `failed`) and `items_total`.
- `eval_results` gains a unique constraint on `(run_id, item_id)`.
- New `eval_item_jobs`: `id`, `run_id`, `item_id`, `status`, `attempts`, `error`,
  `created_at`, `updated_at`.

Retrieval database only:

- New `ingest_jobs`: `id`, `root`, `commit_sha`, `status`, `attempts`, `error`, `chunks`,
  `created_at`, `updated_at`.

`item_count` keeps its current meaning — how many items were actually scored — and is
written at finalisation, so a run that lost items still says so. Progress is
`(done + failed) / items_total`.

Ingest needs none of the fan-in machinery: one job, one row, done or failed.

## API

Ingest and eval runs get **different** status surfaces, because they are genuinely
different shapes: an ingest is one job, a run is an aggregate of eighty. Inventing a parent
job id for a run so both could share one route would be a fiction maintained forever.

| route | principal | behaviour |
| --- | --- | --- |
| `POST /ingest` | operator | `202` with `{"job_id": ...}` |
| `GET /jobs/{job_id}` | operator | status, attempts, error |
| `GET /jobs/{job_id}/events` | operator | SSE, closes on a terminal status |
| `POST /runs` | operator | `202` with `{"run_id": ...}` |
| `GET /eval-runs/{run_id}` | public | now also carries `status` and progress |
| `GET /eval-runs/{run_id}/events` | public | SSE progress, closes when the run finalises |

`/jobs/*` lives in the retrieval service and describes ingest work; it is operator-only,
because triggering and inspecting work is an operational act.

The run surfaces stay **public**, extending routes that are already public rather than
adding gated twins. A run's progress is the same class of information as its results, which
the dashboard already shows to anyone. Watching an eval run is the most interesting thing
this project does; putting it behind a credential would hide the demo.

The SSE frames reuse the shape `/ask` already emits, so the web app's existing parser
applies.

## Errors

| condition | behaviour |
| --- | --- |
| Redis unreachable at enqueue | `503`, transaction rolled back, nothing created |
| `XADD` succeeded but commit failed | worker finds no row: warns and acknowledges |
| work raises, attempts remain | not acknowledged; redelivered after the visibility timeout |
| work raises, attempts exhausted | `status=failed` with the error, then acknowledged |
| job id unknown to `GET /jobs/{job_id}` | `404` |
| run id unknown to `GET /eval-runs/{run_id}` | `404`, as today |

The commit-failed window is narrow but real. Without the warn-and-acknowledge rule, one
unlucky crash leaves a message redelivering forever against a row that will never exist.

## Testing

Everything runs with no Redis and no provider key, so a stranger who clones the repository
runs the whole suite.

- **Fan-in race:** two finalising transactions concurrently produce exactly one set of
  aggregates.
- **Idempotency:** the same item delivered twice produces one `eval_results` row.
- **Stall:** a permanently failed item still lets the run finalise, at
  `items_total - 1` scored.
- **Missing row:** a message whose job row does not exist is acknowledged, not looped.
- **Retry:** work that raises is redelivered, and marked `failed` once attempts are spent.
- **Enqueue atomicity:** a failing `XADD` leaves no job row behind.
- **SSE:** both streams emit progress and close on a terminal status.
- **Principals:** `/jobs/*` rejects an anonymous caller; `/eval-runs/{id}/events` does not.

## Deployment

A Redis container in compose; a Render key-value instance in production. Two new Render
services running the existing images with a worker command.

## The README paragraph that changes

It currently reads:

> **What it did not need.** No service mesh, no Kubernetes, no message broker, no
> distributed tracing backend.

That is about to be half wrong, and it is the repository's strongest signal of judgement, so
it gets rewritten deliberately rather than quietly. The broker moves to *what it needed, and
why*, with the measured reason: a two-hour operation behind a synchronous request, destroyed
twice during this project's own development. The mesh, Kubernetes and tracing backend stay
in *what it still does not need*.

Rewritten that way it is a stronger paragraph than before: it shows a line being moved for a
measured reason rather than a preference being restated.

## Out of scope

Parallelism tuning beyond one worker per stream — the provider's rate limit, not worker
count, is the ceiling. Scheduled or recurring jobs. A job cancellation API. Moving the
`/ask` rate limiter into Redis, which becomes possible once Redis exists but belongs with
whichever sub-project needs the answer service to scale out.
