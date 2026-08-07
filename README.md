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
| `auth` | issues and revokes the opaque sessions everything else trusts |
| `gateway` | the public edge: routing, rate limits and credential checks in one place |
| `web` | Next.js UI for asking, browsing eval runs, and reading traces |

Five FastAPI services — four with a database each, and a gateway with none — Postgres with
pgvector, Groq for generation and judging, and local models for embedding and reranking.

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
| `auth` | 8004 | `deflect_auth` | `admin_users`, `sessions` |
| `gateway` | 8000 | none | routing and edge policy |
| `web` | 3000 | none | UI and backend-for-frontend |

```
web ──/api/ask──────> gateway ──> answer ──/search──> retrieval
web ──/traces───────> gateway ──> answer
web ──/eval-runs────> gateway ──> evals ──/answer──> answer
web ──/api/auth/login, /auth/logout──────────────────────> gateway ──> auth
```

`apps/web` talks to the gateway and nothing else — `GATEWAY_URL` is the only backend
address it holds. Login and logout were the last exception (they called `auth` directly
until this was caught and fixed) and now go through the gateway's own rate-limited
`/auth/login` and session-guarded `/auth/logout` like everything else. Service-to-service
calls (`answer` → `retrieval`, `evals` → `answer`) are unchanged: they cross the private
network directly, the same as before the gateway existed — the gateway fronts the public
edge, not internal calls.

**This isn't the only door, and the README says so rather than implying otherwise.**
`retrieval`, `answer`, `evals` and `auth` all remain `type: web` on Render — reachable at
their own public URLs, not only through the gateway. The private split (`type: pserv`)
was attempted and abandoned: Render Private Services need a paid instance type that
couldn't be confirmed against the connected account, and guessing would have turned this
paragraph into a claim the next reader has no way to check. So the gateway centralises
policy and is the documented entry point, but it is a front door beside other doors, not
the only one — a client that skips it and calls a service's public URL directly still
gets a real answer, just without the gateway's rate limiting or circuit breaker. What
actually protects each service is unchanged by that: `principal_guard` resolves and
checks every credential itself rather than trusting the gateway's verdict, and each
service's own `ENV` check keeps its interactive docs off in production regardless of
which door a request came through.

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

**What it needed, and why.** A message broker — eventually, and not at first. Ingest and
eval runs were synchronous: a full eval run is about two hours against a free-tier quota,
held open by one HTTP request. During this project's own development that run was
destroyed twice, once by a container rebuild and once by a client timeout, losing about
forty-five minutes each time. Redis Streams now carries both as jobs.

Redis carries work and Postgres carries truth: every piece of job state lives in the
owning service's own database, so job status still answers when the broker is down and
there is still no shared table between services.

Parallel workers do **not** make a run faster — the provider's rate limit is the ceiling,
not worker count. What the queue buys is retry granularity, visible progress, and survival
across a restart.

**What it needed next, and why that's a different kind of "needed."** A gateway — but
say plainly why, because it is not the same reason the broker exists. The honest reason is
breadth: this project is built for a resume and for learning, not for end users, and
demonstrating that a gateway is understood has value here that it would not have in a
product with real traffic. There was also a real problem to point it at: `retrieval`,
`answer`, `evals` and `auth` were each their own `type: web` service on Render with their
own public URL, so every edge policy — rate limiting, CORS, docs exposure, credential
handling — had to be enforced four times or not at all, and a policy remembered in four
places is a policy that eventually gets missed in one. Both statements are true, and
neither is allowed to hide the other. But unlike the message broker, which earned its
place only after two eval runs were destroyed in practice, **nothing forced this one.** No
run failed, no request timed out, no incident preceded it. This document's credibility
comes from having once said "this was not needed"; it survives only if it also says, here,
"this one was chosen, not forced."

**What it still does not need.** No Kubernetes, no distributed tracing backend, and —
despite the gateway above — no service mesh either. The two solve different problems: a
gateway terminates public traffic at one place, deciding what a stranger may reach before
any service sees the request; a mesh governs traffic *between* services that already trust
each other, adding things like mTLS and per-hop retries to calls that were already
private. Five services calling a handful of fixed, known URLs over a private network have
no use for the second. Adding infrastructure the system has no use for would obscure the
parts worth understanding — and note that the broker earned its place only after the
synchronous version had failed twice in practice, not because a diagram looked better with
one.

