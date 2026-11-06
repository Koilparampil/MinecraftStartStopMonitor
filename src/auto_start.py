# Copyright (c) 2024 Nomomo
# Copyright (c) 2026 Kevin Perez - Modified work
# Copyright (c) 2026 Koilparampil - Modified work (Minecraft port)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

"""Auto-start manager: a "sleeping" Minecraft server on the game port.

While the committed lifecycle state is STOPPED the manager listens on the game
port and answers like a Minecraft server (src.minecraft.sleeping): the
multiplayer list shows the sleeping MOTD, and the first allowed join attempt
releases the port and asks the controller to start the real server.

Design (fixes the reference's shared ``is_aborting`` flag, which had to be
reset by hand at the start of every cycle):

- Every bind -> accept -> wake/cancel run is a single-use ``_ListenCycle`` with
  its own cancel event, socket and thread. Nothing is reused or reset; a new
  cycle is a new object.
- The cycle's accept thread is the only owner of the listening socket. It
  closes the socket itself (so no other thread is inside a syscall on it and
  the kernel frees the port at once), sets ``released``, and only then calls
  ``controller.start_server()`` -- at most once per cycle.
- ``stop_listening()`` cancels the current cycle and waits for ``released``.
  It never joins a thread that may be inside the controller, so it cannot
  deadlock with the lifecycle thread that publishes STARTING.
- The cycle thread never reads lifecycle state; ``listen()`` does, guarded by
  a disarm counter so a STARTING that lands while ``listen()`` runs cannot
  leave a listener bound during the launch.
"""

import array
import contextlib
import dataclasses
import errno
import ipaddress
import json
import logging
import os
import select
import selectors
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from src.events import Event, EventBus
from src.lifecycle.messages import LifecycleState
from src.minecraft import server_files, sleeping
from src.minecraft.protocol import (
    PacketReader,
    encode_string,
    frame_packet,
    safe_for_log,
)
from src.settings import DEFAULT_GAME_PORT, settings

try:  # FIONREAD: bytes waiting in a socket's receive buffer (POSIX)
    import fcntl
    import termios
except ImportError:  # Windows
    fcntl = termios = None

logger = logging.getLogger(__name__)

DEFAULT_MAX_PLAYERS = 20
WHITELIST_MESSAGE = "You are not white-listed on this server!"
BANNED_MESSAGE = "You are banned from this server."
BANNED_IP_MESSAGE = "Your IP address is banned from this server."
_BAN_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"
BUSY_MESSAGE = "Too many connections, try again in a moment."
FINAL_WAIT = 0.5  # seconds a closing triage still waits for requests to arrive
_REQUEST_LIMIT = 2 * sleeping.FRAME_LIMIT + 8  # a handshake plus the next packet

# accept() errors that concern one client only (keep accepting), and the ones
# meaning the process is out of resources (back off, keep the port bound).
_TRANSIENT_ACCEPT_ERRNOS = frozenset(
    code
    for code in (
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.EPROTO,
        errno.EPERM,
        errno.EINTR,
        errno.EAGAIN,
        errno.ETIMEDOUT,
        getattr(errno, "WSAECONNRESET", None),
        getattr(errno, "WSAECONNABORTED", None),
        getattr(errno, "WSAEWOULDBLOCK", None),
    )
    if code is not None
)
_RESOURCE_ERRNOS = frozenset(
    code
    for code in (
        errno.EMFILE,
        errno.ENFILE,
        errno.ENOBUFS,
        errno.ENOMEM,
        getattr(errno, "WSAEMFILE", None),
        getattr(errno, "WSAENOBUFS", None),
    )
    if code is not None
)

# Errors after which a dual-stack bind falls back to plain IPv4.
_IPV4_FALLBACK_ERRNOS = {
    errno.EAFNOSUPPORT,
    errno.EADDRNOTAVAIL,
    errno.EPROTONOSUPPORT,
    errno.EINVAL,
}


@dataclass(frozen=True)
class ListenerOptions:
    """Timing and capacity knobs (tests shrink them)."""

    bind_attempts: int = 5  # fast retries, as in the reference
    retry_delay: float = 2.0
    slow_retry_delay: float = 30.0  # afterwards keep trying while still armed
    accept_timeout: float = 0.2  # how quickly a cancel/wake is noticed
    connection_timeout: float = 5.0  # per client connection, overall
    request_timeout: float = 2.0  # the first request must have arrived by then
    status_timeout: float = 1.0  # a server-list ping, its Ping/Pong included
    eviction_grace: float = 0.25  # younger connections are not evicted by newcomers
    request_stall: float = (
        0.1  # clients send their whole request this soon after connecting
    )
    max_connections: int = 64  # concurrent connections being served
    max_connections_per_client: int = 4  # per IPv4 address / IPv6 /56
    triage_capacity: int = 256  # sockets waiting in the no-slot triage
    triage_timeout: float = 1.0  # their request must arrive by then
    release_timeout: float = 5.0  # stop_listening() wait for the port release


# ----------------------------------------------------------------------
# Sockets
# ----------------------------------------------------------------------


def bind_address(host, port):
    """(family, sockaddr, dualstack) for listening on ``host:port``.

    ``""`` (and ``"::"``) mean all interfaces: one dual-stack IPv6 socket when
    the platform supports it (like the Java server), else IPv4 ``0.0.0.0``.
    """
    host = (host or "").strip()
    if host in ("", "::") and socket.has_dualstack_ipv6():
        return socket.AF_INET6, ("::", port), True
    if host == "":
        return socket.AF_INET, ("0.0.0.0", port), False
    family, _type, _proto, _name, sockaddr = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )[0]
    return family, sockaddr, False


