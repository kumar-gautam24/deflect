# Authentication and abuse control

Deflect is going onto the public internet as an open demo. Anyone who finds the URL can
ask a question; everything else needs a credential. This spec closes three findings from
the pre-deployment audit:

1. `POST /ingest` accepts an arbitrary filesystem path from an unauthenticated caller,
   and the content it reads becomes queryable through `/search`.
2. No endpoint on any of the three services authenticates anything. CORS on `/ask`
   constrains browsers only; `curl` ignores it.
3. `POST /runs` triggers a full LLM-judged eval run — the most expensive operation in
   the system — with no credential.

## Principals

Three, and no more. Every route maps to exactly one.

- **public** — anonymous internet traffic
- **service** — one Deflect service calling another
- **operator** — the maintainer, and CI

## Policy

| service | route | principal |
| --- | --- | --- |
| retrieval | `GET /health` | public |
| retrieval | `GET /documents` | service |
| retrieval | `POST /search` | service |
| retrieval | `POST /ingest` | operator, plus path confinement |
| answer | `GET /health` | public |
| answer | `POST /ask` | public, rate limited |
| answer | `POST /answer` | service |
| answer | `GET /traces`, `GET /traces/{id}` | operator |
| evals | `GET /health` | public |
| evals | `POST /runs` | operator |
| evals | `GET /eval-runs`, `/eval-runs/diff`, `/eval-runs/{id}` | public |

This table is the security policy in full. It is also the review checklist: a route
added later without a row here has not been considered.

`retrieval` ends with no public route but `/health`. The service holding the corpus is
unreachable by anonymous callers, which makes Render's private networking a bonus rather
than a load-bearing control.

`/health` stays public on all three because Render polls it without credentials. The
retrieval one reports `{"status": "ok", "database": "connected"}`. That confirms a
database is reachable, which a `200` already implies; hiding it would mean failing
health checks to conceal nothing.

`GET /documents` is marked `service` rather than `operator` because its caller is the
golden-dataset validation test, which runs in CI. CI holds both tokens, so either would
work; `service` is correct because the caller is automation, not a person.

## Why static tokens rather than an API-key table

A hashed API-key table with scopes would buy per-caller revocation and an audit trail.
It would also need either a table duplicated into all three databases or a shared one,
and "no shared tables, no cross-service joins" is the invariant the whole split exists
to demonstrate. For a deployment with one operator, that trades the project's strongest
architectural claim for machinery nobody will use.

Two static bearer tokens carried in the environment close all three findings. Rotation
is a redeploy — the same coupling `packages/common` already imposes, and which the
README already names as its cost.

## Components

### `packages/common/src/deflect_common/auth.py` (new)

```python
def bearer_guard(expected: str, principal: str) -> Callable[..., None]:
    """Build a FastAPI dependency requiring `expected` as a bearer token."""
```

Credentials arrive as an argument, never from a settings singleton. This follows the
rule `llm/base.py` already states: a library shared by three services cannot reach into
one service's configuration. Each service builds its guards at startup from its own
settings.

`bearer_guard` raises on an empty `expected` at construction time, so a service with an
unset token fails to start rather than serving a route that accepts anything. Comparison
uses `hmac.compare_digest`. The `principal` argument names the guard in log output —
a `401` should say which credential was expected.

Auth lives in `packages/common` because all three services enforce it and three drifting
copies of one rule is precisely the failure the package exists to prevent.

### `services/answer/src/answer/ratelimit.py` (new)

Not in `packages/common`. Three services need auth; one needs rate limiting.

```python
class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None: ...
    def check(self, key: str, now: float) -> bool: ...
```

`now` is a parameter rather than a call to the clock, so window expiry is tested without
sleeping.

The global daily cap is a function, not a class: one `count(*)` over `traces` where
`created_at >= utc_midnight`. The answer service already writes a trace row per question,
so the day's spend counter already exists. No new table, no migration, and it survives
restarts.

### Configuration

| service | setting | default |
| --- | --- | --- |
| all three | `service_token` | `""` (must be set; empty fails startup) |
| all three | `operator_token` | `""` (must be set; empty fails startup) |
| retrieval | `corpus_root` | `/corpus` |
| answer | `ask_rate_limit_per_hour` | `20` |
| answer | `ask_daily_limit` | `500` |

