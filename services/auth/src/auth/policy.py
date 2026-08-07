"""Auth constants in one place, each with the reason it has that value.

Constants scattered across modules are how the reasoning gets lost -- six months later
nobody remembers whether 300 was chosen or defaulted.
"""


class Policy:
    # Long enough for a working day, short enough that a forgotten session on a shared
    # machine is not a standing invitation.
    SESSION_HOURS = 12

    # There is deliberately no separate cache TTL. The session's cache entry is written
    # with the session's own remaining lifetime, because any shorter value silently
    # becomes the real session length: services read the cache and never the auth
    # database, so an entry that expires early logs the person out everywhere while their
    # cookie and their row both still say twelve hours. Revocation is by explicit delete
    # on logout, not by expiry.
    #
    # Five wrong passwords is a person misremembering; more is someone guessing.
    LOCK_AFTER_FAILURES = 5
    LOCK_SECONDS = 15 * 60

    # A CPU/spend backstop for the direct door, not the precise control -- the gateway's
    # own per-visitor login bucket (60/hour, burst 10) is that. This service stayed
    # type: web on Render, because the private split needs a paid plan that could not be
    # confirmed, so /auth/login is reachable directly and every attempt still runs a full
    # argon2id hash regardless of who dialled it. Keyed on the true peer, which cannot
    # distinguish the gateway's own traffic from anyone else's -- the gateway forwards
    # whatever credential the caller sent rather than proving its own identity, so every
    # request the gateway relays looks identical to this check and must never trip it.
    # Set an order of magnitude above the gateway's per-visitor limit rather than trying
    # to match it.
    LOGIN_BACKSTOP_PER_HOUR = 600
