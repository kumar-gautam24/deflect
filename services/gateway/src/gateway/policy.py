"""Gateway constants in one place, each with the reason it has that value.

The same reasoning as auth/policy.py: a number without its justification is a number
nobody can safely change six months later.
"""


class Policy:
    # The /ask RATE is deliberately NOT here. It lives in Settings, because it is the one
    # number an operator may want to turn down under load and a constant would mean a
    # deploy to do it. The burst below is a design decision rather than an operational
    # knob, so it stays a constant.
    #
    # Five questions back to back is someone trying the demo, not abusing it. The old
    # sliding-window log allowed all twenty at once, so this is strictly smoother.
    ASK_BURST = 5

    # Carried unchanged from auth. A backstop against CPU exhaustion, not the control --
    # account lockout is that, and it is per-account rather than per-address. Sized so an
    # attacker filling this bucket cannot also stop a legitimate admin logging in -- which
    # is why it is not lower.
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
