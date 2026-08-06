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

    # A backstop against CPU exhaustion, not the control -- account lockout is that, and
    # it is per-account rather than per-address. Sized so that an attacker filling this
    # bucket cannot also stop a legitimate admin logging in.
    LOGIN_ATTEMPTS_PER_HOUR = 60
