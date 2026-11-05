"""Sleeping-server responder: speaks just enough of the Minecraft Java
protocol on the game port while the real server is stopped.

One call of handle_connection() serves one accepted connection:

- first byte 0xFE: legacy (pre-1.7) ping, answered with a 0xFF kick packet;
- handshake next_state 1: Status Request -> Status Response JSON (the
  client's protocol number is echoed unless one is configured, so clients do
  not show "outdated"), then Ping -> Pong;
- handshake next_state 2/3: Login Start. Only the leading name is read, which
  is the same in every version (1.19+ append signature/UUID fields after it),
  and Forge/NeoForge address markers (``\\0FML\\0``, ``\\0FML2\\0``,
  ``\\0FML3\\0``, ``\\0FORGE``) are ignored. The reply is Login Disconnect
  (packet 0x00 with a JSON text string in every version up to 1.21.x).

After replying, the write side is shut down and the socket drained briefly so
the client reads the message and closes first.
"""

import json
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from src.minecraft.protocol import (
    PacketReader,
    ProtocolError,
    SocketReader,
    encode_legacy_kick,
    encode_string,
    frame_packet,
    send_all,
    utf16_length,
)

logger = logging.getLogger(__name__)

FRAME_LIMIT = 32 * 1024
MAX_ADDRESS_LENGTH = 1024  # vanilla allows 255; markers/proxies may append more
MAX_STATUS_JSON = 32767  # the vanilla client's limit for the status string
MAX_NAME_LENGTH = 16
LEGACY_PROTOCOL = 127  # what vanilla answers legacy pings with
DRAIN_SECONDS = 1.0
DRAIN_LIMIT = 64 * 1024
ACCESS_ERROR_MESSAGE = "The server could not check your access. Please try again later."
INVALID_NAME_MESSAGE = "Invalid player name."


class ConnectionKind:
    STATUS = "status"  # server-list ping (handshake next_state=1)
    LOGIN = "login"  # join attempt (next_state=2, or 3 = transfer)
    LEGACY_PING = "legacy_ping"  # 0xFE pre-1.7 style ping
    INVALID = "invalid"  # garbage, timeout, disconnect mid-handshake


@dataclass(frozen=True)
class SleepingServerConfig:
    motd: str  # may contain section-sign formatting codes
    version_name: str  # shown in the server list, e.g. "Sleeping"
    protocol_version: int | None  # None -> echo the client's protocol
    max_players: int
    favicon: str | None  # "data:image/png;base64,..." or None
    kick_message: str  # shown to an allowed player whose login wakes the server
    whitelist_message: str = "You are not white-listed on this server!"
    banned_message: str = "You are banned from this server."


@dataclass(frozen=True)
class ConnectionOutcome:
    kind: str
    player_name: str | None = None
    protocol_version: int | None = None
    wake: bool = False  # True only for an allowed LOGIN


# is_allowed(player_name, client_ip) -> (allowed, deny_message_or_None)
AccessCheck = Callable[[str, str], tuple[bool, str | None]]


@dataclass(frozen=True)
class Handshake:
    protocol: int
    address: str  # without Forge markers
    port: int
    next_state: int
    marker: str | None = None  # "FML", "FML2", "FML3", "FORGE"...


def handle_connection(
    conn: socket.socket,
    client_ip: str,
    config: SleepingServerConfig,
    is_allowed: AccessCheck,
    timeout: float = 5.0,
) -> ConnectionOutcome:
    """Serve one accepted client connection as the sleeping server.

    Never raises for client/protocol errors (returns INVALID). Does not close
    the listening socket; closes nothing but may shut down `conn` gracefully
    after the response is sent. The caller closes `conn`.
    """
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = 5.0
    reader = SocketReader(conn, time.monotonic() + timeout)
    try:
        outcome = _serve(conn, reader, client_ip, config, is_allowed)
    except (OSError, ProtocolError) as exc:
        logger.debug(
            "Connection from %s ended without a valid request: %s", client_ip, exc
        )
        return ConnectionOutcome(ConnectionKind.INVALID)
    except Exception:
        logger.exception("Unexpected error serving a connection from %s", client_ip)
        return ConnectionOutcome(ConnectionKind.INVALID)
    _finish(conn)
    return outcome


def _serve(conn, reader, client_ip, config, is_allowed) -> ConnectionOutcome:
    if reader.peek_byte() == 0xFE:
        send_all(conn, _legacy_response(config), reader.deadline)
        return ConnectionOutcome(ConnectionKind.LEGACY_PING)
    handshake = read_handshake(reader)
    if handshake.next_state == 1:
        _serve_status(conn, reader, config, handshake.protocol)
        return ConnectionOutcome(
            ConnectionKind.STATUS, protocol_version=handshake.protocol
        )
    if handshake.next_state in (2, 3):
        return _serve_login(
            conn, reader, client_ip, config, is_allowed, handshake.protocol
        )
    raise ProtocolError(f"unknown handshake next state {handshake.next_state}")


