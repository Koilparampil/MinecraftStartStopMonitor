"""Server List Ping client (the multiplayer-screen status request).

Modern ping (1.7+): handshake (next_state 1), Status Request, JSON Status
Response, then Ping/Pong to measure latency. Legacy ping (0xFE 0x01, answered
by every server version with a 0xFF kick packet) is also supported.
"""

import contextlib
import json
import random
import re
import socket
import time
from dataclasses import dataclass, field, replace
from typing import Any

from src.minecraft.protocol import (
    PacketReader,
    ProtocolError,
    SocketReader,
    decode_long,
    encode_long,
    encode_string,
    encode_ushort,
    encode_varint,
    frame_packet,
    plain_text,
    send_all,
    strip_formatting,
)

# "If the client is pinging to determine what version to use, by convention
# -1 should be set" (protocol docs). Servers answer status for any version.
STATUS_PROTOCOL = -1
# Forge 1.13-1.17 servers put whole mod lists in the status JSON, far above
# the 32767-character limit vanilla clients enforce: accept large frames.
MAX_STATUS_FRAME = 2 * 1024 * 1024
# Player counts outside this range are treated as unknown, never clamped: a
# bogus value must not turn into a low count that arms auto-stop.
MAX_PLAYER_COUNT = 1_000_000
_INT_TEXT = re.compile(r"[+-]?[0-9]{1,10}")  # ASCII only (`\d` matches other scripts)


class PingError(Exception):
    """The status ping failed or the response was not usable."""


@dataclass(frozen=True)
class ServerStatus:
    version_name: str
    protocol: int | None
    online: int
    max_players: int | None  # None when missing or not a sane count
    sample: tuple[
        tuple[str, str], ...
    ] = ()  # (name, id) as reported, anonymous ones included
    motd: str = ""  # plain text, formatting codes stripped
    favicon: bool = False
    latency_ms: float | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class LegacyStatus:
    protocol: int | None  # None for pre-1.4 servers
    version_name: str
    motd: str
    online: int
    max_players: int


def ping_status(
    host: str,
    port: int,
    timeout: float = 5.0,
    protocol: int = STATUS_PROTOCOL,
    *,
    measure_latency: bool = True,
) -> ServerStatus:
    """Status-ping host:port. `timeout` bounds the whole exchange.

    Latency comes from Ping/Pong when the server answers it, else from the
    status round trip. Raises PingError.
    """
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            reader = SocketReader(sock, deadline)
            send_all(
                sock, _handshake(host, port, protocol) + frame_packet(0x00), deadline
            )
            started = time.perf_counter()
            packet_id, payload = reader.read_packet(MAX_STATUS_FRAME)
            status_ms = (time.perf_counter() - started) * 1000
            if packet_id != 0x00:
                raise PingError(
                    f"unexpected packet 0x{packet_id:02x} instead of a status response"
                )
            status = parse_status(PacketReader(payload).read_string(max_length=None))
            latency = _pong_latency(sock, reader, deadline) if measure_latency else None
            return replace(status, latency_ms=status_ms if latency is None else latency)
    except (OSError, ValueError) as exc:  # ProtocolError is a ValueError
        raise PingError(f"status ping to {host}:{port} failed: {exc}") from exc


def _handshake(host: str, port: int, protocol: int) -> bytes:
    payload = (
        encode_varint(protocol)
        + encode_string(host, 255)
        + encode_ushort(port)
        + encode_varint(1)
    )
    return frame_packet(0x00, payload)


def _pong_latency(
    sock: socket.socket, reader: SocketReader, deadline: float
) -> float | None:
    token = random.getrandbits(63)
    started = time.perf_counter()
    try:
        send_all(sock, frame_packet(0x01, encode_long(token)), deadline)
        packet_id, payload = reader.read_packet(64)
        echoed, _ = decode_long(payload)
    except (OSError, ProtocolError):
        return None
    if packet_id != 0x01 or echoed != token:
        return None
    return (time.perf_counter() - started) * 1000


def parse_status(raw: Any) -> ServerStatus:
    """Validate a status response (JSON text or decoded dict) tolerantly.

    Only ``players.online`` is required, as an integer in
    0..MAX_PLAYER_COUNT (JSON number or ASCII digits): without a sane count the
    response cannot be trusted for auto-stop, so it raises PingError.
    """
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise PingError("status response is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise PingError("status response is not a JSON object")
    players = raw.get("players")
    online = _as_count(players.get("online")) if isinstance(players, dict) else None
    if online is None:
        raise PingError("status response has no valid player count")
    version = raw.get("version") if isinstance(raw.get("version"), dict) else {}
    name = version.get("name")
    try:
        motd = plain_text(raw.get("description", ""))
    except RecursionError:
        motd = ""
    return ServerStatus(
        version_name=strip_formatting(name) if isinstance(name, str) else "",
        protocol=_as_int(version.get("protocol")),
        online=online,
        max_players=_as_count(players.get("max")),
        sample=_parse_sample(players.get("sample")),
        motd=motd,
        favicon=isinstance(raw.get("favicon"), str) and bool(raw.get("favicon")),
        raw=raw,
    )


def _as_int(
    value: Any, minimum: int = -(2**31), maximum: int = 2**31 - 1
) -> int | None:
    """A JSON integer (not bool/float) or a string of at most 10 ASCII digits,
    within [minimum, maximum]; None (unknown) for anything else."""
    if isinstance(value, str):
        text = value.strip()
        value = int(text) if _INT_TEXT.fullmatch(text) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _as_count(value: Any) -> int | None:
    return _as_int(value, 0, MAX_PLAYER_COUNT)


def _parse_sample(sample: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(sample, list):
        return ()
    entries = []
    for entry in sample:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            player_id = entry.get("id")
            entries.append(
                (entry["name"], player_id if isinstance(player_id, str) else "")
            )
    return tuple(entries)


# ----------------------------------------------------------------------
# Legacy ping
# ----------------------------------------------------------------------


def parse_legacy_response(text: str) -> LegacyStatus:
    """Parse the text of a 0xFF kick packet answering a legacy ping."""
    if text.startswith("§1\0"):
        fields = text.split("\0")
        if len(fields) < 6:
            raise PingError("legacy ping response has too few fields")
        _, protocol, version, motd, online, maximum = fields[:6]
    else:  # pre-1.4: "motd§online§max"
        parts = text.rsplit("§", 2)
        if len(parts) != 3:
            raise PingError("unrecognised legacy ping response")
        motd, online, maximum = parts
        protocol, version = "", ""
    online_count, max_count = _as_count(online), _as_count(maximum)
    if online_count is None or max_count is None:
        raise PingError("legacy ping response has invalid player counts")
    return LegacyStatus(
        protocol=_as_int(protocol),
        version_name=strip_formatting(version),
        motd=strip_formatting(motd),
        online=online_count,
        max_players=max_count,
    )


def ping_legacy(host: str, port: int, timeout: float = 5.0) -> LegacyStatus:
    """1.4-1.5 style ping (0xFE 0x01). Raises PingError."""
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            reader = SocketReader(sock, deadline)
            send_all(sock, b"\xfe\x01", deadline)
            header = reader.read_exact(3)
            if header[0] != 0xFF:
                raise PingError("legacy ping was not answered with a kick packet")
            units = int.from_bytes(header[1:3], "big")
            text = reader.read_exact(units * 2).decode("utf-16-be", errors="replace")
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ProtocolError) as exc:
        raise PingError(f"legacy ping to {host}:{port} failed: {exc}") from exc
    return parse_legacy_response(text)
