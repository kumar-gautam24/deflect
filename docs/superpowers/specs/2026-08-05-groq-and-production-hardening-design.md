# Groq provider and production hardening

The first of five sub-projects in Deflect's next phase. It is deliberately the smallest and
has no dependencies on the others, because it unblocks them: the answer and evals services
currently cannot serve a real question, having no working provider key.

The remaining four, in the order they are expected to be built:

| # | Sub-project | Depends on |
| --- | --- | --- |
| **A** | **Groq provider and production hardening** (this spec) | nothing |
| C | Auth service — identity, sessions, principals (admins only) | nothing |
| D | API gateway — edge routing, auth termination, rate limiting | C |
| B | Event-driven ingest and eval runs | nothing |
| E | Kubernetes manifests | A–D |

Each gets its own spec, plan and implementation cycle. Sub-project D will partly supersede
the auth layer merged on 2026-08-05: service tokens remain for service-to-service calls, but
the gateway becomes the public edge and the per-address rate limiting moves to it.

## Part one: the Groq provider

### Why the model choice is not free

Groq's API is OpenAI-compatible, so the client is one file. But the answer service depends on
schema-constrained JSON for `{answer, cited_chunk_ids, grounded}`, and **only the `gpt-oss`
family supports `response_format: json_schema` on this account.** Probed directly against the
live API on 2026-08-05:

| model | `json_schema` |
| --- | --- |
| `openai/gpt-oss-120b` | yes |
| `openai/gpt-oss-20b` | yes |
| `llama-3.3-70b-versatile` | no |
| `llama-3.1-8b-instant` | no |
| `qwen/qwen3.6-27b` | no |

The larger Llama models are therefore unavailable to this system regardless of their quality,
which is worth recording so nobody re-litigates it later.

### Client

`packages/common/src/deflect_common/llm/groq.py`, talking to the REST endpoint through
`httpx` rather than the Groq SDK. `httpx` is already a dependency, `ollama.py` sets the
precedent for reaching a provider over plain HTTP, and the endpoint is stable and
OpenAI-shaped. Adding an SDK for one POST would be a dependency bought for nothing.

Credentials arrive as arguments, matching the rule `llm/base.py` states.

```python
SCHEMA_CAPABLE = frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"})
```

`GroqClient.__init__` raises on an empty API key **and** on a model outside `SCHEMA_CAPABLE`.
Both are startup failures rather than first-request failures, matching `bearer_guard` and the
existing provider guard: a misconfigured deploy must refuse traffic rather than take it and
produce a broken answer. Groq itself returns a 400 for an unsupported model, which would
otherwise surface as an outage.

### Which model does which job

`gpt-oss-20b` generates. `gpt-oss-120b` judges.

This preserves a property the Gemini configuration had and which is easy to lose by accident —
`flash` generated, `pro` judged. A judge no stronger than the generator produces
self-preference bias, rating its own phrasing highly, and the eval numbers stop meaning
anything. The judge stays the stronger model.

### Configuration

The answer and evals `Settings` gain `groq_api_key` — retrieval has no provider settings and
needs none, since it calls no model. The two configs already duplicate `llm_provider`,
`gemini_api_key` and `ollama_base_url` rather than sharing a base class, and this follows that
existing shape rather than introducing a fourth place for configuration to live.

Because `get_client` takes a single `api_key` argument, each service selects the right one for
its configured provider rather than passing `gemini_api_key` unconditionally as it does today:

```python
    @property
    def provider_api_key(self) -> str:
        """The credential for the configured provider. Passing gemini_api_key regardless of
        provider silently sends an empty key when LLM_PROVIDER is anything else."""
        return {"gemini": self.gemini_api_key, "groq": self.groq_api_key}.get(
            self.llm_provider, ""
        )
```

`LLM_PROVIDER` defaults to `groq`. `generation_model` and `judge_model` defaults change to the
two `gpt-oss` models.

### Cost accounting

`PRICING` in `telemetry.py` gains real Groq per-million rates for both models. Not zeroes: the
free tier costs nothing today, but `estimate_cost` returning `0.0` would make the traces
surface claim every answer was free, and the figure should stay true if the deployment ever
moves off the free tier.

### Reasoning tokens: recorded, deliberately not surfaced

Both `gpt-oss` models are reasoning models, and most of what you pay for is invisible. Measured
on 2026-08-05: the 120b returned 188 completion tokens of which **152 were reasoning**; the 20b
returned 88 of which **71 were reasoning**.

`Completion.output_tokens` takes the full `completion_tokens` count, which is correct for cost
because Groq bills all of it. Splitting reasoning out into traces would mean extending a shared
dataclass, adding a `traces` column and a migration, for a number nothing currently consumes.
Out of scope, and noted here so the omission is a decision rather than an oversight.

## Part two: production hardening

### Workers collide with the rate limiter

`_ask_limiter` is an in-process dictionary. Gunicorn with N workers means N independent
limiters and requests round-robining between them, so `ask_rate_limit_per_hour = 20` silently
becomes 20×N. Adding workers naively would weaken the abuse control that took three review
rounds to get right.

**Retrieval and evals run multiple gunicorn workers with the uvicorn worker class. The answer
service runs a single process.**

