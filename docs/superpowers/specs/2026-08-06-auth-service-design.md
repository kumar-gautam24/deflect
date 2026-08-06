# Admin auth service

Sub-project C of Deflect's next phase. A (Groq and production hardening) and B (event-driven
jobs) are merged. D (API gateway) follows and will move edge concerns to a single front door;
E (Kubernetes) is optional.

## Why this exists

Deflect authenticates machines. `SERVICE_TOKEN` and `OPERATOR_TOKEN` are shared secrets that
answer one question — *is the caller allowed* — and cannot answer the one that matters once
more than one person is involved: **who did this?**

A shared operator token cannot attribute an ingest, an eval run, or a look at the traces
surface to a person. Accounts can. That is the capability this buys, and it is worth being
honest that it is the justification: for a single operator, the token already works.

The second thing it buys is a line between reading and spending. Reading a trace costs
nothing; starting an eval run consumes two hours of provider quota. A shared token cannot
express that difference. Two roles can.

## Decisions

| decision | choice | why not the alternative |
| --- | --- | --- |
| where sessions are validated | services read a Redis cache the auth service writes | Calling auth per request puts a network hop in front of every protected route and makes auth a hard dependency for services whose own databases are fine. |
| token format | opaque, SHA-256 at rest | A JWT cannot be revoked before it expires, which is the property this model exists to keep. |
| scope | password and sessions only | TOTP, OAuth, password reset and recovery codes are each real work, and none changes what the system can do. |
| roles | `admin` and `viewer` | Flat roles would make this sub-project buy attribution alone. Named permissions are a lot of machinery for four protected operations. |
| bootstrap | a CLI command | Environment seeding puts a real password in Render's dashboard; a migration puts a default credential in a public repository. |

**`SERVICE_TOKEN` and `OPERATOR_TOKEN` both survive.** CI and the workers have no browser to
log in with. Sessions are for humans, tokens for automation — the split the policy table
already draws.

## Architecture

A fourth service, `auth`, with its own database owning `admin_users` and `sessions`. Nothing
else reads those tables.

**Postgres is the record; Redis is the working copy.** On login the auth service writes the
session row and sets `session:<sha256>` in Redis holding the user id, the role and the
expiry. Other services read only Redis, so they never query the auth database and keep
serving while auth is down — you simply cannot log in until it returns.

```
browser ──login──> web ──> auth ──writes──> Postgres (record)
                                 └─writes──> Redis   (working copy)

browser ──cookie──> web ──Bearer session──> answer / retrieval / evals
                                                └─reads──> Redis
```

### Three credential kinds, one header

`Authorization: Bearer <value>` carries any of them, and the guard resolves which:

| credential | who holds it |
| --- | --- |
| `SERVICE_TOKEN` | the other services and the web BFF |
| `OPERATOR_TOKEN` | CI and the workers |
| a session token | a logged-in human |

Routes declare a minimum principal, and the guard accepts more than one kind where that is
honest:

| principal | satisfied by |
| --- | --- |
| `service` | `SERVICE_TOKEN` only — never a human |
| `operator` | `OPERATOR_TOKEN`, or a session whose role is `admin` |
| `viewer` | `OPERATOR_TOKEN`, or any valid session |

`/ingest`, `POST /runs` and `/jobs/*` become `operator`. `/traces` becomes `viewer`. `/ask`
and the eval dashboard stay public. **Nothing CI does changes**, because `OPERATOR_TOKEN`
still satisfies everything it satisfied before.

That a session token is *rejected* where `service` is required is the check that keeps a
logged-in human out of machine-to-machine routes.

## Sessions

A token is 32 bytes from `secrets.token_urlsafe`, and only its SHA-256 is persisted. A dump
of the auth database yields no usable session.

### Revocation is bounded, not instant

Logging out deletes the Redis key and stamps `revoked_at`. If the delete succeeds, revocation
is immediate. If it does not — a Redis blip, a partition — the session stays usable until its
cache entry expires.

That is why the cache TTL is **300 seconds** rather than the session's full lifetime: it is
the ceiling on how long a revoked session can survive a missed delete. A flushed Redis logs
everyone out, which is the safe direction for a cache.

### No idle timeout, deliberately

own-vibes tracks `last_seen_at` with a slide interval so a busy admin does not cause a write
per request. Deflect gets an absolute twelve-hour lifetime and nothing else. Sliding expiry
exists to keep a session alive across a working day without re-authenticating; with one or
two operators and a twelve-hour window, it buys nothing.

### Login resists enumeration and guessing

An unknown email still runs a password hash, so a wrong address and a wrong password take
comparable time and return an identical body. Five consecutive failures lock the account for
fifteen minutes, and the lock is a column rather than in-memory state, so it survives a
restart — the same reasoning that put the rate limiter's daily cap in Postgres.

The login route is rate-limited. An unauthenticated endpoint that performs an argon2id hash
is a denial-of-service target otherwise.

