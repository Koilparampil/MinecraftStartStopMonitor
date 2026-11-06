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

import contextlib
import errno
import ipaddress
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from src.events import Event, EventBus
from src.lifecycle.messages import LifecycleState
from src.minecraft import server_files, sleeping
from src.settings import DEFAULT_GAME_PORT, settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_PLAYERS = 20
WHITELIST_MESSAGE = "You are not white-listed on this server!"
BANNED_MESSAGE = "You are banned from this server."
BANNED_IP_MESSAGE = "Your IP address is banned from this server."
_BAN_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"
BUSY_MESSAGE = "Too many connections, try again in a moment."
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
    eviction_grace: float = 0.25  # younger pending connections are not evicted
    max_connections: int = 64  # concurrent connections being served
    max_connections_per_client: int = 4  # per IPv4 address / IPv6 /56
    max_refusals: int = 8  # concurrent "too many connections" replies
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


class _Admission:
    """One admitted connection. It is pending until its first request has
    arrived; only pending connections can be evicted."""

    def __init__(self, conn, key, admitted_at):
        self.conn = conn
        self.key = key
        self.admitted_at = admitted_at
        self.established = False
        self.evicted = False
        self._lock = threading.Lock()
        self._closed = False

    def abort(self):
        """Evicted: wake the handler's blocked read so it gives up at once."""
        with self._lock:
            if not self._closed:
                with contextlib.suppress(OSError):
                    self.conn.shutdown(socket.SHUT_RDWR)

    def close(self):
        with self._lock:
            self._closed = True
            _close_quietly(self.conn)


class ConnectionSlots:
    """Admission control: a global cap and a per-client cap.

    When a cap is reached, the oldest connection that has not sent its first
    request yet (and is older than *grace*) is evicted to make room, so idle
    or trickling sockets cannot lock players out. Connections whose request
    arrived are never evicted; when only those remain, admit() returns None.
    """

    def __init__(self, total, per_client, grace=0.0, clock=time.monotonic):
        self._total = total
        self._per_client = per_client
        self._grace = grace
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: list[_Admission] = []  # oldest first

    @property
    def in_use(self):
        with self._lock:
            return len(self._entries)

    def admit(self, conn, key):
        """A slot for *conn* (evicting a pending one if needed), or None."""
        now = self._clock()
        with self._lock:
            same_client = [entry for entry in self._entries if entry.key == key]
            if len(same_client) >= self._per_client:
                full = same_client
            elif len(self._entries) >= self._total:
                full = self._entries
            else:
                full = None
            victim = self._oldest_pending(full, now) if full is not None else None
            if full is not None and victim is None:
                return None  # everyone has sent a request: no one to evict
            if victim is not None:
                victim.evicted = True
                self._entries.remove(victim)
            entry = _Admission(conn, key, now)
            self._entries.append(entry)
        if victim is not None:
            victim.abort()
        return entry

    def _oldest_pending(self, entries, now):
        for entry in entries:
            if not entry.established and now - entry.admitted_at >= self._grace:
                return entry
        return None

    def promote(self, entry):
        """The request arrived: never evict it. False if it already was."""
        with self._lock:
            if entry.evicted:
                return False
            entry.established = True
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


def minecraft_request_complete(data):
    """True once *data* holds a whole first request: a legacy ping (0xFE), or
    the handshake frame plus the frame after it (Status Request or Login
    Start), which every client sends right away. ValueError for garbage."""
    if not data:
        return False
    if data[0] == 0xFE:
        return True
    offset = 0
    for _ in range(2):
        found = _varint_at(data, offset)
        if found is None:
            return False
        length, offset = found
        if length > sleeping.FRAME_LIMIT:
            raise ValueError("frame too long")
        offset += length
        if offset > len(data):
            return False
    return True


def read_request(conn, timeout, is_complete):
    """Read the client's first request within *timeout* seconds in total.

    Returns the bytes read (they are replayed to the protocol handler), or
    None if the request did not arrive complete in time, the client closed,
    or it is not a valid request."""
    deadline = time.monotonic() + timeout
    data = b""
    while len(data) <= _REQUEST_LIMIT:
        try:
            if is_complete(data):
                return data
        except ValueError:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            conn.settimeout(remaining)
            chunk = conn.recv(4096)
        except OSError:  # timeout, reset, or shut down by an eviction
            return None
        if not chunk:
            return None
        data += chunk
    return None


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


def _busy(_name, _ip):
    return False, BUSY_MESSAGE


def _close_quietly(sock):
    with contextlib.suppress(OSError):
        sock.close()


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
        )
        self._refusals = threading.BoundedSemaphore(max(1, self._options.max_refusals))
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
                sock = create_listener(host, port)
            except OSError as e:
                failures += 1
                self.bind_failures = failures
                self.bind_error = (
                    f"Cannot listen on game port {port}: {e.strerror or e}"
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
        config = self._manager.sleeping_config()
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
        entry = self._slots.admit(conn, client_key(ip))
        if entry is None:
            self._refuse(conn, ip, config)
            return
        worker = threading.Thread(
            target=self._handle,
            args=(entry, ip, config),
            name="minecraft-autostart-conn",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:
            entry.close()
            self._slots.release(entry)

    def _refuse(self, conn, ip, config):
        """Every slot holds a request in progress: tell a joining player to
        retry (a few at a time), or just drop the connection."""
        if not self._refusals.acquire(blocking=False):
            logger.debug(f"Connection limit reached ({ip}); dropping a connection")
            _close_quietly(conn)
            return
        worker = threading.Thread(
            target=self._serve_refusal,
            args=(conn, ip, config),
            name="minecraft-autostart-busy",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:
            self._refusals.release()
            _close_quietly(conn)

    def _serve_refusal(self, conn, ip, config):
        try:
            self._manager.serve_connection(
                conn, ip, config, is_allowed=_busy, timeout=self._options.request_timeout
            )
        except Exception:
            logger.debug(f"Busy reply to {ip} failed", exc_info=True)
        finally:
            _close_quietly(conn)
            self._refusals.release()

    def _handle(self, entry, ip, config):
        outcome = None
        try:
            request = read_request(
                entry.conn, self._options.request_timeout, self._manager.request_complete
            )
            if request is None or not self._slots.promote(entry):
                logger.debug(f"Incomplete sleeping-server request from {ip} dropped")
            else:
                entry.conn.settimeout(self._options.connection_timeout)
                replay = _ReplaySocket(entry.conn, request)
                outcome = self._manager.serve_connection(replay, ip, config)
        except Exception:
            logger.exception(f"Sleeping-server connection from {ip} failed")
        finally:
            entry.close()
            self._slots.release(entry)
        if outcome is None:
            return
        logger.debug(f"Sleeping-server connection from {ip}: {outcome}")
        if outcome.wake and not self.claim_wake(outcome.player_name, ip):
            logger.debug(
                f"{outcome.player_name} ({ip}) joined while the server is already starting"
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
        request_complete: Callable[[bytes], bool] | None = None,
    ):
        self.controller = controller
        self.bus = bus
        self.options = options or ListenerOptions()
        self._connection_handler = connection_handler
        self._access_policy = access_policy or AccessPolicy()
        # When a connection's first request is complete (tests use lines).
        self.request_complete = request_complete or minecraft_request_complete
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
            logger.info(f"Not starting the server for {name} ({ip}): {reason}")
        return allowed, message

    def _wake_server(self, cycle, name, ip):
        with self._lock:
            if self._closed:
                return
        logger.info(f"{name} ({ip}) is trying to join: starting the Minecraft server")
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
