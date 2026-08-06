# API gateway

Sub-project D of Deflect's next phase. A (Groq and production hardening), B (event-driven
jobs) and C (admin auth service) are merged. E (Kubernetes) remains optional.

## Why this exists

**The honest reason first: breadth.** Deflect is built for a resume and for learning, not for
end users, so demonstrating that a gateway is understood has value here that it would not
have in a product. This document says so plainly rather than inventing an operational
emergency, because the repository's strongest signal of judgement is a README paragraph that
refuses infrastructure the system has no use for — and that paragraph is worth more than the
gateway is.

There is also a real job for it to do, which is what keeps it from being decoration. Today
`retrieval`, `answer`, `evals` and `auth` are each `type: web` on Render with their own
public URL. Every edge policy — rate limiting, CORS, docs exposure, credential handling — is
therefore enforced four times or not at all. The still-open item *"`ENV` unset in Render
silently leaves `/docs` public"* is one instance of that shape: a policy that must be
remembered in four places is a policy that will be missed in one.

So the gateway is justified on breadth, and it is given a genuine problem to solve. Both
statements go in the README. Neither is allowed to hide the other.

**What this deliberately does not claim.** Nothing has failed in production. Unlike the
message broker — which earned its place only after two eval runs were destroyed in practice —
this is not a response to an incident, and the README must not imply that it is.

## Decisions

| decision | choice | why not the alternative |
| --- | --- | --- |
| build or configure | write it in FastAPI | Traefik or Caddy would be the production-honest answer and handles TLS and streaming for free, but configuring one demonstrates that its documentation was read. Writing one demonstrates knowing what a gateway does. For this project's purpose the second is worth more. |
| trust model | gateway **and** upstream both check | Terminating the credential at the gateway and passing `X-Deflect-Principal` would delete more code, but it makes every service's security rest on a header being unforgeable — the exact class of defect commit `a38fafe` fixed. |
| principal forwarding | forward the original credential unchanged | Re-minting it as a signed assertion is closer to what a real mesh does and avoids the second Redis read, but it adds key handling, clock skew and a new verify path for a lookup that short-circuits before Redis on both token comparisons anyway. |
| gateway state | Redis only, no database | The gateway owns no data. Giving it a database to satisfy a pattern would break "database per service" by inverting it — the rule is that a service owning data owns its database, not that every service has one. |
| where `apps/web` sits | BFF stays, retargeted at the gateway | The session cookie is httpOnly on the web origin, so something must translate cookie to Bearer. Doing it in the BFF avoids cross-origin credentialed requests between a Vercel domain and a Render one, and keeps a token out of the browser. |
| route configuration | a declarative table | Decorators scattered across modules make the security posture something you assemble by reading. A table makes it something you read. |

## Architecture

A fifth service, `services/gateway`, on port 8000. It owns no tables and has no migrations.

```
browser ──cookie──> web (Vercel)        BFF: cookie -> Bearer
                      │
                      ▼
                   gateway (Render — the only public service)
                      │
        ┌─────────────┼─────────────┬──────────────┐
        ▼             ▼             ▼              ▼
    retrieval      answer         evals          auth
                                                    │
                  all four private; all still run principal_guard
```

Four modules, each with one job and testable alone:

| module | responsibility | depends on |
| --- | --- | --- |
| `routes.py` | the route table: path, method, upstream, required principal, timeout, streaming | nothing |
| `proxy.py` | streaming passthrough over httpx, header hygiene | `routes` |
| `limits.py` | Redis sliding window keyed by client address | `redis` |
| `principal.py` | coarse allow/deny, then forward the credential untouched | `deflect_common.auth` |

Keeping `routes.py` dependency-free is deliberate: the table is the artifact a reviewer reads
to answer "what is exposed, and to whom", and it should be readable without following imports
into an HTTP client.

### The route table

