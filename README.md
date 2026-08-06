# Deflect

**A support assistant for FastAPI's documentation that answers with citations — or admits
it doesn't know.**

Ask it something the docs cover, and it answers and shows you exactly which passages it
used:

```
$ curl -X POST localhost:8002/ask -d '{"question": "How do I declare a dependency?"}'

  You declare a dependency by writing a function and passing it to Depends() as a
  default value in your path operation. FastAPI resolves it before your handler runs.

  Sources:
    tutorial/dependencies/index.md      Dependencies > First steps
    tutorial/dependencies/classes.md    Classes as Dependencies
    advanced/security/oauth2-scopes.md  Declaring scopes in dependencies
```

Ask it something the docs *don't* cover, and it refuses rather than inventing an answer:

```
$ curl -X POST localhost:8002/ask -d '{"question": "How much does FastAPI cost per seat?"}'

  escalated: true
  reason:    low_retrieval_score

  No passage in the documentation answers this. Handing off to a human.
```

Both responses are shown rendered for readability — the endpoint actually streams
server-sent events, and the refusal reason is one of `no_results`, `low_retrieval_score`,
`ambiguous_retrieval` or `ungrounded_answer`, so a trace records *which* check refused.

That second behaviour is the whole point.

## The problem it solves

**A confidently wrong answer costs more than no answer.** If a support bot invents
something, a human has to notice the error, undo whatever the user did because of it, and
rebuild the user's trust. Saying "I don't know" is cheaper than any of that.

Most retrieval systems optimise one number: how often they answer correctly. Deflect
tracks two, in tension:

- **wrongly answered** — it guessed when it should have deferred
- **wrongly refused** — it deferred when it had the material to answer

Tuning either one alone is easy and useless. A system that refuses everything never lies;
a system that answers everything is never unhelpful. The work is in choosing where to sit
between them, deliberately, and being able to prove where you sat.

Which is why the honest summary of this project is: **the interesting part is not the
retrieval pipeline, it's the eval harness that tells you when the pipeline is wrong** — and
it gates CI.

## How it works

**1. Ingest.** FastAPI's documentation — 155 markdown files — is split along heading
boundaries rather than into fixed-size windows, so every chunk keeps its heading path
(`Tutorial > Dependencies > Sub-dependencies`). That way a citation names something a human
can actually navigate to. 2,370 chunks, embedded and stored in Postgres with pgvector.

**2. Retrieve.** Two searches run over every question: dense vector similarity, which
understands meaning, and lexical keyword matching, which catches exact tokens like `422` and
`Depends` that embeddings blur. Their rankings are fused, then a cross-encoder reranks the
survivors.

**3. Decide whether to answer.** This is the part worth reading the code for. Reranking
actually makes the *ranking* slightly worse — and it is kept anyway, because it is the only
stage in the pipeline that produces a score you can compare against a threshold. Everything
else can tell you what is most relevant; only this can tell you whether anything is relevant
enough. Below the threshold, the system refuses.

**4. Answer.** The model writes a reply, and the response schema restricts its citations to
chunk IDs that were actually retrieved. It cannot cite a source it was never shown, because
the decoder will not let it.

**5. Measure.** 80 test questions, of which **15 have no answer anywhere in the corpus** and
must be refused. Those 15 are what make the refusal behaviour measurable instead of
aspirational.

## What's running

| piece | what it is |
| --- | --- |
| `retrieval` | owns the corpus, the embeddings and the search |
| `answer` | runs the gate and the model, records every question as a trace |
| `evals` | runs the golden dataset and scores it |
| `web` | Next.js UI for asking, browsing eval runs, and reading traces |

Three FastAPI services with a database each, Postgres with pgvector, Groq for generation and
judging, and local models for embedding and reranking.

## Results

All numbers below are measured against the ingested corpus (fastapi/fastapi at
`95f8322`, `docs/en/docs`: 155 documents, 2,370 chunks) and the 80-item golden dataset
in `evals/golden.yaml`. Reproduce them with the scripts named under each table.

### Retrieval ablation

`services/evals/scripts/ablate.py`, over the 65 answerable items.

