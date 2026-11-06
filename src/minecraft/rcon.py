"""RCON client for the Minecraft server (Source RCON as vanilla implements it).

Vanilla quirks this client respects:

- The server reads each packet with a single read of at most 1460 bytes and
  closes the connection when the length field does not match what it read.
  So outgoing payloads are limited to 1446 bytes and the client never has two
  packets in flight: it always waits for a reply before sending again.
- Auth failure is answered with request id -1. Some servers send an empty
  type-0 packet before the auth reply; it is skipped.
- Replies are split every 4096 characters. When a reply fragment is that
  long, the client sends a sentinel packet with an unknown type (vanilla
  answers ``Unknown request 64`` with the sentinel's id) and collects
  fragments until the sentinel's reply arrives, or until a short silence if
  the server never answers it. The sentinel is sent only after the first
  fragment arrived, when the command packet has certainly been read on its
  own (sending it right behind the command can coalesce both into one read,
  which vanilla treats as a protocol error).

One persistent connection, guarded by a lock. An idle connection the server
has closed (it restarted) is detected with a non-blocking peek and replaced
before a command is sent. A command is never sent twice: once its packet went
out, a failure is reported, because the server may already have run it.
"""

import contextlib
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass

from src.minecraft.protocol import ConnectionClosed, ProtocolError, SocketReader

logger = logging.getLogger(__name__)

TYPE_RESPONSE = 0
TYPE_COMMAND = 2
TYPE_AUTH_RESPONSE = 2
TYPE_AUTH = 3
TYPE_SENTINEL = 100  # unknown to the server: answered with "Unknown request 64"

MAX_PAYLOAD = 1446  # 1460-byte read - 4 (length) - 8 (id, type) - 2 (terminators)
FRAGMENT_SIZE = 4096
MAX_PACKET = 1 << 20
MAX_RESPONSE = 8 << 20
_AUTH_SKIP_LIMIT = 8


class RconError(Exception):
    """RCON connection/auth/protocol failure."""


class RconUnavailable(RconError):
    """RCON is not configured (no password) or the server is not answering."""


class RconAuthError(RconError):
    """The server rejected the RCON password (or it cannot be sent)."""


class RconProtocolError(RconError):
    """The RCON endpoint answered with data that is not valid RCON."""


AUTH_GUIDANCE = "check rcon.password / enable-rcon in server.properties"


@dataclass(frozen=True)
class RconPacket:
    request_id: int
    packet_type: int
    payload: bytes


def encode_packet(request_id: int, packet_type: int, payload: bytes = b"") -> bytes:
    body = struct.pack("<ii", request_id, packet_type) + payload + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


def read_packet(reader: SocketReader, deadline: float | None) -> RconPacket:
    """Read one packet; nothing is consumed unless the whole packet arrived."""
    (length,) = struct.unpack("<i", reader.peek(4, deadline))
    if not 8 <= length <= MAX_PACKET:
        raise ProtocolError(f"invalid RCON packet length {length}")
    data = reader.read_exact(4 + length, deadline)
    request_id, packet_type = struct.unpack_from("<ii", data, 4)
    payload = data[12:]
    payload = payload[:-2] if payload.endswith(b"\x00\x00") else payload.rstrip(b"\x00")
    return RconPacket(request_id, packet_type, payload)


def _encode_payload(text: str, what: str) -> bytes:
    data = text.encode("utf-8", errors="replace")
    if b"\x00" in data:
        raise RconError(f"RCON {what} cannot contain NUL characters")
    if len(data) > MAX_PAYLOAD:
        raise RconError(
            f"RCON {what} is too long ({len(data)} bytes, max {MAX_PAYLOAD})"
        )
    return data