```python
Route("POST", "/ask",               answer,    principal=None,       timeout=30, stream=True, limit=ASK)
Route("POST", "/auth/login",        auth,      principal=None,       timeout=10,              limit=LOGIN)
Route("POST", "/auth/logout",       auth,      principal="session",  timeout=10)
Route("POST", "/auth/logout-all",   auth,      principal="session",  timeout=10)
Route("GET",  "/auth/me",           auth,      principal="session",  timeout=10)
Route("GET",  "/traces",            answer,    principal="viewer",   timeout=15)
Route("GET",  "/traces/{id}",       answer,    principal="viewer",   timeout=15)
Route("POST", "/search",            retrieval, principal="service",  timeout=15)
Route("GET",  "/documents",         retrieval, principal="service",  timeout=15)
Route("POST", "/ingest",            retrieval, principal="operator", timeout=15)
Route("GET",  "/jobs/{id}",         retrieval, principal="operator", timeout=15)
Route("GET",  "/jobs/{id}/events",  retrieval, principal="operator",             stream=True)
Route("POST", "/runs",              evals,     principal="operator", timeout=15)
Route("GET",  "/eval-runs",         evals,     principal=None,       timeout=15)
Route("GET",  "/eval-runs/diff",    evals,     principal=None,       timeout=15)
Route("GET",  "/eval-runs/{id}",    evals,     principal=None,       timeout=15)
Route("GET",  "/eval-runs/{id}/events", evals, principal=None,                   stream=True)
```

`ASK` and `LOGIN` are the limits the services enforce today, moved unchanged so the migration
alters where a rule lives without altering the rule: 20 per hour per address for `/ask`
(`answer`'s `ask_rate_limit_per_hour`, which stays settings-driven because it is the one
number an operator may want to turn down under load), and 60 per hour for login
(`auth.policy.Policy.LOGIN_ATTEMPTS_PER_HOUR`, a constant because it is sized against the
lockout and the two must be reasoned about together). The circuit-breaker numbers join the
latter in a gateway `policy.py`, each with the reason it has that value.

Note what does **not** move with them: `answer` keeps `ask_daily_limit` (500) and the
`questions_today` query behind it. The hourly window and the daily cap were deliberately
sized against each other — 20 an hour over 24 hours is 480, just under 500 — so splitting
them across two services means that relationship now spans a boundary and must be stated in
both places rather than assumed.

A path absent from the table is a 404 **from the gateway**; it never becomes an upstream
request. That is what turns two policies from repeated checks into structural facts:

- `/metrics` is not in the table, so it is unroutable rather than protected. It stays
  reachable inside the private network for a scraper.
- `/docs`, `/redoc` and `/openapi.json` are appended to the table only when `ENV` is not
  `production`. The four per-service `ENV` checks stay where they are — they are defence in
  depth, and they are what still protects a service if the private split is unavailable.

`/health` and `/ready` on the gateway are the gateway's own, not proxied. A readiness check
that probes its upstreams would turn one sick service into an unready edge, which is the
amplification the existing services already refuse.

## The trust model

The gateway performs rate limiting, route lookup, and a coarse allow/deny using
`deflect_common.auth.resolve_principal`. It then forwards the **original** `Authorization`
header unchanged, and every upstream service runs `principal_guard` exactly as it does today.

Nothing in `services/*` is deleted, and nothing is weakened. A request that reaches a service
by any other path is still checked. The cost is a second Redis read per session request,
which is acceptable because `resolve_principal` compares the two static tokens first and
short-circuits before touching Redis at all — so machine traffic pays nothing.

**Header hygiene.** The gateway strips any client-supplied `X-Deflect-*` and
`X-Forwarded-*` header before proxying, then sets its own. No inbound header from the public
is ever relayed to an upstream. This is not defending a header the design relies on — the
design deliberately relies on none — it is refusing to create one by accident.

## The client address, and why the existing rule is wrong here