| variant | hit@5 | MRR | precision@5 |
| --- | --- | --- | --- |
| dense only | 0.892 | 0.744 | 0.206 |
| lexical only | 0.323 | 0.291 | 0.126 |
| hybrid | **0.892** | **0.762** | 0.203 |
| hybrid + rerank | 0.862 | 0.706 | 0.194 |

Two honest readings of this table:

**Hybrid retrieval earns its place, modestly.** Lexical search alone is poor, but
fusing it with dense search lifts MRR by 1.8 points at identical hit@5. It contributes
rank signal on exact tokens such as `422` and `Depends` that embeddings blur.

**Reranking makes retrieval worse.** It costs 3 points of hit@5 and 5.6 of MRR. RRF
already orders the top 20 well, and reranking reshuffles them and keeps 5, so any
correct document the cross-encoder ranks sixth or lower is pushed out of the window.
Three stronger cross-encoders were tried; none beat plain hybrid on MRR:

Produced by setting `RERANK_MODEL` and re-running the ablation, rather than by a
script of its own:

| reranker | hit@5 | MRR |
| --- | --- | --- |
| none (hybrid) | 0.892 | **0.762** |
| `ms-marco-MiniLM-L-6-v2` | 0.862 | 0.706 |
| `ms-marco-MiniLM-L-12-v2` | 0.892 | 0.736 |
| `BAAI/bge-reranker-base` | 0.877 | 0.746 |
| `jina-reranker-v1-turbo-en` | **0.908** | 0.739 |

### Why the reranker stays anyway

`services/evals/scripts/gate_separation.py`

| score source | answerable median | unanswerable median | separation |
| --- | --- | --- | --- |
| RRF fused (no rerank) | 0.0164 | 0.0164 | 0.0000 |
| cross-encoder rerank | 4.6359 | -1.0445 | **5.6804** |

Reciprocal Rank Fusion scores carry no relevance information at all. The top-ranked
chunk always scores `1/(k+1)` regardless of whether it answers the question, which is
why every query in the dataset produces the same 0.0164. There is nothing to threshold
on.

The cross-encoder is not in this pipeline to improve retrieval. It is the only stage
that produces a calibrated relevance score, and the escalation gate is built on it.
Removing it would cost 3 points of hit@5 and remove the ability to refuse at all.

`ms-marco-MiniLM-L-6-v2` is kept over `jina-turbo` despite jina's better hit@5: its
median separation is 5.68 against jina's 0.78, which makes the threshold far less
sensitive to where it is set.

### Choosing the operating point

`services/evals/scripts/sweep_thresholds.py`. Abridged; the script prints the full sweep
from -8.0 to +8.0.

| min_top_score | answered | wrongly refused | wrongly answered |
| --- | --- | --- | --- |
| -1.00 | 0.92 | 0.08 | 0.47 |
| 0.00 | 0.91 | 0.09 | 0.33 |
| 1.50 | 0.85 | 0.15 | 0.13 |
| **2.00** | **0.83** | **0.17** | **0.13** |
| 3.50 | 0.71 | 0.29 | 0.07 |
| 5.00 | 0.45 | 0.55 | 0.07 |
| 8.00 | 0.08 | 0.92 | 0.00 |

The operating point is **2.0**: it answers 83% of answerable questions while passing
only 13% of unanswerable ones through to the second check.

Two caveats that belong with this number rather than buried:

- The sweep holds `grounded=True` to isolate the retrieval signal. In production the
  model must also report that its answer is supported by the retrieved passages, so
  the end-to-end wrongly-answered rate is lower than this table shows. Retrieval score
  is the coarse filter; groundedness is the fine one.
- With 15 unanswerable items, each one is 6.7 percentage points. The resolution of the
  right-hand column is coarse, and 0.07 means a single question.

## Architecture

| service | port | database | owns |
| --- | --- | --- | --- |
| `retrieval` | 8001 | `deflect_retrieval` | `documents`, `chunks` |
| `answer` | 8002 | `deflect_answer` | `traces`, `escalations` |
| `evals` | 8003 | `deflect_evals` | `eval_runs`, `eval_results` |
| `web` | 3000 | none | UI and backend-for-frontend |

```
web ──/api/ask──> answer ──/search──> retrieval
                    ^
evals ──/answer─────┘
```

