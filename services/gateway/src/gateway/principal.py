"""Whether a caller may reach a route, decided at the edge.

This deliberately duplicates the verdict the upstream service will reach on its own. The
duplication is the design: the gateway refuses obvious traffic early, and the service
still refuses it if anything reaches the service another way. Neither depends on the
other being correct.

It reuses resolve_principal and satisfies rather than reimplementing them, so the two
layers cannot drift into disagreeing about what an admin is.
"""

from deflect_common.auth import resolve_principal, satisfies
from deflect_common.sessions import SessionStore

from gateway.routes import Route


async def allowed(
    route: Route,
    authorization: str | None,
    sessions: SessionStore,
    service_token: str,
    operator_token: str,
) -> bool:
    """Whether this credential satisfies this route's requirement."""
    if route.principal is None:
        return True

    principal = await resolve_principal(
        authorization, service_token, operator_token, sessions
    )
    if principal is None:
        return False

    if route.principal == "session":
        # Not expressible through satisfies, which is about privilege level. This is about
        # kind: /auth/logout revokes a session row and a token has none, so admitting a
        # token would forward a request that can only fail upstream.
        return principal.kind == "session"

    return satisfies(principal, route.principal)