**`SlidingWindowLimiter` moves from `services/answer` to `packages/common`.** It was one
service's concern when only `/ask` needed it; login makes it two, and the rule this project
already follows is that what two services need lives in the shared package. The answer
service imports it from there instead, and `client_address` moves with it, since both callers
need the same "which address do I trust" answer. Nothing about the behaviour changes, and the
existing tests move with the code.

### Policy

Every constant lives in one `Policy` class with its rationale attached, as own-vibes does:
session lifetime (12 hours), cache TTL (300 seconds), lockout threshold (5) and window (15
minutes). Constants scattered across modules are how the reasoning gets lost.

## Schema

Auth database only:

- `admin_users`: `id`, `email`, `password_hash` (argon2id), `role`, `failed_login_count`,
  `locked_until`, `created_at`, `updated_at`. A unique index on `lower(email)`, so
  `You@x.com` and `you@x.com` cannot both exist.
- `sessions`: `id`, `token_hash` (unique), `user_id`, `role`, `issued_at`, `expires_at`,
  `revoked_at`, `ip`, `user_agent`.

`role` is denormalised onto the session so a validating service needs one Redis read and no
join. Changing a user's role does not retroactively change live sessions; it takes effect at
their next login, which is stated here so the behaviour is a decision rather than a surprise.

## API

| route | principal | behaviour |
| --- | --- | --- |
| `POST /auth/login` | public, rate limited | `{email, password}` → `{token, role, expires_at}` |
| `POST /auth/logout` | valid session | revokes this session |
| `POST /auth/logout-all` | valid session | revokes every session for this user |
| `GET /auth/me` | valid session | `{email, role, expires_at}` |
| `GET /health`, `GET /ready` | public | as the other services |
| `GET /metrics` | service | as the other services |

## The web application

`apps/web/proxy.ts` and `apps/web/lib/basic-auth.ts` are **deleted**. The browser's native
credential prompt is replaced by a `/login` page with an email and password form.

Two route handlers: `POST /api/auth/login` calls the auth service and sets an `httpOnly`,
`secure`, `SameSite=Lax` cookie from the returned token; `POST /api/auth/logout` calls
through and clears it. The cookie is set by the web app rather than the auth service because
it belongs to the browser's origin, not the API's.

Server components read that cookie and forward it as a bearer token. `getFromAnswer` stops
sending `OPERATOR_TOKEN` and sends the caller's session, so the traces page shows data
because *that person* is allowed to see it, not because the server holds a master key.

## Shared code

`packages/common/src/deflect_common/auth.py` grows one function, and it is the whole
integration surface:

```python
@dataclass(frozen=True)
class Principal:
    """Who is calling, and enough to decide what they may do.

    `kind` is "service", "operator" or "session". `role` is set only for a session, and
    `user_id` only for a session -- a token has no person behind it, which is the whole
    reason this sub-project exists.
    """

    kind: str
    role: str | None = None
    user_id: str | None = None


async def resolve_principal(
    authorization: str | None,
    service_token: str,
    operator_token: str,
    sessions: SessionStore,
) -> Principal | None:
    """Which credential kind was presented, and for a session, its role.

    None means no valid credential. It is async because a session requires a Redis read;
    the two token comparisons short-circuit before any I/O, so a machine call never pays
    for the lookup.
    """
```

`SessionStore` is the protocol sessions arrive through:

```python
class SessionStore(Protocol):
    async def get(self, token_hash: str) -> tuple[str, str] | None:
        """The (user_id, role) behind a hashed token, or None if it is unknown or expired."""

    async def put(self, token_hash: str, user_id: str, role: str, ttl_seconds: int) -> None: ...

    async def delete(self, token_hash: str) -> None: ...
```

`bearer_guard` becomes one caller of it. Sessions arrive through a small `SessionStore`
protocol with a Redis implementation and an in-memory fake — the same shape as `JobQueue`,
so no test needs Redis.

## Errors

| condition | status |
| --- | --- |
| wrong password, or unknown email | `401`, identical body for both |
| account locked | `423`, with the remaining seconds |
| expired or revoked session | `401` |
| session presented where `service` is required | `401` |
| login attempts exceeding the window | `429` |

## Testing

Everything runs with no Redis, no network and no provider key.

- Password hashing round-trips; a wrong password and an unknown email both perform a hash,
  so neither is distinguishable by the work done.
- Lockout triggers on the fifth failure and releases after the window, driven by an injected
  clock rather than sleeping.
- A valid session resolves to its role; an expired one does not; a revoked one does not.
- `resolve_principal` returns `service` for the service token, `operator` for the operator
  token, and the session's role for a session — and **rejects a session where `service` is
  required**.
- One route test per service confirming the widened principals still reject anonymous
  callers, and that `OPERATOR_TOKEN` still satisfies every route it satisfied before.
- The CLI creates a user, refuses a duplicate email, and refuses an unknown role.

## Out of scope

TOTP, OAuth, password reset, recovery codes, audit logging, session listing, and per-user
rate limits on `/ask`. The last of these becomes possible once identity exists, but `/ask` is
anonymous by design and giving it per-user quotas would mean requiring login to ask a
question — a different product.