Database per service. No shared tables, no cross-service joins, and each service owns
its own migrations. `packages/common` holds the wire schemas both sides of every call
import, so a contract change breaks compilation rather than failing at runtime.

The web app never calls a model. It proxies an SSE stream from the answer service, so
provider keys stay server-side.

Chunking follows markdown headings rather than a fixed window, and each chunk keeps its
heading path (`Tutorial > Dependencies > Sub-dependencies`) so a citation names
something a human can navigate to.

### On the split

This started as a modular monolith, still available at the `monolith-phase1` tag. The
comparison is the interesting part, so both shapes are kept.

**What the split improved.** The monolith's eval harness called the answer function
directly. That guaranteed evals and production shared a code path, but only because
both lived in one process. The eval service now calls the same HTTP endpoint a real
client calls, so the guarantee survives a network boundary instead of depending on
deployment topology.

**What it cost.** Answering takes an extra hop. Retrieval being unreachable is a new
failure mode, surfaced as a 503 rather than an answer built on no context — a state
that was previously impossible. There is no transaction across services, which is
feasible only because nothing here needs one. And `packages/common` is a coupling
point: a breaking change there is a coordinated deploy.

**What it did not need.** No service mesh, no Kubernetes, no message broker, no
distributed tracing backend. Adding infrastructure the system has no use for would
obscure the parts worth understanding.

## Security

| service | route | principal |
| --- | --- | --- |
| all three | `GET /health` (liveness), `GET /ready` (readiness) | public |
| all three | `GET /metrics` | service |
| all three | `/docs`, `/redoc`, `/openapi.json` | public in development, absent when `ENV=production` |
| retrieval | `GET /documents` | service |
| retrieval | `POST /search` | service |
| retrieval | `POST /ingest` | operator, plus path confinement |
| answer | `POST /ask` | public, rate limited |
| answer | `POST /answer` | service |
| answer | `GET /traces`, `GET /traces/{id}` | operator |
| evals | `POST /runs` | operator |
| evals | `GET /eval-runs`, `/eval-runs/diff`, `/eval-runs/{id}` | public |

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

## Evals

`evals/golden.yaml` holds 80 items: 65 answerable, and 15 that no document in the
corpus answers, which must be refused. A test validates every `expected_sources` path
against the retrieval service's `/documents` endpoint, because a typo there would look
like a permanent retrieval regression rather than a bad label. It skips when that
service is unreachable so the unit suite stays runnable alone; CI sets
`REQUIRE_CORPUS_CHECK` so an unreachable service fails the build instead.

### Which model produced these numbers

**Generation metrics have not been published for this deployment yet.** When they are,
they will use `openai/gpt-oss-20b` generating and `openai/gpt-oss-120b` judging, both on
Groq's free tier. The judge is deliberately the stronger model: a judge no stronger than
the generator rates its own phrasing highly, and the numbers stop meaning anything.

The run is slow rather than expensive — Groq's free tier allows 8,000 tokens a minute, so
the full 80-item dataset takes roughly 110 minutes.

A side-by-side provider comparison was considered and rejected. Doing it honestly needs
one judge scoring both generators; otherwise the generator and the judge both change and
the table cannot attribute a difference to either, which is worse than one honest column.

**The retrieval tables above are unaffected.** They are deterministic and LLM-free, and
were produced without any provider key at all.

Metrics are split into two families:

- **Retrieval**, deterministic and LLM-free: hit@5, MRR, precision@5
- **Generation**, LLM-as-judge: faithfulness, answer relevance, context relevance,
  plus escalation precision and recall

The split is the point. When a run regresses, the deterministic metrics say
immediately whether retrieval or generation broke. Runs are stored with their commit,
prompt version, model and retrieval config, and the dashboard diffs any two.

CI runs a 10-item smoke set on pushes to `main` and fails the build when faithfulness
drops. It is not run per pull request: fourteen minutes against a free-tier quota trains
people to ignore a gate. The token-free checks in that job — ingest and the golden-dataset
validation — still run on every pull request. The subset is stratified rather than the first ten items, because
the unanswerable questions sit at the end of the file and a head-of-list slice would
never exercise refusal. The full dataset runs nightly.

## Running it