`retrieval` gains a `lifespan` it does not currently have, to hold the startup guard.

Every service needs both tokens, though for different reasons, which is why the startup
guard requires both everywhere: retrieval validates both on inbound requests; answer
validates both inbound and presents `service_token` outbound to retrieval; evals validates
`operator_token` inbound and presents `service_token` outbound to answer.

### Clients

`RetrievalClient(base_url, token)` and `AnswerClient(base_url, token)` take the token
beside the URL and send `Authorization: Bearer`. Both already translate an unreachable
dependency into a `503`; a `401` from upstream is a misconfiguration, not an outage, and
propagates as a `500` rather than being disguised as a transient failure.

### Web application

- Basic-auth middleware on `/traces`. The browser renders the prompt natively, so there
  is no login page, no session store, and no cookie handling. A sign-in flow for a single
  operator is machinery maintained forever to avoid one browser prompt.
- `getFromAnswer` sends the operator token. `getFromEvals` sends nothing — the eval
  dashboard is public.
- The `/api/ask` proxy sends `SERVICE_TOKEN` and forwards the client IP.

The traces UI itself does not change. Only access to it does.

## Client IP and what the limiter actually protects

Behind Vercel, every `/ask` reaches the answer service from a Vercel IP. Per-IP limiting
would throttle all users as one, so the BFF forwards the real client address.

If the answer service simply trusted `X-Forwarded-For`, a direct caller would spoof a
fresh address per request and per-IP limiting would evaporate. So: **the BFF presents
`SERVICE_TOKEN` on `/ask`, and a forwarded IP is trusted only from a caller that
authenticated.** Anonymous callers reaching the answer service directly are limited on
their real socket address.

`/ask` therefore accepts anonymous callers but treats an authenticated one as more
trustworthy about who it is speaking for. It is not an authenticated endpoint.

**The per-IP limit is not a spend bound.** It stops one script; a botnet has many real
addresses. **The global daily cap is the only control that bounds the Gemini bill**, and
it cannot be bypassed because it counts rows already written. The two layers do different
jobs, and conflating them would leave the deployment believing it is protected when only
half of it is.

## Errors

| condition | status | detail |
| --- | --- | --- |
| missing or wrong credential | `401` | `WWW-Authenticate` set; missing and wrong are not distinguished |
| per-IP or daily limit exceeded | `429` | `Retry-After` set |
| ingest path outside `CORPUS_ROOT` | `400` | the rejected path is never echoed back |

Missing and wrong credentials return the same response because telling an attacker which
one they got wrong is free information.

The ingest error omits the offending path so the endpoint cannot be used to map the
container filesystem by probing.

A guard rejects before the handler runs: no database query, no model call.

## Failure modes

The daily cap **fails closed**: it is a database query, and if the database is unreachable
the ask path is already broken, so refusing is honest.

The per-IP window **fails open** and is in-memory, so it resets on restart — on Render's
free tier, a redeploy grants everyone a fresh allowance. This is documented rather than
solved. Adding Redis so a throttle survives a restart is the infrastructure-for-its-own-sake
the README argues against, and the control that actually bounds cost does survive.

Ingest paths are checked after `.resolve()`, so a symlink pointing out of the corpus root
is caught, not only `../`.

## Testing

Every case is a unit test needing no new infrastructure.

- `bearer_guard`: accepts the correct token; rejects a wrong one, a missing header, and a
  malformed one; refuses to construct on an empty expected token.
- `SlidingWindowLimiter`: allows up to the limit, rejects beyond it, allows again once the
  window passes — driven by the injected `now`, so it never sleeps.
- Daily cap: traces seeded at known timestamps, asserting the boundary at UTC midnight.
- Path confinement: `../../etc`, an absolute path outside the root, and a symlink escape
  are rejected; a legitimate subdirectory is accepted.
- Each service's existing suite gains one case per protected route asserting `401` without
  a credential. This is what makes removing a guard fail the build.

**Known gap.** These prove each guard works in isolation. They do not prove every route
*has* one — a route added later is not covered automatically. The mitigation is the policy
table above serving as the review checklist. Naming this is better than implying the suite
catches it.

## Out of scope

Token rotation without a redeploy; per-caller revocation; an audit log; Cloudflare
Turnstile. Each is real work and a separate project.
