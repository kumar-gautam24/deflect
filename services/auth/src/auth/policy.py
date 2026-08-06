"""Auth constants in one place, each with the reason it has that value.

Constants scattered across modules are how the reasoning gets lost -- six months later
nobody remembers whether 300 was chosen or defaulted.
"""


class Policy:
    # Long enough for a working day, short enough that a forgotten session on a shared
    # machine is not a standing invitation.
    SESSION_HOURS = 12

    # The ceiling on how long a revoked session survives a missed cache delete. Not a
    # performance number: this is the security bound, and it is why it is minutes rather
    # than hours.
    CACHE_TTL_SECONDS = 300

    # Five wrong passwords is a person misremembering; more is someone guessing.
    LOCK_AFTER_FAILURES = 5
    LOCK_SECONDS = 15 * 60

    # A backstop against CPU exhaustion, not the control -- account lockout is that, and
    # it is per-account rather than per-address. Sized so that an attacker filling this
    # bucket cannot also stop a legitimate admin logging in.
    LOGIN_ATTEMPTS_PER_HOUR = 60
