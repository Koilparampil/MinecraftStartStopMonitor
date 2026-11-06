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

"""Flask web dashboard: authenticated admin UI, JSON API and live SSE stream.

The dashboard never derives lifecycle state itself. Every response and every
Server-Sent Event carries one committed snapshot from
``controller.get_snapshot()``; committed bus events trigger a re-read that is
broadcast under one lock, so clients always receive snapshots in revision
order (see docs/ARCHITECTURE.md, "WebServer").
"""

import contextlib
import errno
import functools
import hmac
import io
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler

from src.auth import (
    LoginAttemptTracker,
    SessionStore,
    User,
    credential_epoch,
    safe_next_url,
    split_user_id,
    verify_credentials,
)
from src.events import Event
from src.lifecycle.messages import ActionResult, LifecycleState
from src.minecraft import server_files
from src.settings import settings

logger = logging.getLogger(__name__)

PLAYER_NAME_RE = re.compile(r"[A-Za-z0-9_.*-]{1,32}")
MAX_COMMAND_LENGTH = 256
MAX_REASON_LENGTH = 256
CONSOLE_SOURCES = ("log", "launch")
DEFAULT_CONSOLE_LINES = 200
MAX_CONSOLE_LINES = 1000
INFO_CACHE_SECONDS = 30.0
SSE_KEEPALIVE_SECONDS = 15.0
SSE_RETRY_MS = 3000
SSE_QUEUE_SIZE = 32
MAX_SSE_CLIENTS = 64
AVATAR_ORIGIN = "https://mc-heads.net"
SESSION_STORE_FILE = "web-sessions.json"  # remember-me token hashes (0600)
ABSOLUTE_SESSION_SECONDS = 12 * 3600  # however active, a session ends after this
REMEMBER_SECONDS = 7 * 86400
MAX_REQUEST_BYTES = 16 * 1024  # dashboard requests are small forms
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
RCON_STATUSES = ("disabled", "ok", "auth_failed", "unreachable", "unknown")

