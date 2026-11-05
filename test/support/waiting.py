"""Bounded polling for asynchronous test results."""

import time


def wait_until(predicate, timeout=5, message="condition not met", interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)

    assert predicate(), message