This is the correct answer on its own merits, not a workaround. Retrieval performs CPU-bound
embedding and reranking in-process, which blocks the event loop, so workers genuinely add
throughput. The answer service spends its time awaiting an HTTP call to a provider, where
async concurrency already handles load and workers add almost nothing. The limiter stays
correct because exactly one of it exists.

The migration path, written down rather than left to be discovered: when sub-project B
introduces Redis for the job queue, the limiter moves there and the answer service can scale
out honestly.

Gunicorn also brings graceful shutdown — in-flight requests finish on `SIGTERM` instead of
being severed mid-stream, which matters for an SSE endpoint.

### Liveness and readiness, and why readiness must not cascade

`/health` becomes pure liveness: the process is answering. No dependency is touched, so it
answers 200 whenever the process is alive at all.

`/ready` reports whether this service can do useful work, and checks **only its own database**.

The answer service's readiness deliberately does **not** probe retrieval. A readiness check
that follows its dependencies turns one service's outage into all three reporting unready, so
an orchestrator restarts healthy processes and the failure amplifies instead of staying
contained. Retrieval being unreachable is already surfaced correctly, as a 503 on the request
that needed it.

Render's `healthCheckPath` and CI's wait loop both move to `/ready`. The existing tests
asserting `{"status": "ok", "database": "connected"}` move with them.

### Structured logs and correlation IDs

A `contextvar` holds a request id. Middleware reads `X-Request-ID` from the incoming request or
generates one, and both `RetrievalClient` and `AnswerClient` forward it, so a single identifier
spans web → answer → retrieval.

JSON formatting comes from a small `logging.Formatter` subclass in `packages/common` — around
thirty lines — rather than `structlog`. Three services need identical log shape, which is what
that package is for, and the repository is deliberately dependency-averse.

### Metrics, and a policy-table row

`prometheus-client` exposes `/metrics` on each service, carrying default process metrics plus
request count and latency.

**`/metrics` is guarded by `require_service`.** Request volumes and latencies are operational
intelligence, and the auth spec calls its policy table "the security policy in full"; a new
public endpoint absent from that table is exactly the drift the table exists to prevent.
Scrapers are machines, so a bearer token costs them nothing.

The auth work's final review found `/docs`, `/redoc` and `/openapi.json` public on all three
services and missing from that table, which falsifies its claim that retrieval has no public
route but `/health`. All three services gain an `env: str = "development"` setting, and each passes
`docs_url=None, redoc_url=None, openapi_url=None` to `FastAPI(...)` when it is `production`.
The policy table gains a row recording that these are public in development and absent in
production. This spec closes the gap because it is already editing the same table.

### Containers

A non-root user, and base images pinned by digest rather than by the floating `python:3.12-slim`
tag.

## Part three: what happens to the published numbers

**The retrieval tables do not change.** Ablation, gate separation and the threshold sweep are
deterministic and LLM-free — they were produced without a provider key and are unaffected by
this work. They stay exactly as published.

**The generation metrics are re-run on Groq** and republished: faithfulness, answer relevance,
context relevance, escalation precision and recall.

**The existing Gemini generation numbers are relabelled, not deleted.** They are marked as
produced with `gemini-2.0-flash` generating and `gemini-2.0-pro` judging, and noted as not
reproducible without a Gemini key.

A side-by-side provider comparison was considered and rejected for now. Doing it honestly
requires one judge scoring both generators — otherwise the generator and the judge both change
and the table cannot attribute any difference to either. Generating the Gemini column needs a
working Gemini key, which this deployment does not have. A comparison built without that
control would look like a model comparison while being nothing, which is worse than no table.

## Errors

| condition | behaviour |
| --- | --- |
| empty API key for the configured provider | raises at client construction; service does not boot |
| model outside `SCHEMA_CAPABLE` | raises at client construction; service does not boot |
| Groq returns a non-2xx | surfaces as it does today for Gemini — no new handling |
| `/metrics` without a service credential | 401, matching every other guarded route |

## Testing

Everything below runs with no network and no API key, so a stranger who clones the repository
can run the whole suite.

- **Groq client** against `httpx.MockTransport`: the schema is passed through as `json_schema`;
  `prompt_tokens` and `completion_tokens` map onto `Completion` correctly — the field names
  differ from Gemini's, which is precisely the kind of mismatch that silently zeroes a cost
  column; an empty key raises at construction; a model outside `SCHEMA_CAPABLE` raises at
  construction.
- **JSON formatter**: emits a parseable line carrying the correlation id.
- **Correlation id**: an inbound `X-Request-ID` is reused rather than replaced, and one is
  generated when absent.
- **Liveness and readiness**, one each per service, including a test asserting the answer
  service's `/ready` still succeeds while retrieval is unreachable. That non-cascading property
  is easy to lose in a later refactor and cheap to pin now.
- **`/metrics`** returns 401 without a credential.
- **Gunicorn** is verified by a container smoke test, not a unit test: worker supervision is not
  observable in-process.

## Out of scope

Reasoning-token accounting in traces. The auth service, API gateway, job queue and Kubernetes
manifests — each is a separate sub-project. Moving the rate limiter to shared storage, which
belongs with sub-project B when Redis arrives.