# Committed notifications that change what the dashboard shows. PLAYER_* are
# deliberately absent: every roster change is followed by SERVER_STATUS.
COMMITTED_EVENTS = (
    Event.SERVER_STARTED,
    Event.SERVER_STOPPED,
    Event.SERVER_STATUS,
    Event.LIFECYCLE_CHANGED,
    Event.BAN_ADDED,
    Event.BAN_REMOVED,
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FORMATTING_CODE_RE = re.compile("§.?")


@dataclass(frozen=True)
class _StreamGrant:
    """Who opened a live stream and until when it may run."""

    owner: str | None  # the session id (logout ends its streams)
    epoch: str  # credential epoch at open time


@dataclass(frozen=True)
class _ControlAction:
    method: str
    started: str
    failed: str


CONTROL_ACTIONS = {
    "startServer": _ControlAction(
        "start_server", "Starting the server…", "Could not start the server"
    ),
    "stopServer": _ControlAction(
        "stop_server", "Stopping the server…", "Could not stop the server"
    ),
    "restartServer": _ControlAction(
        "restart_server", "Restarting the server…", "Could not restart the server"
    ),
}


@dataclass(frozen=True)
class _PlayerAction:
    method: str
    takes_reason: bool
    verb: str
    succeeded: str
    failed: str


PLAYER_ACTIONS = {
    "kick": _PlayerAction(
        "kick_player", True, "Kick", "Kicked {}", "Could not kick {}"
    ),
    "ban": _PlayerAction("ban_player", True, "Ban", "Banned {}", "Could not ban {}"),
    "unban": _PlayerAction(
        "unban_player", False, "Unban", "Unbanned {}", "Could not unban {}"
    ),
}

_STATUS_LABELS = {
    LifecycleState.STOPPED: "OFFLINE",
    LifecycleState.STARTING: "STARTING",
    LifecycleState.STOPPING: "STOPPING",
}


def status_label(snapshot: dict) -> str:
    """OFFLINE / STARTING / LOADING / ONLINE / STOPPING for a snapshot.

    STOPPED with a pending restart is the instant between a restart's stop
    and its start: it reads as STARTING, never as OFFLINE.
    """
    state = snapshot.get("state") or LifecycleState.STOPPED
    if state == LifecycleState.RUNNING:
        return "ONLINE" if snapshot.get("ready") else "LOADING"
    if state == LifecycleState.STOPPED and snapshot.get("restartPending"):
        return "STARTING"
    return _STATUS_LABELS.get(state, "OFFLINE")


def strip_formatting(text) -> str:
    """Remove Minecraft section-sign formatting codes."""
    return _FORMATTING_CODE_RE.sub("", str(text or ""))


def clean_command(raw) -> tuple[str | None, str | None]:
    """``(command, None)`` for a valid console command, else ``(None, error)``.

    One line, at most MAX_COMMAND_LENGTH characters, one leading ``/`` removed.
    """
    text = (raw or "").strip()
    if _CONTROL_CHARS_RE.search(text):
        return None, "Commands must be a single line of text."
    if len(text) > MAX_COMMAND_LENGTH:
        return None, f"Commands are limited to {MAX_COMMAND_LENGTH} characters."
    command = text[1:].strip() if text.startswith("/") else text
    if not command:
        return None, "Enter a command to run."
    return command, None


def clean_reason(raw) -> tuple[str | None, str | None]:
    """``(reason_or_None, None)`` for an optional kick/ban reason, else an error."""
    text = (raw or "").strip()
    if _CONTROL_CHARS_RE.search(text):
        return None, "The reason must be a single line of text."
    if len(text) > MAX_REASON_LENGTH:
        return None, f"The reason is limited to {MAX_REASON_LENGTH} characters."
    return (text or None), None


def _resource_base() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _put_latest(client: queue.Queue, item) -> None:
    """Enqueue without blocking; a full queue drops its oldest message.

    Every message is a complete snapshot, so a slow client only ever skips
    intermediate states and still sees revisions in increasing order.
    """
    try:
        client.put_nowait(item)
    except queue.Full:
        with contextlib.suppress(queue.Empty):
            client.get_nowait()
        with contextlib.suppress(queue.Full):
            client.put_nowait(item)


class _StreamHub:
    """Connected SSE clients, one bounded queue of encoded messages each."""

    def __init__(self, max_clients: int, queue_size: int):
        self._max_clients = max_clients
        self._queue_size = queue_size
        self._clients: dict[queue.Queue, str | None] = {}  # queue -> owner sid
        self._lock = threading.Lock()
        self._closed = threading.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def __len__(self) -> int:
        with self._lock:
            return len(self._clients)

    def add(self, owner: str | None = None) -> queue.Queue | None:
        with self._lock:
            if self._closed.is_set() or len(self._clients) >= self._max_clients:
                return None
            client = queue.Queue(maxsize=self._queue_size)
            self._clients[client] = owner
            return client

    def remove(self, client: queue.Queue) -> None:
        with self._lock:
            self._clients.pop(client, None)

    def close_owner(self, owner: str) -> int:
        """End the streams opened by one session (logout). Returns how many."""
        with self._lock:
            mine = [client for client, sid in self._clients.items() if sid == owner]
            for client in mine:
                _put_latest(client, None)
        return len(mine)

    def broadcast(self, message: str) -> None:
        with self._lock:
            for client in self._clients:
                _put_latest(client, message)

    def end_all(self) -> None:
        """End every open stream but keep accepting new ones."""
        with self._lock:
            for client in self._clients:
                _put_latest(client, None)

    def close(self) -> None:
        """End every open stream; refuse new ones."""
        with self._lock:
            self._closed.set()
            for client in self._clients:
                _put_latest(client, None)


# ----------------------------------------------------------------------
# Bounded HTTP server (werkzeug's threaded server, hardened)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ServerLimits:
    """Bounds that keep one client from exhausting threads and descriptors."""

    max_connections: int = 128  # all open connections, streams included
    max_connections_per_ip: int = 24  # per address (per /64 for IPv6)
    request_read_timeout: float = 15.0  # request line + headers + body, total
    io_timeout: float = 30.0  # any single socket read or write


class _DeadlineSocketIO(socket.SocketIO):
    """Socket reader with one deadline for the whole request, however slowly
    the bytes trickle in (a per-recv timeout alone never fires for a client
    that sends one byte every few seconds)."""

    def __init__(self, sock, deadline: float, io_timeout: float):
        super().__init__(sock, "rb")
        self._deadline = deadline
        self._io_timeout = io_timeout
        self.responding = False  # set once the response has started

    def readinto(self, buffer):
        if self.responding:
            # Only werkzeug's post-response drain reads now. End it cleanly
            # (EOF) so it closes its selector and the response iterator at
            # once instead of leaking them until a garbage collection.
            return 0
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the request was not received in time")
        self._sock.settimeout(min(remaining, self._io_timeout))
        try:
            return super().readinto(buffer)
        finally:
            self._sock.settimeout(self._io_timeout)


class _GuardedRequestHandler(WSGIRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        limits = self.server.limits
        self.connection.settimeout(limits.io_timeout)
        self.rfile.close()  # replaced by a reader with a request deadline
        deadline = time.monotonic() + limits.request_read_timeout
        self.rfile = io.BufferedReader(
            _DeadlineSocketIO(self.connection, deadline, limits.io_timeout)
        )

    def send_response(self, code, message=None):
        # From here on nothing more is read from the client, and the
        # connection is no longer a candidate for eviction.
        if isinstance(getattr(self.rfile, "raw", None), _DeadlineSocketIO):
            self.rfile.raw.responding = True
        self.server.mark_responding(self.request)
        super().send_response(code, message)

    def log_error(self, format, *args):  # noqa: A002 - stdlib signature
        if format.startswith("Request timed out"):
            logger.debug("Dropped a slow HTTP client %s", self.client_address[0])
            return
        super().log_error(format, *args)


@dataclass
class _Connection:
    peer: str  # per-address key (see _peer_key)
    accepted_at: float
    waiting: bool = True  # no response started yet (still reading or working)


class _GuardedWSGIServer(ThreadedWSGIServer):
    """Caps open connections globally and per client address.

    Excess connections are closed as soon as they are accepted, except that
    at the global cap the oldest connection that has not started its response
    is dropped to make room: a flood of slow headers or slow bodies, from many
    addresses or through a trusted proxy, cannot keep a real request (which
    completes in milliseconds) out. Live streams are never dropped.
    """

    daemon_threads = True
    block_on_close = False

    def __init__(self, host, port, app, limits: ServerLimits):
        self.limits = limits
        self._connections: dict[socket.socket, _Connection] = {}
        self._connections_lock = threading.Lock()
        self._warned_at: dict[str, float] = {}
        super().__init__(host, port, app, handler=_GuardedRequestHandler)

    def open_connections(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    def verify_request(self, request, client_address):
        address = str(client_address[0]) if client_address else ""
        peer = _peer_key(address)
        proxy = _in_networks(address, _trusted_networks())
        with self._connections_lock:
            same_peer = sum(1 for c in self._connections.values() if c.peer == peer)
            if not proxy and same_peer >= self.limits.max_connections_per_ip:
                self._warn(
                    "peer", "Refusing connections from %s: too many open", address
                )
                return False
            if (
                len(self._connections) >= self.limits.max_connections
                and not self._evict()
            ):
                self._warn(
                    "full",
                    "Refusing a connection: %s already open",
                    len(self._connections),
                )
                return False
            self._connections[request] = _Connection(peer, time.monotonic())
        return True

    def _evict(self) -> bool:
        """Drop the oldest connection whose response has not started."""
        waiting = [
            (c.accepted_at, s) for s, c in self._connections.items() if c.waiting
        ]
        if not waiting:
            return False
        _accepted, victim = min(waiting, key=lambda item: item[0])
        del self._connections[victim]
        with contextlib.suppress(OSError):
            victim.shutdown(socket.SHUT_RDWR)  # its blocked read returns at once
        return True

    def mark_responding(self, request) -> None:
        with self._connections_lock:
            connection = self._connections.get(request)
            if connection is not None:
                connection.waiting = False

    def shutdown_request(self, request):
        with self._connections_lock:
            self._connections.pop(request, None)
        super().shutdown_request(request)

    def get_request(self):
        try:
            return super().get_request()
        except OSError as error:
            if error.errno in (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM):
                self._warn("accept", "Web server cannot accept connections: %s", error)
                time.sleep(0.1)  # don't spin while the process is out of descriptors
            raise

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, OSError):
            logger.debug("Connection from %s ended: %s", client_address, error)
        else:
            logger.exception("Error while serving %s", client_address)

    def _warn(self, reason: str, message: str, *args) -> None:
        now = time.monotonic()
        if now - self._warned_at.get(reason, -3600.0) >= 60:
            self._warned_at[reason] = now
            logger.warning(message, *args)


def make_server(host, port, app, limits: ServerLimits | None = None):
    """The dashboard's threaded server with connection and read bounds."""
    return _GuardedWSGIServer(host, port, app, limits or ServerLimits())


# ----------------------------------------------------------------------
# Reverse proxies (web.trustedProxies)
# ----------------------------------------------------------------------


@functools.lru_cache(maxsize=8)
def _parse_networks(entries: tuple[str, ...]) -> tuple:
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry.strip(), strict=False))
        except ValueError:
            logger.warning("Ignoring invalid web.trustedProxies entry %r", entry)
    return tuple(networks)