def _set_reuse_options(sock):
    if os.name == "nt":
        # Never SO_REUSEADDR on Windows: it would let another socket steal the port.
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    else:
        # Rebind even while old connections linger in TIME_WAIT.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def _open_listener(family, sockaddr, dualstack, backlog):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        _set_reuse_options(sock)
        if dualstack:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(sockaddr)
        sock.listen(backlog)
        return sock
    except BaseException:
        sock.close()
        raise


def create_listener(host, port, backlog=32):
    """Bind and listen on the game port (dual-stack for all interfaces)."""
    family, sockaddr, dualstack = bind_address(host, port)
    try:
        return _open_listener(family, sockaddr, dualstack, backlog)
    except OSError as e:
        if not dualstack or e.errno not in _IPV4_FALLBACK_ERRNOS:
            raise
        logger.debug(f"Dual-stack bind failed ({e}); falling back to IPv4")
        return _open_listener(socket.AF_INET, ("0.0.0.0", port), False, backlog)


def normalize_client_ip(ip):
    """Plain address text; IPv4-mapped IPv6 (``::ffff:1.2.3.4``) -> ``1.2.3.4``."""
    text = str(ip or "")
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return text
    mapped = getattr(address, "ipv4_mapped", None)
    return str(mapped or address)


def accept_error_action(error):
    """What the accept loop does after *error*: ``"retry"`` (one client's
    problem), ``"backoff"`` (out of descriptors/memory) or ``"rebind"`` (the
    listening socket itself is broken)."""
    if isinstance(error, (ConnectionError, InterruptedError, BlockingIOError)):
        return "retry"
    code = getattr(error, "errno", None)
    if code in _TRANSIENT_ACCEPT_ERRNOS:
        return "retry"
    if code in _RESOURCE_ERRNOS:
        return "backoff"
    return "rebind"


