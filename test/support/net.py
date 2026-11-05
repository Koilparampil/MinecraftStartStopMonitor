"""Ephemeral port allocation. Tests must never hard-code ports."""

import contextlib
import socket


def free_tcp_port(host: str = "127.0.0.1") -> int:
    """Return a TCP port that was free a moment ago on `host`."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def free_tcp_ports(count: int, host: str = "127.0.0.1") -> list[int]:
    """Return `count` distinct free ports (held open together while picking)."""
    sockets = []
    try:
        for _ in range(count):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind((host, 0))
            sockets.append(s)
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()