def read_handshake(reader: SocketReader) -> Handshake:
    packet_id, payload = reader.read_packet(FRAME_LIMIT)
    if packet_id != 0x00:
        raise ProtocolError(f"expected a handshake, got packet 0x{packet_id:02x}")
    body = PacketReader(payload)
    protocol = body.read_varint()
    address = body.read_string(MAX_ADDRESS_LENGTH)
    port = body.read_ushort()
    next_state = body.read_varint()
    host, _, marker = address.partition("\0")
    return Handshake(protocol, host, port, next_state, marker.strip("\0") or None)


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------


def _serve_status(conn, reader, config, client_protocol) -> None:
    packet_id, payload = reader.read_packet(FRAME_LIMIT)
    if packet_id == 0x00:
        response = encode_string(status_json(config, client_protocol), MAX_STATUS_JSON)
        send_all(conn, frame_packet(0x00, response), reader.deadline)
        try:
            packet_id, payload = reader.read_packet(FRAME_LIMIT)
        except (OSError, ProtocolError):
            return  # many pingers close right after the status response
    elif packet_id != 0x01:
        raise ProtocolError(f"expected a status request, got packet 0x{packet_id:02x}")
    if packet_id == 0x01 and len(payload) == 8:
        send_all(conn, frame_packet(0x01, payload), reader.deadline)


def status_json(config: SleepingServerConfig, client_protocol: int) -> str:
    """The Status Response JSON; drops the favicon (then trims the MOTD) to
    stay within what the vanilla client accepts."""
    protocol = (
        client_protocol if config.protocol_version is None else config.protocol_version
    )
    status = {
        "version": {"name": config.version_name, "protocol": protocol},
        "players": {
            "max": _safe_int(config.max_players, 20),
            "online": 0,
            "sample": [],
        },
        "description": {"text": config.motd},
    }
    if config.favicon:
        status["favicon"] = config.favicon
    text = json.dumps(status, ensure_ascii=False, separators=(",", ":"))
    if utf16_length(text) > MAX_STATUS_JSON and "favicon" in status:
        logger.warning(
            "Sleeping-server favicon dropped: the status response would be too long"
        )
        del status["favicon"]
        text = json.dumps(status, ensure_ascii=False, separators=(",", ":"))
    if utf16_length(text) > MAX_STATUS_JSON:
        status["description"] = {"text": config.motd[:1024]}
        text = json.dumps(status, ensure_ascii=False, separators=(",", ":"))
    return text


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _legacy_response(config: SleepingServerConfig) -> bytes:
    fields = [
        "§1",
        str(LEGACY_PROTOCOL),
        config.version_name,
        config.motd,
        "0",
        str(_safe_int(config.max_players, 20)),
    ]
    return encode_legacy_kick("\0".join(field.replace("\0", " ") for field in fields))


# ----------------------------------------------------------------------
# Login
# ----------------------------------------------------------------------


def _serve_login(
    conn, reader, client_ip, config, is_allowed, protocol
) -> ConnectionOutcome:
    packet_id, payload = reader.read_packet(FRAME_LIMIT)
    if packet_id != 0x00:
        raise ProtocolError(f"expected Login Start, got packet 0x{packet_id:02x}")
    name = PacketReader(payload).read_string(MAX_NAME_LENGTH)
    if not _valid_name(name):
        _send_disconnect(conn, reader.deadline, INVALID_NAME_MESSAGE)
        return ConnectionOutcome(ConnectionKind.LOGIN, name, protocol, wake=False)
    allowed, message = _check_access(is_allowed, name, client_ip, config)
    _send_disconnect(conn, reader.deadline, config.kick_message if allowed else message)
    return ConnectionOutcome(ConnectionKind.LOGIN, name, protocol, wake=allowed)


def _valid_name(name: str) -> bool:
    return bool(name) and all(" " < char != "\x7f" for char in name)


def _check_access(is_allowed, name, client_ip, config) -> tuple[bool, str]:
    try:
        allowed, message = is_allowed(name, client_ip)
    except Exception:
        logger.exception("Access check failed for %s (%s)", name, client_ip)
        return False, ACCESS_ERROR_MESSAGE
    if allowed:
        return True, ""
    return False, message or config.whitelist_message


def _send_disconnect(conn, deadline, message: str) -> None:
    payload = encode_string(json.dumps({"text": message}, ensure_ascii=False), 262144)
    send_all(conn, frame_packet(0x00, payload), deadline)


# ----------------------------------------------------------------------


def _finish(conn: socket.socket) -> None:
    """Half-close, then drain until the client closes (bounded), so the client
    reads the reply and holds the TIME_WAIT state. Never raises."""
    try:
        conn.shutdown(socket.SHUT_WR)
    except OSError:
        return
    deadline = time.monotonic() + DRAIN_SECONDS
    drained = 0
    try:
        while drained < DRAIN_LIMIT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            conn.settimeout(remaining)
            chunk = conn.recv(4096)
            if not chunk:
                return
            drained += len(chunk)
    except OSError:
        return
