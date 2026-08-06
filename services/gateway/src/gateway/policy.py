"""Gateway constants in one place, each with the reason it has that value.

The same reasoning as auth/policy.py: a number without its justification is a number
nobody can safely change six months later.
"""


class Policy:
    # Carried unchanged from the answer service, so moving the limit does not also
    # change how much traffic an hour permits. Burst is new -- see below.
    ASK_PER_HOUR = 20
    # Five questions back to back is someone trying the demo, not abusing it. The old
    # sliding-window log allowed all twenty at once, so this is strictly smoother.
    ASK_BURST = 5

    # Carried unchanged from auth. Sized so an attacker filling this bucket cannot also
    # stop a legitimate admin logging in -- which is why it is not lower.
    LOGIN_PER_HOUR = 60
    LOGIN_BURST = 10

    WINDOW_SECONDS = 3600

    # Connect and write are short because a healthy upstream on the same private network
    # answers in milliseconds; a slow one is a failure, not a slow success. The read
    # timeout is per-route, since /ask legitimately takes far longer than /traces.
    CONNECT_TIMEOUT = 5.0
    WRITE_TIMEOUT = 5.0

    # Five consecutive failures is a pattern rather than a blip. Thirty seconds is long
    # enough for a restart to finish and short enough that recovery is not noticed as an
    # outage of its own. Starting points, chosen rather than defaulted.
    BREAKER_FAILURES = 5
    BREAKER_COOLDOWN_SECONDS = 30