## Security

The gateway is where this answer now lives: `services/gateway/src/gateway/routes.py` is a
dependency-free table, deliberately readable without following an import into an HTTP
client — a path absent from it is a 404 **at the gateway**, which never becomes an
upstream request, a stronger guarantee than a path that is merely guarded.

| method | path | upstream | requires | notes |
| --- | --- | --- | --- | --- |
| POST | `/ask` | answer | public | rate limited (`ask`), streamed |
| POST | `/auth/login` | auth | public | rate limited (`login`) |
| POST | `/auth/logout` | auth | session | |
| POST | `/auth/logout-all` | auth | session | |
| GET | `/auth/me` | auth | session | |
| GET | `/traces` | answer | viewer | |
| GET | `/traces/{trace_id}` | answer | viewer | |
| POST | `/search` | retrieval | service | |
| GET | `/documents` | retrieval | service | |
| POST | `/ingest` | retrieval | operator | returns `202` |
| GET | `/jobs/{job_id}` | retrieval | operator | |
| GET | `/jobs/{job_id}/events` | retrieval | operator | streamed |
| POST | `/runs` | evals | operator | returns `202` |
| GET | `/eval-runs` | evals | public | |
| GET | `/eval-runs/diff` | evals | public | |
| GET | `/eval-runs/{run_id}` | evals | public | |
| GET | `/eval-runs/{run_id}/events` | evals | public | streamed |

`/health` and `/ready` on the gateway are its own, not proxied — a readiness check that
probed four upstreams would turn one sick service into an unready edge. `/metrics` is not
in the table at all, so it is unroutable rather than protected, and stays reachable only
inside the private network for a scraper. `/docs`, `/redoc` and `/openapi.json` are
appended to the table only when `ENV` is not `production`.

### Per-service guards — defence in depth, not the primary control

Nothing below was deleted when the gateway arrived. Every route the table above allows
still runs its own check on the far side, because a request that reaches a service by any
other path — direct to its still-public Render URL, or straight past a gateway bug — must
still be refused on its own. The gateway's verdict is duplicated, not trusted.

| service | route | principal |
| --- | --- | --- |
| all four | `GET /health` (liveness), `GET /ready` (readiness) | public |
| all four | `GET /metrics` | service |
| all four | `/docs`, `/redoc`, `/openapi.json` | public in development, absent when `ENV=production` |
| retrieval | `GET /documents` | service |
| retrieval | `POST /search` | service |
| retrieval | `POST /ingest` | operator, plus path confinement — returns `202` |
| retrieval | `GET /jobs/{job_id}`, `/jobs/{job_id}/events` | operator |
| answer | `POST /ask` | public |
| answer | `POST /answer` | service |
| answer | `GET /traces`, `GET /traces/{id}` | viewer |
| evals | `POST /runs` | operator — returns `202` |
| evals | `GET /eval-runs`, `/eval-runs/diff`, `/eval-runs/{id}`, `/eval-runs/{id}/events` | public |
| auth | `POST /auth/login` | public |
| auth | `POST /auth/logout`, `/auth/logout-all`, `GET /auth/me` | valid session |

Two static bearer tokens carried in the environment, enforced by one dependency in
`packages/common`. An API-key table with per-caller revocation would have needed either a
shared table or one duplicated into all four databases, trading the invariant the split
exists to demonstrate for machinery a single-operator deployment will not use.

Every service refuses to start with either token unset, so a misconfigured deploy never
takes traffic. `/ingest` additionally resolves its requested root and rejects anything
outside `CORPUS_ROOT`: a leaked operator token should not become a filesystem read
primitive.