`deflect_common.ratelimit.client_address(trust_forwarded=True)` takes the **leftmost**
`X-Forwarded-For` entry. That is correct today: the only trusted forwarder is the web BFF,
and `apps/web/app/api/ask/route.ts` computes the address itself and **overwrites** the header
rather than relaying the visitor's. With exactly one entry, leftmost is the client.

At the gateway that rule inverts, because the gateway is the edge and sits behind Render's
load balancer, which **appends** the real client IP to whatever the client sent:

```
client sends:  X-Forwarded-For: 9.9.9.9
Render yields: X-Forwarded-For: 9.9.9.9, <real client>
                                 ^^^^^^^ attacker-controlled
```

Taking the leftmost entry would let any caller mint a fresh rate-limit key per request, and
the limiter would be decorative — the same outcome as the uvicorn defect, reached by a
different route. This is the third appearance of this class in the project (uvicorn rewriting
`request.client`; unscoped test queries; now this), which is itself worth recording.

**The rule:**

- `--forwarded-allow-ips ""` stays on all five services, the gateway included, so
  `request.client` is always the true peer and uvicorn never rewrites anything.
- The gateway parses `X-Forwarded-For` itself and takes the **rightmost** entry — the one
  appended by the one proxy it trusts.
- This lands as a new function, `edge_address(request, trusted_hops=1)`, rather than another
  boolean on `client_address`. The two rules are genuinely different, and a flag would invite
  a future caller to pick the wrong one silently.

`trusted_hops` is explicit because the correct entry is *n*-from-the-right, and a deployment
behind two proxies rather than one needs a different number, not different code.

## Rate limiting

`SlidingWindowLimiter` is in-memory and per-process, so its allowance divides by worker
count. That is documented and tolerable while `answer` and `auth` are pinned to one worker;
at the edge it stops being tolerable, because the gateway is the service most likely to need
more than one.

A `RedisSlidingWindowLimiter` joins it in `packages/common`, backed by a sorted set — one
pipelined `ZREMRANGEBYSCORE` + `ZCARD` + `ZADD` + `EXPIRE` per check. The in-memory
implementation stays for tests and for single-worker use; both satisfy the same protocol, the
way `SessionStore` already has a real and a fake implementation.

Limits move as follows:

| limit | today | after |
| --- | --- | --- |
| per-address `/ask` window | `answer` | gateway |
| per-address login window | `auth` | gateway |
| daily `/ask` spend cap | `answer` | **stays in `answer`** |

The daily cap stays because it counts rows in `answer`'s own `traces` table and is a spend
bound, not an abuse bound. The general rule worth writing down: *a rate limit is about volume
arriving from the public, which only the edge sees; an authorisation check is about
correctness, which must hold everywhere.*

## Failure handling

| condition | response | reasoning |
| --- | --- | --- |
| upstream unreachable | 502 | Distinct from an upstream that answered with an error, which is relayed as-is. |
| upstream exceeds route timeout | 504 | A gateway that hangs converts one slow service into an exhausted edge. |
| rate limit exceeded | 429 + `Retry-After` | Same shape the services return today, so no client changes. |
| path not in the table | 404 | Refused before any upstream call. |
| upstream failing repeatedly | 503, fail fast | A circuit breaker: after 5 consecutive failures, stop dialling that upstream for 30 seconds. Without it every gateway worker ends up blocked on the same sick service. The numbers are a starting point and belong in a policy module with their reasoning, the way `auth/policy.py` already does it. |

Correlation ids are minted at the gateway using the existing `RequestIdMiddleware` from
`packages/common` and forwarded, so one id spans the whole hop chain rather than starting
fresh at each service.

## Testing

The route table is data, so the important tests are table-driven and cheap:

- **The full matrix.** Every route × every credential kind, asserting the gateway's decision
  matches the table. This is the artifact that replaces reading four services' decorators.
