"""End-to-end: a join attempt on the game port wakes the sleeping server.

No browser: speaks the Minecraft protocol directly (handshake + status
request, then handshake + Login Start) with an independent mini client.
"""

import json

import pytest

from test.support import wait_until

from .helpers import (
    is_online,
    is_stopped,
    join_attempt,
    port_accepts,
    status_ping,
    strip_codes,
)

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

MONITOR_OPTIONS = {"auto_stop_delay": 600}


def reason_text(raw: str) -> str:
    try:
        component = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(component, dict):
        parts = [component.get("text", "")]
        parts += [
            extra.get("text", "") if isinstance(extra, dict) else str(extra)
            for extra in component.get("extra", [])
        ]
        return strip_codes("".join(parts))
    return strip_codes(str(component))


def test_join_attempt_wakes_the_server(api, monitor_app):
    app = monitor_app
    api.wait_for(is_stopped, timeout=20, message="monitor did not settle on STOPPED")
    wait_until(
        lambda: port_accepts(app.game_port),
        timeout=20,
        message="sleeping listener not bound",
    )

    # The server list shows the sleeping MOTD and nobody online.
    status = status_ping(app.game_port)
    description = status["description"]
    motd = (
        description.get("text", "")
        if isinstance(description, dict)
        else str(description)
    )
    assert "sleeping" in strip_codes(motd).lower(), status
    assert status["players"]["online"] == 0
    assert not app.dummy_processes()

    # A join attempt is told to come back, and wakes the real server.
    reason = reason_text(join_attempt(app.game_port, "WakeUpTester"))
    assert "starting" in reason.lower(), reason

    snapshot = api.wait_for(is_online, timeout=60, message="server did not come online")
    assert snapshot["playerData"] in ("ok", "unknown")
    assert app.dummy_processes(), "no dummy server process in the server dir"

    # Clean stop through the API; the listener takes the port back afterwards.
    assert api.post("/action", action="stopServer")["success"] is True
    api.wait_for(is_stopped, timeout=60, message="server did not stop")
    wait_until(
        lambda: not app.dummy_processes(), timeout=15, message="dummy still running"
    )
    wait_until(
        lambda: port_accepts(app.game_port), timeout=20, message="listener not re-armed"
    )