def client_key(ip):
    """Per-client cap key: the IPv4 address, or the /56 network of an IPv6
    one (homes are commonly delegated a /56 or /48, so /64 keys would give a
    single host 256 of them)."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/56", strict=False))
    return str(address)


# Phases of an admitted connection: "pending" until its first request has
# arrived, then the request's kind ("status", "legacy", "login", or "refused"
# for an invalid name), then the access check's "allowed" or "refused".
ALLOWED = "allowed"  # a login about to wake the server: never evicted
_IN_PROGRESS = frozenset({"pending", "login"})  # spared while young


def bytes_waiting(conn) -> bool:
    """Whether bytes from the client wait unread in *conn*'s receive buffer.

    Never blocks and never consumes. POSIX asks the kernel (FIONREAD): a
    ``recv(1, MSG_PEEK | MSG_DONTWAIT)`` through the socket object would
    first wait out the socket's timeout. Windows has no FIONREAD for Python
    sockets: there a readable socket (data, or a close not yet read) counts.
    """
    try:
        if fcntl is not None:
            count = array.array("i", [0])
            fcntl.ioctl(conn.fileno(), termios.FIONREAD, count, True)
            return count[0] > 0
        readable, _writable, _failed = select.select([conn], [], [], 0)
        return bool(readable)
    except (OSError, ValueError):
        return False


def _wait_readable(conn, timeout) -> bool:
    """Data, a close or an error is ready on *conn* within *timeout* s."""
    timeout = max(0.0, timeout)
    if hasattr(select, "poll"):  # no FD_SETSIZE limit
        poller = select.poll()
        poller.register(conn, select.POLLIN)
        return bool(poller.poll(timeout * 1000))
    readable, _writable, _failed = select.select([conn], [], [], timeout)
    return bool(readable)


class _Admission:
    """One admitted connection and its phase (see the phases above)."""

    def __init__(self, conn, key, admitted_at, phase="pending", clock=time.monotonic):
        self.conn = conn
        self.key = key
        self.admitted_at = admitted_at
        self.phase = phase
        self.first_byte_at = None  # when its handler read the first request byte
        self.evicted = False
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False

    def receive(self, size, timeout):
        """Wait up to *timeout* s for request bytes, then read them. The read
        and noting its time are one step for the evictor's probe (both hold
        the lock), so a request in progress never looks idle. None: nothing
        to read yet; b"": closed. Raises TimeoutError and OSError."""
        if not _wait_readable(self.conn, timeout):
            raise TimeoutError("timed out")
        with self._lock:
            if self._closed:
                return b""
            self.conn.settimeout(0)  # it is readable: never block holding the lock
            try:
                chunk = self.conn.recv(size)
            except BlockingIOError:
                return None
            if chunk and self.first_byte_at is None:
                self.first_byte_at = self._clock()
            return chunk

    def spared_until(self, stall) -> float:
        """A client sends its whole request as soon as it has connected:
        until then (and *stall* s more) it is not idle, just not there yet."""
        started = self.admitted_at if self.first_byte_at is None else self.first_byte_at
        return started + stall

    def delivering(self, probe, now, stall) -> bool:
        """Its client is still delivering its request: bytes wait unread
        (*probe*), or it connected or started the request less than *stall*
        seconds ago."""
        with self._lock:
            if self._closed:
                return False
            return now < self.spared_until(stall) or probe(self.conn)

    def has_bytes(self, probe) -> bool:
        with self._lock:
            return not self._closed and probe(self.conn)

    def abort(self, how=socket.SHUT_RDWR):
        """Evicted: wake the handler's blocked read so it gives up at once.
        With SHUT_RD a reply that is being sent still goes out."""
        with self._lock:
            if not self._closed:
                with contextlib.suppress(OSError):
                    self.conn.shutdown(how)

    def close(self):
        with self._lock:
            self._closed = True
            _close_quietly(self.conn)


class ConnectionSlots:
    """Admission control: a global cap and a per-client cap.

    When a cap is reached, a newcomer evicts, oldest first:

    1. a cheap connection: a ping, a legacy ping, a refused login -- every
       phase except a request in progress and an allowed login, so a new
       phase can never become unevictable by accident. Spared while younger
       than *grace*, except when a complete login needs the room;
    2. else a request in progress (``pending`` its request, ``login`` its
       access check) older than *grace*;
    3. else, for a complete login only, a young ``pending`` connection whose
       client is not delivering a request: nothing unread in its socket
       buffer, and neither connected nor started a request within the last
       *stall* seconds (clients send the whole request at once). Idle ones
       go first, then those stalled after a few bytes. A player of the same
       burst, whose request waits in the buffer or is arriving, is spared;
       retry_at() tells a login refused for that alone when to try again.

    Allowed logins (about to wake the server) are never evicted, so admit()
    returns None only when nothing else may be evicted. Victims are shut
    down for reading only (SHUT_RD), which wakes their handler's read: a
    reply being sent (a refusal, a status) still goes out, a login evicted
    during its access check still gets its answer, and a request that
    completed just as it was evicted can still be told "busy". Only a
    connection that waited too long for its request is shut down
    completely.
    """

    def __init__(
        self,
        total,
        per_client,
        grace=0.0,
        clock=time.monotonic,
        probe=bytes_waiting,
        stall=0.1,
    ):
        self._total = total
        self._per_client = per_client
        self._grace = grace
        self._stall = stall
        self._clock = clock
        self._probe = probe  # bytes_waiting(conn); tests pass fakes
        self._lock = threading.Lock()
        self._entries: list[_Admission] = []  # oldest first

    @property
    def in_use(self):
        with self._lock:
            return len(self._entries)

    def admit(self, conn, key, phase="pending"):
        """A slot for *conn* (evicting if needed), or None."""
        now = self._clock()
        victim = None
        with self._lock:
            full = self._full(key)
            if full is not None:
                victim, how = self._victim(full, now, for_login=phase == "login")
                if victim is None:
                    return None
                victim.evicted = True
                self._entries.remove(victim)
            entry = _Admission(conn, key, now, phase, self._clock)
            self._entries.append(entry)
        if victim is not None:
            victim.abort(how)
        return entry

    def _full(self, key):
        same_client = [entry for entry in self._entries if entry.key == key]
        if len(same_client) >= self._per_client:
            return same_client
        if len(self._entries) >= self._total:
            return self._entries
        return None

    def _victim(self, entries, now, for_login):
        """(connection to evict, how to shut it down), or (None, None); see
        the class docstring for the order."""
        cheap_grace = 0.0 if for_login else self._grace
        for entry in entries:
            cheap = entry.phase not in _IN_PROGRESS and entry.phase != ALLOWED
            if cheap and now - entry.admitted_at >= cheap_grace:
                return entry, socket.SHUT_RD
        for entry in entries:
            if entry.phase in _IN_PROGRESS and now - entry.admitted_at >= self._grace:
                stalled = entry.phase == "pending"
                return entry, socket.SHUT_RDWR if stalled else socket.SHUT_RD
        if for_login:
            young = [entry for entry in entries if entry.phase == "pending"]
            young.sort(key=lambda entry: entry.first_byte_at is not None)
            for entry in young:
                if not entry.delivering(self._probe, now, self._stall):
                    return entry, socket.SHUT_RD
        return None, None

    def retry_at(self, key):
        """When a complete login of *key* that found no room may try again:
        the moment a connection spared only as just connected (or just
        started) stops being spared, if its client is silent; else None."""
        with self._lock:
            full = self._full(key)
            if full is None:
                return self._clock()
            times = [
                entry.spared_until(self._stall)
                for entry in full
                if entry.phase == "pending" and not entry.has_bytes(self._probe)
            ]
        return min(times) if times else None

    def promote(self, entry, phase):
        """Its request arrived (its kind), or the access check decided
        (``"allowed"`` / ``"refused"``). False if evicted."""
        with self._lock:
            if entry.evicted:
                return False
            entry.phase = phase
            return True

    def release(self, entry):
        with self._lock:
            if entry in self._entries:
                self._entries.remove(entry)


def _varint_at(data, offset):
    """(value, end) of the VarInt at *offset*, None if incomplete. ValueError
    if it is longer than any frame length up to 32 KiB needs (3 bytes)."""
    value = 0
    for index in range(3):
        if offset + index >= len(data):
            return None
        byte = data[offset + index]
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return value, offset + index + 1
    raise ValueError("frame length too long")


def _frames(data, count):
    """Payloads of the first *count* frames of *data*, None if incomplete."""
    payloads, offset = [], 0
    for _ in range(count):
        found = _varint_at(data, offset)
        if found is None:
            return None
        length, offset = found
        if length > sleeping.FRAME_LIMIT:
            raise ValueError("frame too long")
        if offset + length > len(data):
            return None
        payloads.append(data[offset : offset + length])
        offset += length
    return payloads


def _handshake(payload):
    """(protocol, next_state) of a handshake packet; ValueError otherwise."""
    reader = PacketReader(payload)
    if reader.read_varint() != 0x00:
        raise ValueError("not a handshake")
    protocol = reader.read_varint()
    reader.read_string(sleeping.MAX_ADDRESS_LENGTH)
    reader.read_ushort()
    return protocol, reader.read_varint()


def _valid_login_name(payload):
    """The Login Start name is one the sleeping server accepts (else the
    login is refused before any access check)."""
    try:
        reader = PacketReader(payload)
        if reader.read_varint() != 0x00:
            return False
        name = reader.read_string(sleeping.MAX_NAME_LENGTH)
    except ValueError:
        return False
    return bool(name) and all(" " < char != "\x7f" for char in name)


class MinecraftRequests:
    """Recognises a client's first request -- the handshake plus the packet
    after it (Status Request or Login Start), which every client sends right
    away, or a legacy 0xFE ping -- and builds the short replies for
    connections that get no slot."""

    @staticmethod
    def classify(data):
        """None while incomplete, then "legacy", "status", "login" or
        "refused" (a login with an invalid name); ValueError for anything
        that is not a request."""
        if not data:
            return None
        if data[0] == 0xFE:
            return "legacy"
        frames = _frames(data, 2)
        if frames is None:
            return None
        _protocol, next_state = _handshake(frames[0])
        if next_state == 1:
            return "status"
        if next_state in (2, 3):
            return "login" if _valid_login_name(frames[1]) else "refused"
        raise ValueError(f"unknown next state {next_state}")

    @staticmethod
    def busy_reply(kind, data, config, message=None):
        """*message* ("Too many connections" by default) for a login, the
        sleeping status (without the icon) for a ping; None to just close."""
        if kind in ("login", "refused"):
            text = json.dumps({"text": message or BUSY_MESSAGE})
            return frame_packet(0x00, encode_string(text, None))
        if kind == "status":
            protocol, _next_state = _handshake(_frames(data, 1)[0])
            plain = dataclasses.replace(config, favicon=None)
            text = sleeping.status_json(plain, protocol)
            return frame_packet(0x00, encode_string(text, sleeping.MAX_STATUS_JSON))
        return None


def read_request(conn, timeout, classify, receive=None):
    """Read the client's first request within *timeout* seconds in total.

    Returns (bytes read, kind); the bytes are replayed to the protocol
    handler. (None, None) if the request did not arrive complete in time,
    the client closed, or it is not a valid request. *receive(size,
    timeout)* replaces ``conn.recv`` (an admitted connection notes when its
    request started; None means nothing to read yet)."""
    deadline = time.monotonic() + timeout
    data = b""
    while len(data) <= _REQUEST_LIMIT:
        try:
            kind = classify(data)
        except ValueError:
            return None, None
        if kind is not None:
            return data, kind
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None
        try:
            if receive is None:
                conn.settimeout(remaining)
                chunk = conn.recv(4096)
            else:
                chunk = receive(4096, remaining)
        except (OSError, ValueError):  # timeout, reset, shut down by an eviction
            return None, None
        if chunk is None:
            continue
        if not chunk:
            return None, None
        data += chunk
    return None, None


class _ReplaySocket:
    """The accepted socket with its already-read request put back in front."""

    def __init__(self, sock, data):
        self._sock = sock
        self._pending = data

    def recv(self, size, flags=0):
        if not self._pending:
            return self._sock.recv(size, flags)
        data = self._pending[:size]
        if not flags & socket.MSG_PEEK:
            self._pending = self._pending[size:]
        return data

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _Triage:
    """Connections that got no slot, all on one thread with non-blocking
    reads, so it never holds up the accept loop. Once a request is complete,
    a login takes a slot by evicting a non-login connection and is served
    normally; anything else (or a login when every slot holds a login) gets
    a short reply and is closed. A login that finds only connections spared
    for a moment (just connected, request arriving) waits that moment once
    (ConnectionSlots.retry_at). Bounded: past *capacity* waiting sockets
    the oldest is dropped, and each one waits at most *timeout*."""

    def __init__(self, cycle, capacity, timeout):
        self._cycle = cycle
        self._capacity = capacity
        self._timeout = timeout
        self._lock = threading.Lock()
        self._incoming = []
        self._waiting = {}  # socket -> [ip, key, config, deadline, data]
        self._deferred = []  # logins retried once: (at, conn, ip, key, config, data)
        self._closed = False
        self._final_message = None  # set by close(): the reply for logins
        self._selector = selectors.DefaultSelector()
        try:
            self._wake_r, self._wake_w = socket.socketpair()
            self._wake_r.setblocking(False)
            self._wake_w.setblocking(False)
            self._selector.register(self._wake_r, selectors.EVENT_READ)
            self.thread = threading.Thread(
                target=self._run, name="minecraft-autostart-triage", daemon=True
            )
            self.thread.start()
        except BaseException:
            for sock in (
                getattr(self, "_wake_r", None),
                getattr(self, "_wake_w", None),
            ):
                if sock is not None:
                    _close_quietly(sock)
            self._selector.close()
            raise

    def add(self, conn, ip, key, config):
        with self._lock:
            if self._closed:
                _close_quietly(conn)
                return
            self._incoming.append((conn, ip, key, config))
        self._poke()

    def close(self, login_message=BUSY_MESSAGE):
        """Stop. Requests that are complete (or arrive within FINAL_WAIT) are
        still answered: logins get *login_message* ("starting" after a wake)."""
        with self._lock:
            self._closed = True
            self._final_message = login_message
        self._poke()

    def _poke(self):
        with contextlib.suppress(OSError):
            self._wake_w.send(b"\0")

    def _run(self):
        try:
            while True:
                with self._lock:
                    if self._closed:
                        break
                    incoming, self._incoming = self._incoming, []
                for item in incoming:
                    self._register(*item)
                for key, _events in self._selector.select(self._next_timeout()):
                    if key.fileobj is self._wake_r:
                        with contextlib.suppress(OSError):
                            self._wake_r.recv(4096)
                    else:
                        self._read(key.fileobj)
                self._expire()
                self._retry_deferred()
        except Exception:
            logger.exception("Auto-start triage failed")
        finally:
            self._shutdown()

    def _register(self, conn, ip, key, config):
        if len(self._waiting) >= self._capacity:
            self._drop(next(iter(self._waiting)))  # the oldest
        try:
            conn.setblocking(False)
            self._selector.register(conn, selectors.EVENT_READ)
        except (OSError, ValueError):
            _close_quietly(conn)
            return
        deadline = time.monotonic() + self._timeout
        self._waiting[conn] = [ip, key, config, deadline, b""]

    def _next_timeout(self):
        times = [state[3] for state in self._waiting.values()]
        times += [item[0] for item in self._deferred]
        if not times:
            return None
        return max(0.0, min(times) - time.monotonic())

    def _read(self, conn):
        state = self._waiting.get(conn)
        if state is None:
            return
        try:
            chunk = conn.recv(4096)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk or len(state[4]) + len(chunk) > _REQUEST_LIMIT:
            self._drop(conn)
            return
        state[4] += chunk
        try:
            kind = self._cycle.requests.classify(state[4])
        except ValueError:
            self._drop(conn)
            return
        if kind is not None:
            self._forget(conn)
            self._resolve(conn, kind, *state)

    def _resolve(self, conn, kind, ip, key, config, _deadline, data, retried=False):
        final = self._final_message
        if final is None and kind == "login":
            if self._cycle.admit_login(conn, ip, key, config, data):
                return
            at = None if retried else self._cycle.retry_login_at(key)
            if at is not None and len(self._deferred) < self._capacity:
                self._deferred.append((at, conn, ip, key, config, data))
                return
        try:
            reply = self._cycle.requests.busy_reply(kind, data, config, final)
            if reply:
                conn.send(reply)  # small: fits the socket buffer, never blocks
        except (OSError, ValueError):
            pass
        _close_quietly(conn)

    def _expire(self):
        now = time.monotonic()
        for conn in [c for c, state in self._waiting.items() if state[3] <= now]:
            self._drop(conn)

    def _retry_deferred(self, everything=False):
        now = time.monotonic()
        due = [item for item in self._deferred if everything or item[0] <= now]
        if not due:
            return
        self._deferred = [item for item in self._deferred if item not in due]
        for _at, conn, ip, key, config, data in due:
            self._resolve(conn, "login", ip, key, config, None, data, retried=True)

    def _forget(self, conn):
        self._waiting.pop(conn, None)
        with contextlib.suppress(KeyError, ValueError, OSError):
            self._selector.unregister(conn)

    def _drop(self, conn):
        self._forget(conn)
        _close_quietly(conn)

    def _shutdown(self):
        """Answer what is still here: requests that are complete or arrive
        within FINAL_WAIT get their reply; the rest are read before closing,
        so clients see a clean close rather than a reset."""
        with self._lock:
            leftover, self._incoming = self._incoming, []
        for item in leftover:
            self._register(*item)
        deadline = time.monotonic() + FINAL_WAIT
        while self._waiting and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            for key, _events in self._selector.select(remaining):
                if key.fileobj is not self._wake_r:
                    self._read(key.fileobj)
        for conn in list(self._waiting):
            with contextlib.suppress(OSError):
                conn.recv(_REQUEST_LIMIT)  # nothing unread: FIN, not RST
            self._drop(conn)
        self._retry_deferred(everything=True)  # the final message
        self._selector.close()
        _close_quietly(self._wake_r)
        _close_quietly(self._wake_w)


def _close_quietly(sock):
    with contextlib.suppress(OSError):
        sock.close()


class _RefusalLog:
    """Logs refused join attempts without flooding the log: the first one at
    once, then at most one line per *interval* with the count since."""

    def __init__(self, interval=10.0, clock=time.monotonic):
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._quiet_until = 0.0
        self._suppressed = 0

    def note(self, name, ip, reason):
        """*name* is what the client sent (control characters are often
        why it was refused): it is escaped before it is logged."""
        with self._lock:
            now = self._clock()
            if now < self._quiet_until:
                self._suppressed += 1
                return
            suppressed, self._suppressed = self._suppressed, 0
            self._quiet_until = now + self._interval
        more = (
            f" ({suppressed} more refused since the last report)" if suppressed else ""
        )
        logger.info(
            f"Not starting the server for {safe_for_log(name)} ({ip}): {reason}{more}"
        )


# ----------------------------------------------------------------------
# Access policy
# ----------------------------------------------------------------------


def _ban_is_active(entry, now):
    expires = str(entry.get("expires") or "forever").strip()
    if expires.lower() == "forever":
        return True
    try:
        return datetime.strptime(expires, _BAN_DATE_FORMAT).timestamp() > now
    except ValueError:
        return True  # unparseable expiry: treat like the server would, as banned


def _ban_message(base, entry):
    reason = str(entry.get("reason") or "").strip()
    return f"{base}\nReason: {reason}" if reason else base


class AccessPolicy:
    """Who may wake the server. Mirrors the server's own ban lists and whitelist.

    Files are read on every check: join attempts are rare and the files can
    change while the server sleeps (offline bans, manual edits).
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock

    def check(self, name, client_ip):
        """(allowed, deny_message_or_None) for a join attempt."""
        if not settings.respectWhitelist or not settings.serverDir:
            return True, None
        lowered = (name or "").lower()
        now = self._clock()

        for entry in self._read("banned-players.json"):
            if str(entry.get("name", "")).lower() == lowered and _ban_is_active(
                entry, now
            ):
                return False, _ban_message(BANNED_MESSAGE, entry)
        ip = normalize_client_ip(client_ip)
        for entry in self._read("banned-ips.json"):
            if normalize_client_ip(entry.get("ip")) == ip and _ban_is_active(
                entry, now
            ):
                return False, _ban_message(BANNED_IP_MESSAGE, entry)

        props = settings.read_server_properties()
        if str(props.get("white-list", "")).strip().lower() == "true":
            allowed = {
                str(entry.get("name", "")).lower()
                for file_name in ("whitelist.json", "ops.json")
                for entry in self._read(file_name)
            }
            if lowered not in allowed:
                return False, WHITELIST_MESSAGE
        return True, None

    @staticmethod
    def _read(file_name):
        entries = server_files.read_json_list(
            os.path.join(settings.serverDir, file_name)
        )
        return [entry for entry in entries if isinstance(entry, dict)]