```bash
docker compose up -d --build      # postgres plus all three services
# Host ports are overridable if one is taken: RETRIEVAL_PORT=9001 docker compose up

# Migrate each service, then ingest the corpus through the retrieval service.
for s in retrieval answer evals; do docker compose exec -T $s alembic upgrade head; done

git clone --depth 1 https://github.com/fastapi/fastapi /tmp/fastapi-src
docker compose cp /tmp/fastapi-src/docs/en/docs retrieval:/corpus
curl -X POST localhost:8001/ingest -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${OPERATOR_TOKEN:-dev-operator-token}" \
  -d "{\"root\": \"/corpus\", \"commit_sha\": \"$(git -C /tmp/fastapi-src rev-parse HEAD)\"}"
```

The compose file creates a database per service on first start. To run a service
directly instead, `cd services/<name> && uv sync && uv run uvicorn <name>.main:app`.

```bash
cd apps/web
npm install
npm run dev
```

## Deploying

1. **Neon** — one database per service. Run `CREATE EXTENSION vector` on the retrieval
   one only, then apply each service's migrations against its own `DATABASE_URL`.
2. **Render** — deploy from `render.yaml`. It wires `RETRIEVAL_URL` and `ANSWER_URL`
   between services; set each `DATABASE_URL` (Neon pooled string, with the
   `postgresql+asyncpg://` prefix), `GROQ_API_KEY`, `WEB_ORIGIN`, `ENV=production`,
   `SERVICE_TOKEN`, and
   `OPERATOR_TOKEN`. The same `SERVICE_TOKEN` must be given to all three services, since
   each one both presents it and checks it on incoming calls.
3. **Vercel** — deploy `apps/web` with `ANSWER_URL` and `EVALS_URL` set to the
   corresponding Render URLs, plus `OPERATOR_TOKEN` and `SERVICE_TOKEN`.

`WEB_ORIGIN` is what the answer service's CORS allowlist reads, so the deployed
frontend must be named there or browser requests are rejected.

`GROQ_API_KEY` is required by the answer and evals services, which refuse to start
without it rather than failing on the first request — and refuse equally if the configured
model cannot produce schema-constrained output, since the answer path cannot work without
it. The retrieval service needs no provider key, and neither do the ablation and threshold
sweep: every retrieval table above was produced without one.

Groq's free tier allows 8,000 tokens a minute, which is what makes an eval run slow rather
than expensive — roughly 110 minutes for the full 80-item dataset. The client retries a 429
honouring `Retry-After`, so a run pauses instead of dying partway and leaving a partial
row. For the same reason the ten-item CI gate runs on pushes to `main` and nightly rather
than on every pull request: fourteen minutes per PR trains people to ignore a gate.

### Running in production

Each service runs under gunicorn with the uvicorn worker class. Retrieval and evals use
two workers; **the answer service uses exactly one, deliberately.** Its per-address rate
limiter is an in-process dictionary, so N workers would mean N independent limiters and a
twenty-per-hour limit would silently become twenty N. That service awaits a model provider
rather than burning CPU, so workers would buy it almost nothing anyway. Raising it weakens
the abuse control; when a job queue brings shared storage, the limiter moves there.

`/health` is liveness and touches no dependency — a probe that queried the database would
have an orchestrator restart healthy processes during a Postgres hiccup. `/ready` checks
only the service's own database, and deliberately does not probe the services it calls: a
readiness check that follows its dependencies turns one outage into all three reporting
unready, amplifying the failure instead of containing it.

Every request carries an `X-Request-ID`, adopted from the caller when present and minted
otherwise, forwarded across each service hop and included in every JSON log line — so one
question's path through three services reassembles into a single story.

Containers run as a non-root user and pin their base image by digest.

### Tests

```bash
for s in retrieval answer evals; do (cd services/$s && uv run pytest -q); done
(cd packages/common && uv run pytest -q)
cd apps/web && npm test
```

161 service tests (49 retrieval, 61 answer, 51 evals), 48 for the shared contracts, and
24 component tests. Each service's suite runs against its own test
database and needs nothing else: the answer service's tests use a fake retrieval, and
the eval service's tests use a fake answer service, so neither needs a vector database,
an embedding model or a provider key. That isolation is a direct benefit of the split.
