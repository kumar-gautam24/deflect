import os
import subprocess
from pathlib import Path

_GATEWAY_ROOT = Path(__file__).resolve().parent.parent


def test_an_unset_operator_token_aborts_startup():
    """Mirrors the service token's own fail-closed startup check, and closes the gap
    where main.py's own comment claimed every token was checked at import while the
    operator token was not: unset, the gateway used to boot and every operator route
    would silently 401 forever rather than the process refusing to bind a port.

    Run in a subprocess, deliberately, rather than reimporting gateway.main in this test
    session: other test files hold direct references to that module and its objects
    (test_limits.py monkeypatches gateway_main._settings, for instance), and a reload
    that raises partway through re-executing the module body would leave those objects
    in a broken, half-updated state for whatever test runs after this one.
    """
    env = {**os.environ, "SERVICE_TOKEN": "s", "OPERATOR_TOKEN": ""}
    result = subprocess.run(
        ["uv", "run", "python", "-c", "import gateway.main"],
        cwd=_GATEWAY_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "operator" in result.stderr