- **Unroutable paths.** `/metrics` is 404 at the gateway; `/docs` is 404 when
  `ENV=production` and routed otherwise.
- **Streaming is not buffered.** A fake upstream that emits SSE frames slowly must arrive at
  the client incrementally. This is the test most likely to catch a real regression, because
  buffering is the default failure mode of a naive proxy and it is invisible until a user
  waits thirty seconds for a first token.
- **A spoofed leftmost `X-Forwarded-For` does not change the rate-limit key.** The direct
  regression test for the section above.
- **Header hygiene.** A client-supplied `X-Forwarded-For` or `X-Deflect-*` never reaches the
  upstream.
- **Credential passthrough.** The upstream receives the caller's original `Authorization`
  value, not the gateway's service token.

Upstreams are faked with an in-process ASGI app, which keeps these tests fast and honest —
the same technique the existing `test_principals.py` files use. The circuit breaker is tested
against a fake that fails on demand rather than by timing.

Note the limitation, stated rather than hidden: `ASGITransport` calls the app in-process and
does not pass through the middleware stack, so — exactly as with the uvicorn defect — no test
here can prove the deployed proxy behaviour. The `X-Forwarded-For` rule must additionally be
verified over real HTTP against the running stack before this is called done.

## Deployment

`render.yaml` gains `deflect-gateway` as `type: web`. The four existing services should
become private, reachable only inside the Render network.

**This is the one thing to verify first, because it is not fully in our control:** Render
Private Services (`type: pserv`) require a paid instance type. Two outcomes, both specified:

- **Private split available.** The four services become `pserv`, drop their public URLs, and
  the gateway is the genuine and only edge. The `ENV`-unset-leaves-`/docs`-public item is
  closed by construction rather than by remembering a variable.
- **Not available.** The services stay `type: web` with their own URLs. The gateway still
  centralises policy and becomes the documented entry point, but it is a *front door beside
  other doors* rather than the only one. The per-service `ENV` checks and `principal_guard`
  remain the real protection — which they already are, which is why the defence-in-depth
  trust model above is what makes this fallback survivable rather than embarrassing.

The design does not change between the two. Only the honesty of the README paragraph does,
and it must match whichever is true.

`docker-compose.yml` gains the gateway and keeps every service's port published, because
local development benefits from reaching a service directly.

## Out of scope

Deliberately excluded, each because it would add machinery this system has no use for:

- **API keys per consumer, quotas, plans.** There are no third-party consumers.
- **Response caching.** Answers are streamed and personalised; eval runs are already cheap
  reads.
- **Request/response transformation or API versioning.** One client, versioned by deploy.
- **Load balancing across upstream replicas.** Render already does this.
- **Service discovery.** Four upstreams, known by name.
- **mTLS between gateway and services.** The private network plus the service token is the
  boundary; mTLS would be the sort of infrastructure the README refuses.

## README obligations

Two paragraphs must change, and getting them right matters more than the code:

1. **"What it still does not need"** currently names "no service mesh, no Kubernetes, no
   distributed tracing backend". A gateway is adjacent to a mesh, so this paragraph must
   distinguish them honestly: a gateway terminates public traffic at one place, a mesh
   governs traffic between services — and this project still has no use for the second.
2. **A new paragraph stating the motivation plainly.** That the gateway is here for breadth,
   that the four-origin problem is real and is what it was pointed at, and that unlike the
   broker it was not forced by an incident. The README's credibility comes from having said
   "this was not needed" once; it survives only if it also says "this one was chosen, not
   forced."

The `Security` table's principal column becomes the gateway's route table, since that is now
where the answer lives.

## Open questions

1. Does the Render plan support Private Services? First thing to verify; the spec covers both.
2. Does the gateway need more than one worker? If the private split lands and traffic stays at
   demo levels, one worker keeps the in-memory limiter viable and the Redis limiter could be
   deferred. Decide with a measurement, not in advance.
