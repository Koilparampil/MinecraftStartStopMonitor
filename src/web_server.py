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
import hmac
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
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.serving import make_server

from src.auth import (
    LoginAttemptTracker,
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
    expires_at: float  # time.monotonic() deadline (the session's lifetime)


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

    def close(self) -> None:
        """End every open stream; refuse new ones."""
        with self._lock:
            self._closed.set()
            for client in self._clients:
                _put_latest(client, None)


class WebServer:
    """The dashboard: a Flask app plus its committed-snapshot cache and SSE hub."""

    def __init__(self, controller, bus):
        self.controller = controller
        self.bus = bus
        self.sse_keepalive_interval = SSE_KEEPALIVE_SECONDS
        self.info_cache_seconds = INFO_CACHE_SECONDS

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
        suffix = str(settings.webServerPort)
        app.config.update(
            PERMANENT_SESSION_LIFETIME=timedelta(seconds=int(settings.sessionTimeout)),
            SESSION_COOKIE_NAME=f"mcmonitor_session_{suffix}",
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
            REMEMBER_COOKIE_NAME=f"mcmonitor_remember_{suffix}",
            REMEMBER_COOKIE_HTTPONLY=True,
            REMEMBER_COOKIE_SAMESITE="Lax",
            REMEMBER_COOKIE_DURATION=timedelta(days=7),
            # The token lives in the (expiring) session; a dashboard left open
            # for hours must still be able to act.
            WTF_CSRF_TIME_LIMIT=None,
        )
        app.jinja_env.globals["static_url"] = self._static_url
        return app

    def _create_limiter(self) -> Limiter:
        # Login is exempt (LoginAttemptTracker owns that throttle: two
        # counters on one request would lock out at an unpredictable count),
        # as are /stream, /healthz, /console and static files.
        return Limiter(
            get_remote_address,
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
        return User(username, epoch)

    def _ensure_session_id(self):
        """Give every logged-in session an id (remember-cookie logins too), so
        logout can end exactly that session's live streams."""
        if request.endpoint in (None, "static", "healthz"):
            return
        if "sid" not in session and current_user.is_authenticated:
            session["sid"] = secrets.token_urlsafe(16)

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

        @app.route("/healthz")
        @limiter.exempt
        def healthz():
            return jsonify(ok=True)

    def _register_error_handlers(self):
        self.app.before_request(self._ensure_session_id)
        self.app.register_error_handler(CSRFError, self._handle_csrf_error)
        self.app.register_error_handler(429, self._handle_rate_limited)
        self.app.after_request(self._add_security_headers)

    # ------------------------------------------------------------------
    # Auth / error responses
    # ------------------------------------------------------------------

    def _unauthorized(self):
        """Pages redirect to the login form; API calls get 401 JSON."""
        if request.endpoint in ("index", "logout") and request.method == "GET":
            return redirect(self._login_url_for_current_page())
        if request.endpoint == "logout":
            return redirect(url_for("login"))
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
        if not current_user.is_authenticated:
            return self._unauthorized()
        logger.warning("CSRF check failed for %s: %s", request.path, error.description)
        return (
            jsonify(
                success=False,
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
    def _add_security_headers(response):
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Content-Security-Policy", _content_security_policy())
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

    @staticmethod
    def _payload(snapshot: dict) -> dict:
        return {
            "data": dict(snapshot),
            "autoStopDelay": _auto_stop_delay(),
            "serverTime": time.time(),
        }

    def _json_state(self, status: int = 200, **extra):
        """JSON body built around one consistent committed snapshot."""
        body = self._payload(self._refresh_snapshot())
        body.update(extra)
        return jsonify(body), status

    # ------------------------------------------------------------------
    # Login / logout / dashboard
    # ------------------------------------------------------------------

    def _handle_login(self):
        ip = request.remote_addr or "unknown"
        next_url = self._next_url()
        if request.method == "GET" and current_user.is_authenticated:
            return redirect(next_url)
        if self.login_tracker.is_locked_out(ip):
            return self._render_locked_out(ip)
        if request.method == "POST":
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
            session.permanent = True
            user = User(str(settings.webUsername), self._credential_epoch())
            login_user(user, remember=remember)
            session["sid"] = secrets.token_urlsafe(16)
            self.login_tracker.record_successful_login(ip)
            logger.info("Web login from %s", ip)
            return redirect(next_url)

        self.login_tracker.record_failed_attempt(ip)
        logger.warning("Failed login attempt from %s for user %r", ip, username)
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
            status=429,
        )

    def _render_login(self, status: int = 200, **context):
        context.setdefault("next_url", self._next_url())
        return (
            render_template("login.html", theme=_theme_cookie(), **context),
            status,
        )

    def _handle_logout(self):
        logger.info("Web logout from %s", request.remote_addr)
        sid = session.pop("sid", None)
        logout_user()
        if sid:
            self._streams.close_owner(sid)
        flash("You have been signed out.", "info")
        return redirect(url_for("login"))

    def _handle_index(self):
        snapshot = self._refresh_snapshot()
        config = _ui_config()
        server = snapshot.get("server") or {}
        return render_template(
            "index.html",
            theme=_theme_cookie(),
            snapshot=snapshot,
            state=snapshot.get("state") or LifecycleState.STOPPED,
            server=server,
            motd=strip_formatting(server.get("motd")).strip(),
            status=status_label(snapshot),
            config=config,
            bootstrap={"payload": self._payload(snapshot), "config": config},
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
        exists = bool(path) and os.path.isfile(path)
        lines = (
            [_ANSI_RE.sub("", line) for line in server_files.tail_file(path, count)]
            if exists
            else []
        )
        return jsonify(success=True, source=source, lines=lines, exists=exists)

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
        return jsonify(self._get_info())

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
            owner=session.get("sid"),
            epoch=getattr(current_user, "epoch", ""),
            expires_at=time.monotonic() + float(settings.sessionTimeout),
        )
        with self._lock:
            # The initial snapshot is read and the queue registered under the
            # broadcast lock, so no committed update can slip in between.
            snapshot = self._cache_snapshot(self._read_snapshot())
            client = self._streams.add(grant.owner)
            initial = self._sse_message(self._payload(snapshot), retry=SSE_RETRY_MS)
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
        when that session would expire or the credentials change (logout
        ends it at once through close_owner)."""
        if time.monotonic() >= grant.expires_at:
            return False
        return hmac.compare_digest(grant.epoch, self._credential_epoch())

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
            server = make_server(host, port, self.app, threaded=True)
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
