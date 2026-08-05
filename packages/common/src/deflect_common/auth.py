"""Bearer-token guards, shared by every service.

Credentials arrive as arguments rather than from a settings singleton. This package is
imported by three services, and a library that reaches into one service's configuration
cannot be used by the other two -- the same rule llm/base.py states for provider keys.

The guard lives here rather than in each service because three copies of one
authorisation rule is exactly the drift this package exists to prevent.
"""

import hmac
from collections.abc import Callable
from typing import Annotated

from fastapi import Header, HTTPException


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