`/ask` is open, because the demo is. What used to be a per-address sliding window in
`answer` is now a leaky bucket at the gateway — 20 requests an hour per address, smoothed
to a burst of 5 rather than letting the whole hour go in one second — and `answer` keeps
only the daily cap, counted from `traces`, because that one counts rows in `answer`'s own
database and is a spend bound rather than an abuse bound. A botnet has many real
addresses, so only the daily cap actually protects the bill; the two stay sized together
regardless of which service enforces which — 20 an hour over 24 hours is 480, just under
the 500 daily ceiling, so no single address can exhaust a day's budget. `/auth/login`
moved the same way: the per-address window is now the gateway's `login` bucket (60/hour,
burst 10), and account lockout — five wrong passwords, fifteen minutes, per account rather
than per address — stays the precise control in `auth`, described below.

### The address the gateway trusts

`deflect_common.ratelimit` keeps two functions for "the caller's address" —
`client_address` and `edge_address` — rather than one function with a boolean, because the
trust rule inverts depending on where a service sits relative to the public internet.
`client_address(trust_forwarded=True)` takes the **leftmost** `X-Forwarded-For` entry:
right for a service standing behind exactly one forwarder it trusts to compute the
visitor's address itself and *overwrite* the header rather than relay it, which is the
shape the web BFF still has when it calls the gateway for `/ask`. `edge_address(request,
trusted_hops)` takes the **rightmost** *n* entries instead, because the gateway is the
opposite shape: it sits behind Render's own load balancer, which **appends** the real
client to whatever arrived rather than replacing it, so the leftmost entry there is
whatever the caller sent and only the entry the trusted proxy itself wrote is safe.
Collapsing these into one flag would invite a future caller to pick the wrong end and
never find out — the two rules aren't a spectrum, they're opposites, and getting this
backwards has already cost something real once: an earlier version of this project trusted
`uvicorn`'s default proxy handling, which rewrites `request.client` from an unvalidated
forwarded header, and the limiter it was meant to protect was provably decorative until
that was caught — over a real HTTP request, because the in-process test transport never
passes through the middleware that does the rewriting.

### Who did this

Two shared tokens answer whether a caller is allowed. They cannot answer who, which is the
question that matters as soon as more than one person can trigger an ingest or an eval run.

An `auth` service issues opaque sessions — 32 random bytes, stored only as a SHA-256, so a
dump of its database yields nothing replayable. Services read those sessions from Redis
rather than calling auth, so none of them depends on auth being reachable to serve its own
data.

That Redis entry carries the session's own twelve-hour lifetime, because it is the only
thing the other services consult. A shorter cache TTL is tempting as a revocation backstop,
but it would not bound revocation — it would quietly *become* the session length, logging an
admin out everywhere while their cookie and their database row both still called the session
live. Revocation is therefore the explicit delete that logout performs, and the cost is that
a delete which never lands leaves a session usable until it would have expired anyway.

The consequence worth stating plainly: **Redis is now an authentication authority.** Before
the auth service it held job messages, and a compromise meant forged work. Now anything that
can write a `session:<sha256>` key mints an admin session accepted by every service. On
Render it is a private, authenticated Key Value instance; locally it is password-protected
in `docker-compose.yml` rather than left open on a published port.

Two roles draw the line that exists in this system: a **viewer** can read traces and eval
runs, an **admin** can also spend two hours of provider quota by starting a run.

Five wrong passwords lock an account for fifteen minutes. A locked account is refused with
the same `401` and the same body as a wrong password, and no `Retry-After`: the lockout is
reachable only for an account that exists, so any reply that differed would answer "is this
address an admin?" in five requests. The admin is not told why; the operator finds it in the
auth service's logs, which record the user id and nothing else.

**What the gateway costs the audit trail.** The `sessions.ip` column exists so an operator
can see where a login came from — recorded, not trusted for any decision. The gateway
strips every inbound `X-Forwarded-*` header before dialling an upstream and sets none of
its own, on purpose: nothing downstream is meant to trust a forwarded value it did not
compute itself. `auth`'s login route records `request.client.host`, the address of
whichever process opened the TCP connection to it directly. Login now reaches `/auth/login`
through the gateway's route table above for every caller, including `apps/web`'s own login
form — so that process is always the gateway, never the visitor, and `sessions.ip` records
the same private-network address for every login. The column still writes a value; it just
no longer answers the question it was built to answer. This is not fixed here, on purpose:
the fix is either teach the gateway to compute and forward the real address the way the web
BFF already does for `/ask`, and teach `auth` to trust it from that one edge — or accept the
loss and drop the column, and say why in the code rather than let it keep implying data it
no longer holds. Recording the choice is the point of this paragraph; making it isn't this
task's job.

