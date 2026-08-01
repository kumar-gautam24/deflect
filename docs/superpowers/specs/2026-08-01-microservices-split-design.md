# Microservices split — design

Date: 2026-08-01
Status: approved by the project owner, who asked for hands-on microservices experience

## Context

Phase 1 shipped as a modular monolith, tagged `monolith-phase1`. That tag stays in the
repository: having the same system in both shapes is the point of the exercise, and the
comparison is more useful than either version alone.

I recommended against this split on technical grounds — the corpus is small, the
services share one transaction boundary, and the monolith already had clean internal
seams. The owner wants the operational experience, which is a legitimate reason that
the technical argument does not answer. Proceeding.

## Services

| service | port | database | owns |
| --- | --- | --- | --- |
| retrieval | 8001 | `deflect_retrieval` | `documents`, `chunks` |
| answer | 8002 | `deflect_answer` | `traces`, `escalations` |
| evals | 8003 | `deflect_evals` | `eval_runs`, `eval_results` |
| web | 3000 | none | UI, backend-for-frontend |

Database per service, no shared tables and no cross-service joins. Each service owns its
own migrations.

## Contracts

**retrieval**
- `POST /search` → `{hits: [{chunk_id, document_id, source_path, heading_path, text, score}]}`
- `POST /ingest` → `{chunks: int}`
- `GET /health`

**answer**
- `POST /answer` → the full result, synchronous. Calls retrieval `/search`.
- `POST /ask` → the same work as SSE. Wraps `/answer`.
- `GET /traces`, `GET /traces/{id}`
- `GET /health`

**evals**
- `POST /runs` → executes the golden dataset against answer `/answer`, stores the run
- `GET /eval-runs`, `GET /eval-runs/{id}`, `GET /eval-runs/diff`
- `GET /health`

## What this improves

The monolith's eval harness called `answer_question` as a function. That guaranteed
evals and production shared a code path, but only because both were in one process.
Here the eval service calls the same HTTP endpoint a real client calls, so the guarantee
now holds across a network boundary rather than by construction. This is a genuine
improvement, and the one clear win of the split.

## What this costs

- **Latency.** Answering now involves an extra network hop for retrieval.
- **Partial failure.** Retrieval being down is a new failure mode the answer service
  must handle explicitly rather than an impossible state.
- **No transaction across services.** Nothing currently needs one, which is why the
  split is feasible at all.
- **Shared code.** `packages/common` holds the LLM client and the wire schemas. Two
  services need both, so it earns its place, but it is a coupling point to watch: a
  breaking change there is a coordinated deploy.

## Non-goals

No service mesh, no Kubernetes, no message broker, no distributed tracing backend. The
services talk over plain HTTP and run under Docker Compose. Adding infrastructure that
nothing in the system needs would obscure the parts worth learning.