# ----------------------------------------------------------------------
# One listen cycle
# ----------------------------------------------------------------------


class _ListenCycle:
    """One bind -> accept -> (wake | cancel) run. Single use, never re-armed."""

    def __init__(self, manager, number, previous=None):
        self._manager = manager
        self._options = manager.options
        self._previous = previous
        self.number = number
        self.cancelled = threading.Event()
        self.released = threading.Event()  # holds no socket and never will again
        self._woken = threading.Event()
        self._claim_lock = threading.Lock()
        self._waker = None
        self._sock = None
        self._slots = ConnectionSlots(
            self._options.max_connections,
            self._options.max_connections_per_client,
            grace=self._options.eviction_grace,
            stall=self._options.request_stall,
        )
        self.requests = manager.requests
        self._triage = None
        self._config = None
        self.bind_failures = 0
        self.bind_error = None  # why the last bind failed, until one succeeds
        self.thread = threading.Thread(
            target=self._run, name=f"minecraft-autostart-{number}", daemon=True
        )

    # --- state seen by the manager -----------------------------------

    @property
    def active(self):
        """Armed: neither cancelled nor woken, and its thread is still running."""
        return (
            self.thread.is_alive()
            and not self.cancelled.is_set()
            and not self._woken.is_set()
        )

    @property
    def active_connections(self):
        return self._slots.in_use

    @property
    def bound_address(self):
        sock = self._sock
        if sock is None:
            return None
        try:
            return sock.getsockname()
        except OSError:
            return None

    def start(self):
        self.thread.start()

    def cancel(self):
        with self._claim_lock:
            self.cancelled.set()

    def claim_wake(self, name, ip):
        """Record the first allowed join attempt. False if cancelled/already woken."""
        with self._claim_lock:
            if self.cancelled.is_set() or self._woken.is_set():
                return False
            self._waker = (name, ip)
            self._woken.set()
            return True

    # --- the cycle thread --------------------------------------------

    def _run(self):
        try:
            while not self._done():
                sock = self._bind()
                if sock is None:
                    break
                self._serve(sock)
                self._close_socket()
        except Exception:
            logger.exception("Auto-start listener failed")
        finally:
            self._close_socket()
            if self._triage is not None:
                # After a wake (or a start from elsewhere) "starting" is the
                # truth; when the monitor shuts down, "busy".
                closing = self._manager.closed and not self._woken.is_set()
                kick = None if closing else self._last_config_kick()
                self._triage.close(kick or BUSY_MESSAGE)
            self.released.set()
        # The port is released before the start request: the real server binds
        # the same port seconds later.
        if self._woken.is_set():
            self._manager._wake_server(self, *self._waker)

    def _done(self):
        return self.cancelled.is_set() or self._woken.is_set()

    def _bind(self):
        previous, self._previous = self._previous, None
        if previous is not None:
            previous.released.wait(self._options.release_timeout)
        failures = 0
        while not self.cancelled.is_set():
            host, port = self._manager.listen_address()
            try:
                sock = self._open(host, port)
            except (OSError, RuntimeError) as e:
                failures += 1
                self.bind_failures = failures
                self.bind_error = (
                    f"Cannot listen on game port {port}: "
                    f"{getattr(e, 'strerror', None) or e}"
                )
                delay = self._retry_delay(failures, port, e)
                if self.cancelled.wait(delay):
                    return None
                continue
            self._sock = sock
            self.bind_error = None
            if self.cancelled.is_set():
                return None
            logger.info(
                f"Sleeping on {host or '*'}:{port}: a join attempt starts the server"
            )
            return sock
        return None

    def _open(self, host, port):
        """Bind the game port; the triage is created with it (lazily), so a
        failure to create either is retried like a busy port."""
        sock = create_listener(host, port)
        try:
            if self._triage is None:
                self._triage = _Triage(
                    self, self._options.triage_capacity, self._options.triage_timeout
                )
        except BaseException:
            _close_quietly(sock)
            raise
        return sock

    def _last_config_kick(self):
        config = self._config
        return config.kick_message if config is not None else None

    def _retry_delay(self, failures, port, error):
        attempts = self._options.bind_attempts
        if failures < attempts:
            logger.warning(f"Game port {port} is not available yet ({error}); retrying")
            return self._options.retry_delay
        if failures == attempts:
            logger.error(
                f"Could not listen on game port {port} after {attempts} attempts: {error}. "
                "Is another server (or program) using it? Retrying every "
                f"{self._options.slow_retry_delay:g}s while the server is stopped."
            )
        return self._options.slow_retry_delay

    def _serve(self, sock):
        sock.settimeout(self._options.accept_timeout)
        config = self._config = self._manager.sleeping_config()
        while not self._done():
            try:
                conn, address = sock.accept()
            except TimeoutError:
                continue
            except OSError as e:
                if self._done() or not self._survive_accept_error(e):
                    break
                continue
            self._dispatch(conn, address, config)
        if self._woken.is_set() and not self.cancelled.is_set():
            self._drain_backlog(sock, config)

    def _survive_accept_error(self, error):
        """Keep the port bound through per-client and resource errors; only a
        broken listening socket is closed and bound again (False)."""
        action = accept_error_action(error)
        if action == "retry":
            logger.debug(f"A client connection failed during accept: {error}")
            return True
        if action == "backoff":
            logger.warning(
                f"Auto-start listener is out of resources ({error}); backing off"
            )
            self.cancelled.wait(0.5)
            return True
        logger.warning(f"Auto-start listener accept failed ({error}); re-binding")
        self.cancelled.wait(0.5)
        return False

    def _drain_backlog(self, sock, config):
        """Serve connections already queued when the wake came (friends joining
        together) so they get the "starting" message instead of a reset when
        the listening socket closes. Non-blocking: the port is released next."""
        try:
            sock.setblocking(False)
            for _ in range(self._options.max_connections):
                conn, address = sock.accept()
                conn.setblocking(True)
                self._dispatch(conn, address, config)
        except OSError:  # BlockingIOError: the backlog is empty
            pass

    def _dispatch(self, conn, address, config):
        if self.cancelled.is_set():
            _close_quietly(conn)
            return
        ip = normalize_client_ip(address[0])
        key = client_key(ip)
        entry = self._slots.admit(conn, key)
        if entry is None:
            self._triage.add(conn, ip, key, config)  # never blocks
            return
        self._start_handler(entry, ip, config)

    def retry_login_at(self, key):
        return self._slots.retry_at(key)

    def admit_login(self, conn, ip, key, config, request):
        """Called by the triage for a complete login with no slot: evict a
        non-login connection for it. False when every slot holds a login."""
        entry = self._slots.admit(conn, key, phase="login")
        if entry is None:
            return False
        conn.setblocking(True)
        self._start_handler(entry, ip, config, request, "login")
        return True

    def _start_handler(self, entry, ip, config, request=None, kind=None):
        worker = threading.Thread(
            target=self._handle,
            args=(entry, ip, config, request, kind),
            name="minecraft-autostart-conn",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:
            entry.close()
            self._slots.release(entry)

    def _handle(self, entry, ip, config, request=None, kind=None):
        outcome = None
        try:
            if request is None:
                request, kind = read_request(
                    entry.conn,
                    self._options.request_timeout,
                    self.requests.classify,
                    receive=entry.receive,
                )
            if kind is None:
                logger.debug(f"Incomplete sleeping-server request from {ip} dropped")
            elif not self._slots.promote(entry, kind):
                # Evicted just as its request completed: a word, not a reset.
                self._reply_busy(entry.conn, kind, request, config)
            else:
                outcome = self._serve_request(entry, ip, config, request, kind)
        except Exception:
            logger.exception(f"Sleeping-server connection from {ip} failed")
        finally:
            entry.close()
            self._slots.release(entry)
        if outcome is None:
            return
        logger.debug(f"Sleeping-server connection from {ip}: {outcome}")
        if kind == "refused" and outcome.player_name:
            # Refused before any access check: an invalid player name.
            self._manager.refusals.note(outcome.player_name, ip, "invalid name")
        if outcome.wake and not self.claim_wake(outcome.player_name, ip):
            logger.debug(
                f"{safe_for_log(outcome.player_name)} ({ip}) joined while the "
                "server is already starting"
            )

    def _reply_busy(self, conn, kind, request, config):
        try:
            reply = self.requests.busy_reply(kind, request, config)
            if reply:
                conn.settimeout(self._options.status_timeout)
                conn.sendall(reply)
        except (OSError, ValueError):
            pass

    def _serve_request(self, entry, ip, config, request, kind):
        # A ping gets little time (a client that never sends the Ping must
        # not hold its slot); a login gets the full connection timeout, and
        # only an allowed one is protected from eviction meanwhile.
        if kind in ("login", "refused"):
            timeout = self._options.connection_timeout
        else:
            timeout = self._options.status_timeout

        def is_allowed(name, client_ip):
            allowed, message = self._manager.is_allowed(name, client_ip)
            self._slots.promote(entry, ALLOWED if allowed else "refused")
            return allowed, message

        entry.conn.settimeout(timeout)
        replay = _ReplaySocket(entry.conn, request)
        return self._manager.serve_connection(
            replay, ip, config, is_allowed=is_allowed, timeout=timeout
        )

    def _close_socket(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            _close_quietly(sock)
            logger.debug("Auto-start listener released the game port")


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class AutoStartManager:
    """Listens on the game port while STOPPED; a join attempt starts the server.

    Subscriptions: SERVER_STOPPED -> listen(); SERVER_STARTED and
    LIFECYCLE_CHANGED(STARTING) -> stop_listening(). close() unsubscribes.
    """

    def __init__(
        self,
        controller,
        bus: EventBus,
        *,
        options: ListenerOptions | None = None,
        connection_handler: Callable | None = None,
        access_policy: AccessPolicy | None = None,
        requests=None,
    ):
        self.controller = controller
        self.bus = bus
        self.options = options or ListenerOptions()
        self._connection_handler = connection_handler
        self._access_policy = access_policy or AccessPolicy()
        # Recognises first requests and builds busy replies (tests use lines).
        self.requests = requests or MinecraftRequests()
        self.refusals = _RefusalLog()
        self._lock = threading.Lock()
        self._cycle: _ListenCycle | None = None
        self._last_cycle: _ListenCycle | None = None
        self._cycle_count = 0
        self._disarms = 0
        self._closed = False
        self._subscribed = False
        self._subscribe()

    # --- subscriptions -------------------------------------------------

    def _subscribe(self):
        if self._subscribed:
            return
        self.bus.subscribe(Event.SERVER_STARTED, self._on_server_started)
        self.bus.subscribe(Event.SERVER_STOPPED, self._on_server_stopped)
        self.bus.subscribe(Event.LIFECYCLE_CHANGED, self._on_lifecycle_changed)
        self._subscribed = True

    def _unsubscribe(self):
        if not self._subscribed:
            return
        self.bus.unsubscribe(Event.SERVER_STARTED, self._on_server_started)
        self.bus.unsubscribe(Event.SERVER_STOPPED, self._on_server_stopped)
        self.bus.unsubscribe(Event.LIFECYCLE_CHANGED, self._on_lifecycle_changed)
        self._subscribed = False

    def _on_server_started(self, data=None):
        self.stop_listening()

    def _on_server_stopped(self, data=None):
        self.listen()

    def _on_lifecycle_changed(self, data=None):
        if (data or {}).get("state") == LifecycleState.STARTING:
            # Release the game port before the server process is launched.
            self.stop_listening()

    # --- public API ----------------------------------------------------

    def listen(self, data=None) -> bool:
        """Arm the sleeping server if the lifecycle is STOPPED. Idempotent.

        Never blocks or sleeps on the calling thread (binding and its retries
        happen on the cycle thread). Returns True when a cycle is armed.
        """
        with self._lock:
            if self._closed:
                return False
            if self._cycle is not None and self._cycle.active:
                return True
            disarms = self._disarms
        if not self._server_is_stopped():
            return False
        with self._lock:
            if self._closed or self._disarms != disarms:
                return False  # a stop_listening() raced with us; it wins
            if self._cycle is not None and self._cycle.active:
                return True
            self._cycle_count += 1
            cycle = _ListenCycle(self, self._cycle_count, previous=self._last_cycle)
            self._cycle = self._last_cycle = cycle
            cycle.start()
        return True

    def stop_listening(self, data=None) -> bool:
        """Disarm. Returns once the game port is released (True), or False if
        the listener did not let go within ``options.release_timeout``."""
        with self._lock:
            self._disarms += 1
            cycle, self._cycle = self._cycle, None
        if cycle is None:
            return True
        cycle.cancel()
        if threading.current_thread() is cycle.thread:
            return cycle.released.is_set()
        if cycle.released.wait(self.options.release_timeout):
            return True
        logger.error("Auto-start listener did not release the game port in time")
        return False

    def close(self):
        """Stop listening and unsubscribe from lifecycle events. Idempotent."""
        with self._lock:
            already_closed = self._closed
            self._closed = True
        if not already_closed:
            self._unsubscribe()
        self.stop_listening()

    @property
    def is_listening(self) -> bool:
        """True while a cycle has the game port bound."""
        with self._lock:
            cycle = self._cycle
        return cycle is not None and cycle.bound_address is not None

    @property
    def is_armed(self) -> bool:
        """True while a listen cycle is armed (binding, retrying or listening)."""
        with self._lock:
            cycle = self._cycle
        return cycle is not None and cycle.active

    def status(self) -> dict:
        """Listener state for the dashboard: ``armed`` (a cycle wants the game
        port), ``listening`` (it is bound) and ``error`` (why binding fails,
        while armed but not bound; else None)."""
        with self._lock:
            cycle = self._cycle
        armed = cycle is not None and cycle.active
        listening = armed and cycle.bound_address is not None
        error = cycle.bind_error if armed and not listening else None
        return {"armed": armed, "listening": listening, "error": error}

    @property
    def active_connections(self) -> int:
        """Sleeping-server connections being served by the latest cycle."""
        with self._lock:
            cycle = self._last_cycle
        return cycle.active_connections if cycle is not None else 0

    @property
    def bound_address(self):
        """getsockname() of the listening socket, or None."""
        with self._lock:
            cycle = self._cycle
        return cycle.bound_address if cycle is not None else None

    # --- used by the cycles ------------------------------------------

    def listen_address(self):
        port = settings.serverPort or DEFAULT_GAME_PORT
        return settings.listenerBindHost or "", int(port)

    def sleeping_config(self):
        """The sleeping-server presentation for one listen cycle."""
        props = settings.read_server_properties()
        try:
            max_players = int(
                str(props.get("max-players", DEFAULT_MAX_PLAYERS)).strip()
            )
        except ValueError:
            max_players = DEFAULT_MAX_PLAYERS
        return sleeping.SleepingServerConfig(
            motd=str(settings.sleepingMotd),
            version_name=str(settings.sleepingVersionName),
            protocol_version=None,
            max_players=max_players,
            favicon=self._favicon(),
            kick_message=str(settings.startingMessage),
        )

    @staticmethod
    def _favicon():
        if not settings.serverDir:
            return None
        try:
            return server_files.read_server_icon(settings.serverDir)
        except Exception:
            logger.debug("Could not read server-icon.png", exc_info=True)
            return None

    def serve_connection(self, conn, ip, config, is_allowed=None, timeout=None):
        handler = self._connection_handler or sleeping.handle_connection
        return handler(
            conn,
            ip,
            config,
            is_allowed or self.is_allowed,
            timeout=timeout or self.options.connection_timeout,
        )

    def is_allowed(self, name, ip):
        """Access check for a join attempt; fails open (the server enforces its own)."""
        try:
            allowed, message = self._access_policy.check(name, ip)
        except Exception:
            logger.exception("Access check failed; allowing the join attempt")
            return True, None
        if not allowed:
            reason = (message or "access denied").splitlines()[0]
            self.refusals.note(name, ip, reason)
        return allowed, message

    @property
    def closed(self):
        with self._lock:
            return self._closed

    def _wake_server(self, cycle, name, ip):
        with self._lock:
            if self._closed:
                return
        logger.info(
            f"{safe_for_log(name)} ({ip}) is trying to join: "
            "starting the Minecraft server"
        )
        try:
            started = bool(self.controller.start_server())
        except Exception:
            logger.exception("Could not request a server start")
            started = False
        if started:
            return
        with self._lock:
            current = self._cycle is cycle
        if current:
            # Rejected (e.g. stop cooldown): stay reachable instead of leaving
            # the port unbound until some later SERVER_STOPPED.
            logger.warning(
                "The server start was not accepted; sleeping on the game port again"
            )
            self.listen()

    def _server_is_stopped(self):
        controller = self.controller
        if controller is None:
            return False
        try:
            return controller.state == LifecycleState.STOPPED
        except Exception:
            logger.exception("Could not read the lifecycle state")
            return False