def _trusted_networks() -> tuple:
    raw = settings.get("trustedProxies", []) or []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    return _parse_networks(tuple(str(item) for item in raw if str(item).strip()))


def _ip(text):
    try:
        address = ipaddress.ip_address(str(text).strip().split("%")[0])
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _in_networks(host, networks) -> bool:
    address = _ip(host) if networks else None
    return address is not None and any(
        address.version == network.version and address in network
        for network in networks
    )


def _peer_key(host: str) -> str:
    """Connection-cap key: the address, or its /64 for IPv6."""
    address = _ip(host)
    if address is None:
        return host
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def _last_value(header) -> str:
    return str(header or "").split(",")[-1].strip()


_HOST_RE = re.compile(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9A-Fa-f:.]+\](:\d{1,5})?")


class _TrustedProxyMiddleware:
    """Honours X-Forwarded-For/-Proto/-Host/-Port only when the TCP peer is a
    trusted proxy, so the lockout, the rate limit, the logs and the Origin
    check see the real client and the public URL."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        networks = _trusted_networks()
        if networks and _in_networks(environ.get("REMOTE_ADDR", ""), networks):
            _apply_forwarded_headers(environ, networks)
        return self.app(environ, start_response)


def _apply_forwarded_headers(environ, networks) -> None:
    hops = [hop.strip() for hop in environ.get("HTTP_X_FORWARDED_FOR", "").split(",")]
    client = None
    for hop in reversed([hop for hop in hops if hop]):
        if _ip(hop) is None:
            break
        client = hop  # the nearest address that is not one of our proxies
        if not _in_networks(hop, networks):
            break
    environ["mcmonitor.proxied"] = True
    if client:
        environ["REMOTE_ADDR"] = client
    proto = _last_value(environ.get("HTTP_X_FORWARDED_PROTO")).lower()
    if proto in ("http", "https"):
        environ["wsgi.url_scheme"] = proto
    host = _last_value(environ.get("HTTP_X_FORWARDED_HOST"))
    if host and _HOST_RE.fullmatch(host):
        environ["HTTP_HOST"] = host
    port = _last_value(environ.get("HTTP_X_FORWARDED_PORT"))
    current = environ.get("HTTP_HOST", "")
    if port.isdigit() and current and not current.endswith("]") and ":" not in current:
        default = "443" if environ.get("wsgi.url_scheme") == "https" else "80"
        if port != default:
            environ["HTTP_HOST"] = f"{current}:{port}"


def _origin_of(url):
    """(scheme, host, port) of an http(s) URL, or None."""
    try:
        parts = urlsplit(str(url))
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    return scheme, parts.hostname.lower(), port or (443 if scheme == "https" else 80)


def _client_key() -> str:
    """Lockout and rate-limit key: the client address (after trusted-proxy
    resolution), or its /64 for IPv6."""
    return _peer_key(request.remote_addr or "unknown")


def _secure_cookies() -> bool:
    return bool(settings.get("secureCookies", False))


class WebServer:
    """The dashboard: a Flask app plus its committed-snapshot cache and SSE hub."""

    def __init__(self, controller, bus, auto_start_status=None):
        """``auto_start_status``: optional callable returning the sleeping
        listener's ``{"armed": bool, "listening": bool, "error": str|None}``."""
        self.controller = controller
        self.bus = bus
        self.auto_start_status = auto_start_status
        self.sse_keepalive_interval = SSE_KEEPALIVE_SECONDS
        self.info_cache_seconds = INFO_CACHE_SECONDS
        self.server_limits = ServerLimits()
        self.sessions = SessionStore(settings.data_path(SESSION_STORE_FILE))
        self._console_errors: dict[str, int | None] = {}  # path -> errno logged

        self._lock = threading.RLock()  # snapshot cache + SSE broadcast order
        self.snapshot = self._read_snapshot()
        self._streams = _StreamHub(MAX_SSE_CLIENTS, SSE_QUEUE_SIZE)
        self._info_lock = threading.Lock()
        self._info_cache: tuple[float, dict] | None = None
        self._static_versions: dict[str, str] = {}

        self._server = None
        self._server_lock = threading.Lock()
        self._serving_thread: threading.Thread | None = None
        self._shutdown_requested = False
        self.serving = threading.Event()  # set while run() accepts requests
        self.bound_port: int | None = None

        self.app = self._create_app()
        self.csrf = CSRFProtect(self.app)
        self.limiter = self._create_limiter()
        self.login_manager = self._create_login_manager()
        self.login_tracker = LoginAttemptTracker(
            max_attempts=settings.maxLoginAttempts,
            lockout_duration=settings.lockoutDuration,
        )
        self._register_routes()
        self._register_error_handlers()

        self._subscribed = False
        self._subscribe()

    # ------------------------------------------------------------------
    # Flask setup
    # ------------------------------------------------------------------

    def _create_app(self) -> Flask:
        base = _resource_base()
        app = Flask(
            __name__,
            template_folder=os.path.join(base, "templates"),
            static_folder=os.path.join(base, "static"),
        )
        secret = settings.sessionSecretKey
        if not secret:
            logger.warning("No sessionSecretKey configured; sessions end on restart")
            secret = secrets.token_hex(32)
        app.secret_key = secret
        # Cookies are shared by every port of a host: suffix the port so two
        # monitors (or other Flask apps) on one machine keep separate logins.
        # Over HTTPS the __Host- prefix also stops plain-HTTP pages on the
        # same host from planting or overwriting them.
        secure = _secure_cookies()
        prefix = "__Host-" if secure else ""
        suffix = str(settings.webServerPort)
        self.remember_cookie_name = f"{prefix}mcmonitor_remember_{suffix}"
        app.config.update(
            PERMANENT_SESSION_LIFETIME=timedelta(seconds=int(settings.sessionTimeout)),
            SESSION_COOKIE_NAME=f"{prefix}mcmonitor_session_{suffix}",
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Strict",
            SESSION_COOKIE_SECURE=secure,
            # Flask-Login's own remember cookie is unused: remember-me tokens
            # are random, server-side and revocable (SessionStore).
            REMEMBER_COOKIE_NAME=f"mcmonitor_unused_{suffix}",
            # The token lives in the session, which expires server-side; a
            # dashboard left open for hours must still be able to act.
            WTF_CSRF_TIME_LIMIT=None,
            # Same-origin is enforced by _check_origin from Origin or Referer;
            # Flask-WTF's HTTPS-only check would demand a Referer as well.
            WTF_CSRF_SSL_STRICT=False,
            MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        )
        app.wsgi_app = _TrustedProxyMiddleware(app.wsgi_app)
        app.before_request(self._check_origin)  # runs before the CSRF check
        app.jinja_env.globals["static_url"] = self._static_url
        return app

    def _create_limiter(self) -> Limiter:
        # Login is exempt (LoginAttemptTracker owns that throttle: two
        # counters on one request would lock out at an unpredictable count),
        # as are /stream, /healthz, /console and static files.
        return Limiter(
            _client_key,
            app=self.app,
            storage_uri="memory://",
            default_limits=[_default_rate_limit],
            enabled=bool(settings.rateLimitEnabled),
        )

    def _create_login_manager(self) -> LoginManager:
        manager = LoginManager()
        manager.init_app(self.app)
        manager.login_view = "login"
        manager.login_message = None
        manager.user_loader(self._load_user)
        manager.request_loader(self._load_user_from_remember)
        manager.unauthorized_handler(self._unauthorized)
        return manager

    def _credential_epoch(self) -> str:
        return credential_epoch(
            str(settings.webUsername), str(settings.webPassword), self.app.secret_key
        )

    def _load_user(self, user_id):
        """Sessions and remember cookies carry the credential epoch: after a
        password (or username) change they no longer load a user."""
        username, epoch = split_user_id(user_id)
        if username != str(settings.webUsername) or not epoch:
            return None
        if not hmac.compare_digest(epoch, self._credential_epoch()):
            return None
        if not self.sessions.check_session(session.get("sid"), epoch):
            return None  # logged out, expired, or never opened here
        return User(username, epoch)

    def _load_user_from_remember(self, req):
        """Restore a login from a remember-me token into a brand-new session
        (new id, new CSRF secret). Only on safe requests: a state change must
        come from a session that already existed, with its own token."""
        cookie = req.cookies.get(self.remember_cookie_name)
        if not cookie:
            return None
        epoch = self._credential_epoch()
        token_id = self.sessions.redeem_token(cookie, epoch)
        if token_id is None:
            g.drop_remember_cookie = True
            return None
        if req.method not in SAFE_METHODS:
            return None
        user = User(str(settings.webUsername), epoch)
        self._establish_session(user, token_id)
        g.csrf_rotated = True
        logger.info("Restored a remembered login from %s", req.remote_addr)
        return user

    def _establish_session(self, user, token_id=None) -> str:
        """A fresh session: nothing carried over from before the login, so a
        cookie planted beforehand (and its CSRF secret) is worthless."""
        session.clear()
        g.pop("csrf_token", None)
        session.permanent = True
        sid = self.sessions.open_session(
            user.epoch,
            idle=float(settings.sessionTimeout),
            lifetime=max(float(settings.sessionTimeout), ABSOLUTE_SESSION_SECONDS),
            token_id=token_id,
        )
        session["sid"] = sid
        login_user(user)
        generate_csrf()
        return sid

    def _set_remember_cookie(self, response, value: str) -> None:
        response.set_cookie(
            self.remember_cookie_name,
            value,
            max_age=REMEMBER_SECONDS,
            path="/",
            secure=_secure_cookies(),
            httponly=True,
            samesite="Strict",
        )

    def _delete_remember_cookie(self, response) -> None:
        response.delete_cookie(
            self.remember_cookie_name,
            path="/",
            secure=_secure_cookies(),
            httponly=True,
            samesite="Strict",
        )

    def _check_origin(self):
        """Refuse state-changing requests sent from another origin (another
        port or host). Browsers always send Origin on such POSTs; clients
        that send neither Origin nor Referer still need the CSRF token."""
        if request.method in SAFE_METHODS:
            return None
        claimed = request.headers.get("Origin") or request.headers.get("Referer")
        if claimed is None:
            return None
        expected = _origin_of(request.host_url)
        actual = _origin_of(claimed)
        if expected is not None and actual == expected:
            return None
        logger.warning(
            "Refused a cross-origin %s %s from %s (origin %r, expected %s)",
            request.method,
            request.path,
            request.remote_addr,
            claimed[:200],
            request.host_url.rstrip("/"),
        )
        message = (
            "Request blocked: it did not come from this dashboard's own page. "
            "Behind a reverse proxy, list it in web.trustedProxies."
        )
        proxied = request.environ.get("mcmonitor.proxied")
        if proxied and expected and actual and actual[:2] == expected[:2]:
            message = (
                "Request blocked: the reverse proxy did not pass on the public "
                "port. Forward the browser's Host header with its port (nginx: "
                "proxy_set_header Host $http_host;) or send X-Forwarded-Port."
            )
        if request.endpoint == "login":
            return self._render_login(error=message, status=403)
        return jsonify(success=False, message=message), 403

    def _static_url(self, filename: str) -> str:
        """url_for('static') with a cache-busting version (file mtime)."""
        version = self._static_versions.get(filename)
        if version is None:
            path = os.path.join(self.app.static_folder, filename)
            try:
                version = str(int(os.path.getmtime(path)))
            except OSError:
                version = "0"
            self._static_versions[filename] = version
        return url_for("static", filename=filename, v=version)

    def _register_routes(self):
        app, limiter = self.app, self.limiter

        @app.route("/login", methods=["GET", "POST"])
        @limiter.exempt
        def login():
            return self._handle_login()

        @app.route("/logout", methods=["POST"])
        @login_required
        def logout():
            return self._handle_logout()

        @app.route("/")
        @login_required
        def index():
            return self._handle_index()

        @app.route("/action", methods=["POST"])
        @login_required
        def action():
            return self._handle_action()

        @app.route("/kick", methods=["POST"])
        @login_required
        def kick():
            return self._handle_player_action("kick")

        @app.route("/ban", methods=["POST"])
        @login_required
        def ban():
            return self._handle_player_action("ban")

        @app.route("/unban", methods=["POST"])
        @login_required
        def unban():
            return self._handle_player_action("unban")

        @app.route("/banned")
        @login_required
        def banned():
            return self._handle_banned()

        @app.route("/stream")
        @limiter.exempt
        @login_required
        def stream():
            return self._handle_stream()

        @app.route("/console")
        @limiter.exempt
        @login_required
        def console():
            return self._handle_console()

        @app.route("/command", methods=["POST"])
        @login_required
        def command():
            return self._handle_command()

        @app.route("/info")
        @login_required
        def info():
            return self._handle_info()

        @app.route("/csrf")
        @login_required
        def csrf_token():
            return jsonify(success=True, csrfToken=generate_csrf())

        @app.route("/healthz")
        @limiter.exempt
        def healthz():
            return jsonify(ok=True)

    def _register_error_handlers(self):
        self.app.register_error_handler(CSRFError, self._handle_csrf_error)
        self.app.register_error_handler(429, self._handle_rate_limited)
        self.app.register_error_handler(413, self._handle_too_large)
        self.app.after_request(self._after_request)

    # ------------------------------------------------------------------
    # Auth / error responses
    # ------------------------------------------------------------------

    def _unauthorized(self):
        """Pages redirect to the login form; API calls get 401 JSON."""
        if request.endpoint in ("index", "logout") and request.method == "GET":
            return redirect(self._login_url_for_current_page())
        if request.endpoint == "logout":
            # The session had already lapsed: still forget this browser.
            self.sessions.revoke_token(
                self.sessions.redeem_token(
                    request.cookies.get(self.remember_cookie_name),
                    self._credential_epoch(),
                )
            )
            response = redirect(url_for("login"))
            self._delete_remember_cookie(response)
            return response
        return (
            jsonify(
                success=False,
                error="Unauthorized",
                message="Your session has expired. Please log in again.",
            ),
            401,
        )

    @staticmethod
    def _login_url_for_current_page() -> str:
        target = request.path
        if request.query_string:
            target += "?" + request.query_string.decode("utf-8", "replace")
        if target == "/":
            return url_for("login")
        return url_for("login", next=target)

    def _handle_csrf_error(self, error):
        if request.endpoint == "login":
            return self._render_login(
                error="Your login form expired. Please try again.", status=400
            )
        if request.endpoint == "logout":
            # Same-origin was already checked; a stale token must never keep
            # anyone from logging out.
            return self._handle_logout()
        if not current_user.is_authenticated:
            return self._unauthorized()
        logger.warning("CSRF check failed for %s: %s", request.path, error.description)
        return (
            jsonify(
                success=False,
                csrfError=True,  # the page fetches /csrf and retries once
                message="Security token missing or expired. Reload the page and try again.",
            ),
            400,
        )

    @staticmethod
    def _handle_rate_limited(_error):
        return (
            jsonify(success=False, message="Too many requests. Slow down a little."),
            429,
        )

    @staticmethod
    def _handle_too_large(_error):
        return jsonify(success=False, message="Request too large."), 413

    def _after_request(self, response):
        if g.pop("drop_remember_cookie", False):
            self._delete_remember_cookie(response)
        if g.pop("csrf_rotated", False):
            # The page's token belonged to the session that just lapsed.
            response.headers["X-CSRF-Token"] = generate_csrf()
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Content-Security-Policy", _content_security_policy())
        if _secure_cookies():
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.endpoint != "static":
            headers.setdefault("Cache-Control", "no-store")
        return response

    # ------------------------------------------------------------------
    # Committed state
    # ------------------------------------------------------------------

    def _subscribe(self):
        if self._subscribed:
            return
        for event in COMMITTED_EVENTS:
            self.bus.subscribe(event, self._on_committed)
        self._subscribed = True

    def close(self):
        """Unsubscribe from the bus and end open streams. Idempotent."""
        self._streams.close()
        if not self._subscribed:
            return
        for event in COMMITTED_EVENTS:
            self.bus.unsubscribe(event, self._on_committed)
        self._subscribed = False

    def _read_snapshot(self) -> dict:
        snapshot = dict(self.controller.get_snapshot())
        snapshot["revision"] = snapshot.get("revision", 0)
        snapshot["players"] = list(snapshot.get("players") or [])
        snapshot["banned_players"] = list(snapshot.get("banned_players") or [])
        return snapshot

    def _cache_snapshot(self, snapshot: dict) -> dict:
        """Keep the newest snapshot; an equal revision refreshes ban data."""
        if snapshot["revision"] >= self.snapshot.get("revision", 0):
            self.snapshot = snapshot
        return snapshot

    def _refresh_snapshot(self) -> dict:
        with self._lock:
            return self._cache_snapshot(self._read_snapshot())

    def _on_committed(self, _data=None):
        # Read + broadcast under one lock: concurrent notifications (and ban
        # requests) can never emit snapshots out of revision order.
        with self._lock:
            snapshot = self._read_snapshot()
            if snapshot["revision"] < self.snapshot.get("revision", 0):
                return
            self.snapshot = snapshot
            self._streams.broadcast(self._sse_message(self._payload(snapshot)))

    def _payload(self, snapshot: dict) -> dict:
        return {
            "data": dict(snapshot),
            "autoStopDelay": _auto_stop_delay(),
            "serverTime": time.time(),
            # A quick read under the client's small state lock; arrives with
            # every committed snapshot so the page never acts on a stale one.
            "rconStatus": self._rcon_status(),
        }

    def _json_state(self, status: int = 200, **extra):
        """JSON body built around one consistent committed snapshot."""
        body = self._payload(self._refresh_snapshot())
        body["monitor"] = self._monitor_status()
        body.update(extra)
        return jsonify(body), status

    def _monitor_status(self) -> dict:
        """Live state that is not part of the snapshot. Only called from
        request handlers, never under the broadcast lock or on the bus."""
        return {
            "rconStatus": self._rcon_status(),
            "autoStartStatus": self._auto_start_state(),
        }

    def _rcon_status(self) -> str | None:
        try:
            value = getattr(
                getattr(self.controller, "client", None), "rcon_status", None
            )
            if callable(value):
                value = value()
        except Exception:
            logger.debug("rcon_status failed", exc_info=True)
            return None
        return value if value in RCON_STATUSES else None

    def _auto_start_state(self) -> dict | None:
        if self.auto_start_status is None:
            return None
        try:
            raw = self.auto_start_status()
        except Exception:
            logger.debug("auto_start_status failed", exc_info=True)
            return None
        if not isinstance(raw, dict):
            return None
        error = raw.get("error")
        return {
            "armed": bool(raw.get("armed")),
            "listening": bool(raw.get("listening")),
            "error": str(error) if error else None,
        }

    # ------------------------------------------------------------------
    # Login / logout / dashboard
    # ------------------------------------------------------------------

    def _handle_login(self):
        ip = _client_key()  # IPv6 clients by /64: rotating addresses is free
        next_url = self._next_url()
        if request.method == "GET" and current_user.is_authenticated:
            return redirect(next_url)
        if self.login_tracker.is_locked_out(ip):
            return self._render_locked_out(ip)
        if request.method == "POST":
            wait = self.login_tracker.global_wait(ip)
            if wait:
                return self._render_login(
                    error="Too many failed logins on this dashboard. "
                    f"Try again in {wait} seconds.",
                    locked_out=True,
                    lockout_seconds=wait,
                    status=429,
                )
            return self._attempt_login(ip, next_url)
        return self._render_login()

    def _next_url(self) -> str:
        target = request.form.get("next") or request.args.get("next")
        target = safe_next_url(target)
        if urlsplit(target).path in (url_for("login"), url_for("logout")):
            return "/"
        return target

    def _attempt_login(self, ip: str, next_url: str):
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        remember = request.form.get("remember") in ("on", "yes", "1", "true")
        if verify_credentials(
            username, password, settings.webUsername, settings.webPassword
        ):
            user = User(str(settings.webUsername), self._credential_epoch())
            token_id, token = None, None
            if remember:
                token_id, token = self.sessions.issue_token(
                    user.epoch, REMEMBER_SECONDS
                )
            self._establish_session(user, token_id)
            self.login_tracker.record_successful_login(ip)
            logger.info("Web login from %s", request.remote_addr)
            response = redirect(next_url)
            if token:
                self._set_remember_cookie(response, token)
            return response

        self.login_tracker.record_failed_attempt(ip)
        logger.warning(
            "Failed login attempt from %s for user %r", request.remote_addr, username
        )
        remaining = self.login_tracker.get_remaining_attempts(ip)
        if remaining == 0:
            return self._render_locked_out(ip)
        warning = None
        if remaining <= 2:
            plural = "s" if remaining != 1 else ""
            warning = f"{remaining} attempt{plural} left before a temporary lockout."
        return self._render_login(
            error="Invalid username or password.", warning=warning, username=username
        )

    def _render_locked_out(self, ip: str):
        remaining = self.login_tracker.get_lockout_time_remaining(ip) or 0
        return self._render_login(
            error=f"Too many failed attempts. Try again in {remaining} seconds.",
            locked_out=True,
            lockout_seconds=remaining,
            status=429,
        )

    def _render_login(self, status: int = 200, **context):
        context.setdefault("next_url", self._next_url())
        return (
            render_template("login.html", theme=_theme_cookie(), **context),
            status,
        )

    def _handle_logout(self):
        everywhere = request.form.get("scope") == "all"
        logger.info(
            "Web logout%s from %s",
            " of every session" if everywhere else "",
            request.remote_addr,
        )
        sid = session.get("sid")
        self.sessions.revoke_token(self.sessions.close_session(sid))
        presented = request.cookies.get(self.remember_cookie_name)
        self.sessions.revoke_token(
            self.sessions.redeem_token(presented, self._credential_epoch())
        )
        if everywhere:
            self.sessions.close_all()
            self._streams.end_all()
        elif sid:
            self._streams.close_owner(sid)
        logout_user()
        session.clear()
        flash(
            "Signed out everywhere: every session and remembered login has ended."
            if everywhere
            else "You have been signed out.",
            "info",
        )
        response = redirect(url_for("login"))
        self._delete_remember_cookie(response)
        return response

    def _handle_index(self):
        snapshot = self._refresh_snapshot()
        config = _ui_config()
        server = snapshot.get("server") or {}
        monitor = self._monitor_status()
        listener = monitor["autoStartStatus"] or {}
        return render_template(
            "index.html",
            theme=_theme_cookie(),
            snapshot=snapshot,
            state=snapshot.get("state") or LifecycleState.STOPPED,
            server=server,
            motd=strip_formatting(server.get("motd")).strip(),
            status=status_label(snapshot),
            config=config,
            auto_start_blocked=bool(
                config["autoStart"]
                and listener.get("armed")
                and not listener.get("listening")
            ),
            bootstrap={
                "payload": self._payload(snapshot),
                "config": config,
                "monitor": monitor,
            },
            git_hash=settings.get_git_hash(),
            username=current_user.username,
        )

    # ------------------------------------------------------------------
    # Server control and moderation
    # ------------------------------------------------------------------

    def _handle_action(self):
        action = request.form.get("action", "")
        if action == "getStatus":
            return self._json_state(success=True, message="Status refreshed.")
        spec = CONTROL_ACTIONS.get(action)
        if spec is None:
            return jsonify(success=False, message="Unknown action."), 400
        if not settings.controlServerThroughWeb:
            return (
                jsonify(
                    success=False,
                    message="Server control through the web UI is disabled.",
                ),
                403,
            )
        logger.info(
            "Server action %s by %s from %s",
            action,
            current_user.username,
            request.remote_addr,
        )
        result = self._call_controller(spec.method)
        if isinstance(result, ActionResult):  # carries the real reason
            accepted, message = result.success, result.message
        else:  # plain bool from older controllers
            accepted, message = bool(result), ""
        if not message:
            message = spec.started if accepted else self._rejection_guess(spec, result)
        return self._json_state(success=accepted, message=message)

    def _rejection_guess(self, spec, result) -> str:
        if result is None:
            return "Internal error; see the monitor log."
        state = str(self._refresh_snapshot().get("state", "")).lower()
        return f"{spec.failed}: the server is {state or 'busy'}."

    def _handle_player_action(self, kind: str):
        spec = PLAYER_ACTIONS[kind]
        name = request.form.get("player") or ""
        if not PLAYER_NAME_RE.fullmatch(name):
            return (
                jsonify(
                    success=False,
                    message="A valid player name is required "
                    "(1-32 letters, digits or _ . * -).",
                ),
                400,
            )
        args = [name]
        if spec.takes_reason:
            reason, error = clean_reason(request.form.get("reason"))
            if error:
                return jsonify(success=False, message=error), 400
            args.append(reason)
        logger.info(
            "%s %s by %s from %s",
            spec.verb,
            name,
            current_user.username,
            request.remote_addr,
        )
        result = self._call_action(spec.method, *args)
        template = spec.succeeded if result.success else spec.failed
        return self._json_state(
            success=result.success,
            message=result.message or template.format(name),
            output=result.output,
        )

    def _handle_banned(self):
        snapshot = self._refresh_snapshot()
        return jsonify(success=True, banned_players=list(snapshot["banned_players"]))

    def _call_controller(self, method: str, *args, default=None):
        try:
            return getattr(self.controller, method)(*args)
        except Exception:
            logger.exception("Controller call %s failed", method)
            return default

    def _call_action(self, method: str, *args) -> ActionResult:
        result = self._call_controller(method, *args)
        if isinstance(result, ActionResult):
            return result
        if result is None:
            return ActionResult(False, "Internal error; see the monitor log.")
        return ActionResult(bool(result))

    # ------------------------------------------------------------------
    # Console
    # ------------------------------------------------------------------

    def _handle_console(self):
        if not settings.webConsole:
            return jsonify(success=False, message="The web console is disabled."), 403
        source = request.args.get("source", "log")
        if source not in CONSOLE_SOURCES:
            return jsonify(success=False, message="Unknown log source."), 400
        try:
            count = int(request.args.get("lines", DEFAULT_CONSOLE_LINES))
        except ValueError:
            return jsonify(success=False, message="lines must be a number."), 400
        count = min(max(count, 1), MAX_CONSOLE_LINES)
        path = _console_path(source)
        if not (path and os.path.isfile(path)):
            return jsonify(success=True, source=source, lines=[], exists=False)
        try:
            raw = server_files.tail_file(path, count)
        except OSError as error:
            return self._console_unreadable(source, path, error)
        self._console_errors.pop(path, None)
        lines = [_ANSI_RE.sub("", line) for line in raw]
        return jsonify(success=True, source=source, lines=lines, exists=True)

    def _console_unreadable(self, source: str, path: str, error: OSError):
        """A JSON error, logged once per file and error rather than on every
        refresh of the console panel."""
        if self._console_errors.get(path, -1) != error.errno:
            self._console_errors[path] = error.errno
            logger.warning("The web console cannot read %s: %s", path, error)
        name = "logs/latest.log" if source == "log" else "the launch output"
        reason = (error.strerror or str(error)).lower()
        return jsonify(
            success=False,
            source=source,
            lines=[],
            exists=True,
            message=f"Cannot read {name}: {reason}.",
        )

    def _handle_command(self):
        if not (settings.webConsole and settings.controlServerThroughWeb):
            return (
                jsonify(
                    success=False,
                    message="Console commands are disabled.",
                    output=None,
                ),
                403,
            )
        command, error = clean_command(request.form.get("command"))
        if error:
            return jsonify(success=False, message=error, output=None), 400
        logger.info(
            "Console command %r by %s from %s",
            command,
            current_user.username,
            request.remote_addr,
        )
        result = self._call_action("run_console_command", command)
        message = result.message or (
            "Command sent." if result.success else "Command failed."
        )
        return jsonify(success=result.success, message=message, output=result.output)

    # ------------------------------------------------------------------
    # Server info (mods, icon, automation)
    # ------------------------------------------------------------------

    def _handle_info(self):
        return jsonify({**self._get_info(), **self._monitor_status()})

    def _get_info(self) -> dict:
        with self._info_lock:
            cached = self._info_cache
            if cached is not None and time.monotonic() < cached[0]:
                return cached[1]
        info = _build_info()
        with self._info_lock:
            self._info_cache = (time.monotonic() + self.info_cache_seconds, info)
        return info

    # ------------------------------------------------------------------
    # Server-Sent Events
    # ------------------------------------------------------------------

    @staticmethod
    def _sse_message(payload, retry: int | None = None) -> str:
        prefix = f"retry: {retry}\n" if retry else ""
        return f"{prefix}data: {json.dumps(payload)}\n\n"

    def _handle_stream(self):
        grant = _StreamGrant(
            owner=session.get("sid"), epoch=getattr(current_user, "epoch", "")
        )
        with self._lock:
            # The initial snapshot is read and the queue registered under the
            # broadcast lock, so no committed update can slip in between.
            snapshot = self._cache_snapshot(self._read_snapshot())
            client = self._streams.add(grant.owner)
            first = self._payload(snapshot)
            # This request may have restored a remembered login into a new
            # session: hand the page that session's token (only to this client).
            first["csrfToken"] = generate_csrf()
            initial = self._sse_message(first, retry=SSE_RETRY_MS)
        if client is None:
            return jsonify(success=False, message="Live updates unavailable."), 503
        response = Response(
            self._stream_events(client, initial, grant), mimetype="text/event-stream"
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    def _grant_valid(self, grant: "_StreamGrant") -> bool:
        """A stream lives no longer than the session that opened it: it ends
        when that session expires or the credentials change (logout ends it
        at once through close_owner)."""
        if not hmac.compare_digest(grant.epoch, self._credential_epoch()):
            return False
        return self.sessions.check_session(grant.owner, grant.epoch, touch=False)

    def _stream_events(self, client: queue.Queue, initial: str, grant=None):
        try:
            yield initial
            while True:
                try:
                    message = client.get(timeout=self.sse_keepalive_interval)
                except queue.Empty:
                    if self._streams.closed:
                        return
                    message = ": keepalive\n\n"
                if message is None:
                    return
                if grant is not None and not self._grant_valid(grant):
                    return  # the browser reconnects and re-authenticates
                yield message
        finally:
            self._streams.remove(client)

    # ------------------------------------------------------------------
    # Serving
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Serve on settings.webBindHost:settings.webServerPort. Blocks."""
        host = settings.webBindHost or "0.0.0.0"
        port = int(settings.webServerPort)
        try:
            server = make_server(host, port, self.app, self.server_limits)
        except OSError as error:
            logger.error("Web server could not listen on %s:%s: %s", host, port, error)
            raise
        with self._server_lock:
            if self._shutdown_requested:
                server.server_close()
                return
            self._server = server
            self._serving_thread = threading.current_thread()
        self.bound_port = server.server_port
        logger.info("Web server listening on http://%s:%s", host, server.server_port)
        self.serving.set()
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            self.serving.clear()
            with self._server_lock:
                self._server = None
                self._serving_thread = None
            self._streams.close()
            server.server_close()
            logger.info("Web server stopped")

    def shutdown(self) -> None:
        """Make a blocking run() return. Safe from any thread, including a
        signal handler running on the thread that is inside run()."""
        with self._server_lock:
            self._shutdown_requested = True
            server = self._server
            serving_thread = self._serving_thread
        self._streams.close()
        if server is None:
            return
        if threading.current_thread() is serving_thread:
            # BaseServer.shutdown() waits for serve_forever() to return; on its
            # own thread that would deadlock, so wait from a helper instead.
            threading.Thread(
                target=server.shutdown, name="web-shutdown", daemon=True
            ).start()
        else:
            server.shutdown()


# ----------------------------------------------------------------------
# Module helpers (read settings at call time)
# ----------------------------------------------------------------------


def _default_rate_limit() -> str:
    return (
        f"{int(settings.rateLimitRequests)} per {int(settings.rateLimitWindow)} seconds"
    )


def _auto_stop_delay() -> int:
    try:
        return round(float(settings.autoStopDelay))
    except (TypeError, ValueError):
        return 0


def _theme_cookie() -> str | None:
    theme = request.cookies.get("theme")
    return theme if theme in ("light", "dark") else None


def _content_security_policy() -> str:
    images = "'self' data:"
    if settings.playerAvatars:
        images += f" {AVATAR_ORIGIN}"
    return "; ".join(
        [
            "default-src 'self'",
            f"img-src {images}",
            "style-src 'self'",
            "font-src 'self'",
            "script-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


def _ui_config() -> dict:
    """Feature flags the dashboard script needs (no secrets)."""
    control = bool(settings.controlServerThroughWeb)
    console = bool(settings.webConsole)
    return {
        "controlServer": control,
        "webConsole": console,
        "commands": control and console,
        "playerAvatars": bool(settings.playerAvatars),
        "autoStart": bool(settings.autoStart),
        "autoStop": bool(settings.autoStop),
        "autoStopDelay": _auto_stop_delay(),
        "maxCommandLength": MAX_COMMAND_LENGTH,
        "maxReasonLength": MAX_REASON_LENGTH,
    }


def _console_path(source: str) -> str | None:
    if source == "launch":
        return settings.data_path("server-console.log")
    if not settings.serverDir:
        return None
    return os.path.join(settings.serverDir, "logs", "latest.log")


def _safe_call(function, *args, default=None):
    try:
        return function(*args)
    except Exception:
        logger.exception("%s failed", getattr(function, "__name__", function))
        return default


def _build_info() -> dict:
    server_dir = settings.serverDir
    mods = list(_safe_call(server_files.list_mods, server_dir, default=[]) or [])
    icon = _safe_call(server_files.read_server_icon, server_dir) if server_dir else None
    if not (isinstance(icon, str) and icon.startswith("data:image/")):
        icon = None
    info = {
        "mods": mods,
        "modCount": len(mods),
        "icon": icon,
        "autoStart": bool(settings.autoStart),
        "autoStop": bool(settings.autoStop),
        "autoStopDelay": _auto_stop_delay(),
        "sleepingMotd": strip_formatting(settings.sleepingMotd).strip(),
        "respectWhitelist": bool(settings.respectWhitelist),
    }
    if settings.showServerIPAddress:
        info["serverAddress"] = _server_address()
    return info


def _server_address() -> str:
    """Address players use to join: this host's LAN IP and the game port."""
    port = settings.serverPort or 25565
    bind = settings.listenerBindHost
    if bind and bind not in ("0.0.0.0", "::"):
        return f"{bind}:{port}"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # No packet is sent: this only asks the routing table which local
            # address would carry off-host traffic.
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
    except OSError as error:
        logger.warning("Could not determine the server address: %s", error)
        address = "unknown"
    return f"{address}:{port}"