Accounts are created with `python -m auth.cli create-admin --email you@example.com`. There
is no signup route: this system has operators, not users.

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
docker compose up -d --build      # postgres plus all five services (the gateway has no
                                   # migration of its own -- it owns no database)
# Host ports are overridable if one is taken: RETRIEVAL_PORT=9001 docker compose up

# Migrate each service that has a database, then ingest the corpus through retrieval.
for s in retrieval answer evals auth; do docker compose exec -T $s alembic upgrade head; done

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
2. **Render** — deploy from `render.yaml`. It wires `RETRIEVAL_URL` into `answer`,
   `ANSWER_URL` into `evals`, and all four upstream URLs into the gateway; set each
   `DATABASE_URL` (Neon pooled string, with the `postgresql+asyncpg://` prefix),
   `GROQ_API_KEY`, `WEB_ORIGIN`, `ENV=production`, `SERVICE_TOKEN`, and `OPERATOR_TOKEN`.
   The same `SERVICE_TOKEN` must be given to all five services, since each one both
   presents it and checks it on incoming calls. The gateway also takes
   `TRUSTED_PROXY_HOPS` (`1` on Render, where its own load balancer is the one hop in
   front) — see "The address the gateway trusts" below for why that number matters.
3. **Vercel** — deploy `apps/web` with `GATEWAY_URL` set to the Render gateway URL. It's
   the only backend address `apps/web` holds: `/ask`, the traces page, the eval dashboard,
   and login/logout all go through it — see the request diagram under Architecture. Plus
   `SERVICE_TOKEN`: `/api/ask` refuses to proxy a question at all if it's unset.

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
two workers. **The gateway stays pinned to one, deliberately** — its `ask` and `login`
leaky buckets are in-process dictionaries, so N workers would turn one bucket into N and
every limit would silently multiply by worker count. `RedisLeakyBucket` already exists,
tested for exact agreement with the in-memory one, for the day that stops being
acceptable. `answer` and `auth` are also pinned to one, but the per-address limiters that
originally justified it moved to the gateway along with `/ask` and `/auth/login`; what
each keeps in-process now — `answer`'s daily cap, `auth`'s account lockout — is a database
query, safe under any worker count. Neither Dockerfile has been revisited to say so.

`/health` is liveness and touches no dependency — a probe that queried the database would
have an orchestrator restart healthy processes during a Postgres hiccup. `/ready` checks
only the service's own database, and deliberately does not probe the services it calls: a
readiness check that follows its dependencies turns one outage into every dependent
service reporting unready, amplifying the failure instead of containing it. The gateway's
own `/ready` follows the same rule for the same reason — it does not probe its four
upstreams either. A sick upstream is the circuit breaker's job, not the readiness probe's.

Every request carries an `X-Request-ID`, adopted from the caller when present and minted
otherwise, forwarded across each service hop and included in every JSON log line — so one
request's path through the gateway and whichever services it touches reassembles into a
single story.

Containers run as a non-root user and pin their base image by digest.

### Tests

```bash
for s in retrieval answer evals auth gateway; do (cd services/$s && uv run pytest -q); done
(cd packages/common && uv run pytest -q)
cd apps/web && npm test
```

317 service tests (70 retrieval, 54 answer, 82 evals, 42 auth, 69 gateway), 114 for the
shared contracts, and 21 component tests. Each service's suite runs against its own test
database, except the gateway's, which needs none — it owns no data, so its tests fake the
four upstreams instead. The answer service's tests use a fake retrieval, and the eval
service's tests use a fake answer service, so neither needs a vector database, an
embedding model or a provider key. That isolation is a direct benefit of the split.
