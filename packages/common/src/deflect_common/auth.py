"""Bearer-token guards, shared by every service.

Credentials arrive as arguments rather than from a settings singleton. This package is
imported by three services, and a library that reaches into one service's configuration
cannot be used by the other two -- the same rule llm/base.py states for provider keys.

The guard lives here rather than in each service because three copies of one
authorisation rule is exactly the drift this package exists to prevent.
"""

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from deflect_common.sessions import SessionStore

# Which credential kinds satisfy which requirement. A service token deliberately does not
# satisfy "operator": machines and operators are different principals, and collapsing them
# would let any service trigger spend.
_SATISFIES: dict[str, set[str]] = {
    "service": {"service"},
    "operator": {"operator", "session:admin"},
    "viewer": {"operator", "session:admin", "session:viewer"},
}


def token_matches(expected: str, authorization: str | None) -> bool:
    """Whether an Authorization header carries exactly `expected` as a bearer token."""
    if not authorization:
        return False

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False

    # compare_digest rather than == so a wrong token takes the same time to reject
    # regardless of how many leading characters were right.
    return hmac.compare_digest(token, expected)


def bearer_guard(expected: str, principal: str) -> Callable[..., None]:
    """Build a FastAPI dependency requiring `expected` as a bearer token.

    Raises on an empty `expected` at construction rather than at request time. Services
    build their guards at import, so an unset token aborts the import of main.py and the
    process exits before binding a port -- the same refuse-to-boot behaviour a missing
    provider key already has, and the reason a misconfigured deploy never takes traffic.

    `principal` names the expected credential in the 401 body, so a failing caller learns
    which token it should have sent without learning anything about the one it did.
    """
    if not expected:
        raise ValueError(f"the {principal} token is empty; refusing to build an open guard")

    def guard(authorization: Annotated[str | None, Header()] = None) -> None:
        if not token_matches(expected, authorization):
            raise HTTPException(
                status_code=401,
                detail=f"a {principal} credential is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return guard


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