class RconClient:
    """Thread-safe RCON client with a persistent, auto-reconnecting connection."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 5.0,
        sentinel_timeout: float = 1.0,
    ):
        self.host = host
        self.port = int(port)
        self._password = password
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._sentinel_timeout = sentinel_timeout
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._reader: SocketReader | None = None
        self._request_id = 0

    def __repr__(self) -> str:  # never show the password
        return f"RconClient({self.host}:{self.port})"

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def command(self, command: str) -> str:
        """Run a command; returns the reply text (all fragments, UTF-8 decoded
        with replacement). Raises RconUnavailable, RconAuthError or RconError."""
        payload = _encode_payload(command, "command")
        with self._lock:
            try:
                return self._run(payload)
            except (OSError, ProtocolError) as exc:
                raise self._translate(exc) from exc

    def send_final(self, command: str) -> bool:
        """Send a command that may end the connection (``stop``) on a fresh,
        authenticated connection. True once the command was sent, whether or
        not a reply arrives. Raises RconError if it could not be sent."""
        payload = _encode_payload(command, "command")
        with self._lock:
            self._close_locked()
            try:
                self._connect_locked()
                request_id = self._next_id()
                self._send(request_id, TYPE_COMMAND, payload)
            except (OSError, ProtocolError) as exc:
                self._close_locked()
                raise self._translate(exc) from exc
            try:
                self._read_reply(request_id)
            except (OSError, ProtocolError, RconError) as exc:
                logger.debug("No reply to %r after sending it (%s)", command, exc)
            finally:
                self._close_locked()
            return True

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # ------------------------------------------------------------------

    def _run(self, payload: bytes) -> str:
        try:
            fresh = self._ensure_connected()
            request_id = self._next_id()
            try:
                self._send(request_id, TYPE_COMMAND, payload)
            except ConnectionError:
                if fresh:
                    raise
                # The dead connection refused the packet: it never arrived.
                logger.debug("RCON connection to %s:%s was stale", self.host, self.port)
                self._close_locked()
                self._ensure_connected()
                request_id = self._next_id()
                self._send(request_id, TYPE_COMMAND, payload)
            return self._receive(request_id)
        except BaseException:
            self._close_locked()
            raise

    def _ensure_connected(self) -> bool:
        """Connect and authenticate unless a live connection exists; True if new."""
        if self._sock is not None and not self._peer_closed():
            return False
        self._close_locked()
        self._connect_locked()
        return True

    def _peer_closed(self) -> bool:
        """Non-blocking probe of the idle connection: True when the server has
        closed it (EOF or a reset is pending)."""
        sock = self._sock
        try:
            sock.setblocking(False)
            try:
                return sock.recv(1, socket.MSG_PEEK) == b""
            finally:
                sock.setblocking(True)
        except BlockingIOError:
            return False  # nothing pending: still open
        except OSError:
            return True

    def _connect_locked(self) -> None:
        try:  # an unusable password never opens a connection the server would log
            password = _encode_payload(self._password, "password")
        except RconError as exc:
            raise RconAuthError(f"{exc} ({AUTH_GUIDANCE})") from exc
        sock = socket.create_connection(
            (self.host, self.port), timeout=self._connect_timeout
        )
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._reader = SocketReader(sock)
        try:
            self._authenticate(password)
        except BaseException:
            self._close_locked()
            raise

    def _authenticate(self, password: bytes) -> None:
        request_id = self._next_id()
        self._send(request_id, TYPE_AUTH, password)
        deadline = time.monotonic() + self._read_timeout
        for _ in range(_AUTH_SKIP_LIMIT):
            packet = read_packet(self._reader, deadline)
            if packet.request_id == -1:
                raise RconAuthError(
                    f"RCON authentication failed at {self.host}:{self.port}: "
                    f"the server rejected the password ({AUTH_GUIDANCE})"
                )
            if (
                packet.request_id == request_id
                and packet.packet_type == TYPE_AUTH_RESPONSE
            ):
                return
            # Older servers send an empty RESPONSE_VALUE (type 0) first: skip it.
        raise ProtocolError("no RCON authentication reply")

    def _receive(self, request_id: int) -> str:
        first = self._read_reply(request_id)
        fragments = [first]
        if len(first) >= FRAGMENT_SIZE:
            fragments += self._read_continuation(request_id)
        return b"".join(fragments).decode("utf-8", errors="replace")

    def _read_reply(self, request_id: int) -> bytes:
        deadline = time.monotonic() + self._read_timeout
        while True:
            packet = read_packet(self._reader, deadline)
            if packet.request_id == request_id:
                return packet.payload
            if packet.request_id == -1:
                raise RconAuthError("RCON refused the command: not authenticated")
            # A late reply to an earlier request (an unanswered sentinel): skip.

    def _read_continuation(self, request_id: int) -> list[bytes]:
        sentinel_id = self._next_id()
        self._send(sentinel_id, TYPE_SENTINEL, b"")
        fragments: list[bytes] = []
        total = FRAGMENT_SIZE
        while True:
            try:
                packet = read_packet(
                    self._reader, time.monotonic() + self._sentinel_timeout
                )
            except TimeoutError:
                logger.debug(
                    "RCON sentinel unanswered; using %d extra fragment(s)",
                    len(fragments),
                )
                return fragments
            if packet.request_id == sentinel_id:
                return fragments
            if packet.request_id == request_id:
                fragments.append(packet.payload)
                total += len(packet.payload)
                if total > MAX_RESPONSE:
                    raise ProtocolError("RCON response too large")

    def _send(self, request_id: int, packet_type: int, payload: bytes) -> None:
        self._sock.settimeout(self._read_timeout)
        self._sock.sendall(encode_packet(request_id, packet_type, payload))

    def _next_id(self) -> int:
        self._request_id = self._request_id % 0x7FFFFFFF + 1  # 1..2**31-1, never -1
        return self._request_id

    def _close_locked(self) -> None:
        sock, self._sock, self._reader = self._sock, None, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def _translate(self, exc: Exception) -> RconError:
        where = f"{self.host}:{self.port}"
        if isinstance(exc, ProtocolError):
            return RconProtocolError(f"invalid RCON data from {where}: {exc}")
        if isinstance(exc, ConnectionRefusedError):
            return RconUnavailable(f"RCON connection refused at {where}")
        if isinstance(exc, TimeoutError):
            return RconUnavailable(f"RCON at {where} timed out")
        if isinstance(exc, ConnectionClosed):
            return RconUnavailable(
                f"RCON connection to {where} was closed by the server"
            )
        return RconUnavailable(f"RCON at {where} is unavailable: {exc}")
