"""End-to-end: an empty server stops by itself after the startup guard plus
`autoStop.stopDelay`, observed through the committed states on /stream."""

import time

import pytest

from src.lifecycle.auto_stop import AutoStopConfig
from test.support import wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

STOP_DELAY = 3
MONITOR_OPTIONS = {
    "dummy_env": {"DUMMY_PLAYERS": ""},  # nobody online
    "auto_stop_delay": STOP_DELAY,
    "polling_rate": 1,
}


def test_empty_server_stops_automatically(api, monitor_app):
    guard = AutoStopConfig(True, STOP_DELAY).startup_guard
    deadline = time.monotonic() + guard + STOP_DELAY + 60

    with api.stream() as snapshots:
        first = next(snapshots)
        assert first["state"] == "STOPPED", first
        assert api.post("/action", action="startServer")["success"] is True

        running_at = armed = stopping_at = None
        saw_healthy_empty = False
        for snapshot in snapshots:
            assert time.monotonic() < deadline, "auto-stop did not happen in time"
            assert monitor_app.process.poll() is None, monitor_app.output_tail()
            state = snapshot["state"]
            if state == "RUNNING" and running_at is None:
                running_at = time.monotonic()
            if (
                state == "RUNNING"
                and snapshot["ready"]
                and snapshot["playerData"] == "ok"
            ):
                assert snapshot["playerCount"] == 0, snapshot
                saw_healthy_empty = True
                if snapshot["autoStopAt"] is not None and armed is None:
                    armed = (snapshot, time.time())
            if state == "STOPPING" and running_at is not None:
                stopping_at = time.monotonic()
                assert saw_healthy_empty, "stopped without a healthy empty roster"
                assert not snapshot.get("restartPending")
            if state == "STOPPED" and stopping_at is not None:
                break

    assert armed is not None, "the countdown (autoStopAt) was never published"
    armed_snapshot, received_at = armed
    # autoStopAt is wall-clock: about STOP_DELAY ahead when first published.
    assert -1 <= armed_snapshot["autoStopAt"] - received_at <= STOP_DELAY + 1
    # Never before the startup guard + the empty delay (1 s scheduling slack).
    assert stopping_at - running_at >= guard + STOP_DELAY - 1
    wait_until(
        lambda: not monitor_app.dummy_processes(),
        timeout=15,
        message="dummy still running",
    )
