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

"""Authentication helpers for the web admin interface."""

import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from flask_login import UserMixin

logger = logging.getLogger(__name__)

# Expired lockout entries are pruned once the table grows past this size, so
# a stream of distinct client addresses cannot grow it without bound.
_PRUNE_THRESHOLD = 1024


# Separates the username from the credential epoch in the Flask-Login id.
# The epoch is hex, so the last separator is always the right one.
_EPOCH_SEPARATOR = "|"


class User(UserMixin):
    """The single dashboard administrator, as seen by Flask-Login.

    The id stored in the session and the remember-me cookie is
    ``<username>|<credential epoch>``: changing the password changes the
    epoch, which revokes every existing session and remember cookie.
    """

    def __init__(self, username, epoch=""):
        self.username = username
        self.epoch = epoch
        self.id = f"{username}{_EPOCH_SEPARATOR}{epoch}" if epoch else username


def credential_epoch(username, password, secret_key) -> str:
    """HMAC (keyed with the session secret) of a hash of the credentials."""
    material = hashlib.sha256(
        f"{username}\0{password}".encode("utf-8", "surrogatepass")
    ).digest()
    key = (
        secret_key.encode("utf-8") if isinstance(secret_key, str) else bytes(secret_key)
    )
    return hmac.new(key, material, hashlib.sha256).hexdigest()[:32]


def split_user_id(user_id) -> tuple[str, str]:
    """(username, epoch) from a Flask-Login id; epoch is "" when absent."""
    username, separator, epoch = str(user_id).rpartition(_EPOCH_SEPARATOR)
    return (username, epoch) if separator else (str(user_id), "")


def verify_credentials(
    username: str | None,
    password: str | None,
    expected_username,
    expected_password,
) -> bool:
    """Constant-time credential check.

    Both fields are always compared (no short-circuit) and compared as
    SHA-256 digests so neither the match position nor the expected length
    leaks through timing. A missing configured username or password never
    authenticates anyone.
    """
    if expected_username in (None, "") or expected_password in (None, ""):
        logger.warning("Web login refused: web username/password not configured")
        return False
    user_ok = _digest_equal(username or "", str(expected_username))
    password_ok = _digest_equal(password or "", str(expected_password))
    return user_ok and password_ok


def _digest_equal(given: str, expected: str) -> bool:
    given_digest = hashlib.sha256(given.encode("utf-8", "surrogatepass")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8", "surrogatepass")).digest()
    return hmac.compare_digest(given_digest, expected_digest)


def is_safe_redirect(target: str | None) -> bool:
    """True only for a same-site relative path such as ``/`` or ``/x?y=1``.

    Rejects absolute URLs, scheme-relative ``//host`` and ``/\\host`` forms
    (browsers treat backslashes as slashes), and any control character
    (browsers drop tabs/newlines, turning ``/\\t/host`` into ``//host``).
    """
    if not target or not isinstance(target, str):
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in target):
        return False
    normalized = target.replace("\\", "/")
    if not normalized.startswith("/") or normalized.startswith("//"):
        return False
    parts = urlsplit(normalized)
    return not parts.scheme and not parts.netloc


def safe_next_url(target: str | None, default: str = "/") -> str:
    """``target`` when it is a safe relative path, else ``default``."""
    return target if is_safe_redirect(target) else default


class LoginAttemptTracker:
    """Failed login attempts per client address, with a temporary lockout.

    Failures are counted inside a window of ``lockout_duration`` seconds that
    starts at the first failure. Reaching ``max_attempts`` locks the address
    for ``lockout_duration`` seconds from that moment. Thread-safe; the clock
    is injectable for tests.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        lockout_duration: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_attempts = max(1, int(max_attempts))
        self.lockout_duration = float(lockout_duration)
        self._clock = clock
        self._lock = threading.Lock()
        # ip -> [failure_count, window_start, locked_until_or_None]
        self._failures: dict[str, list] = {}

    def _entry(self, ip, now):
        """The live entry for ``ip`` (expired entries are dropped)."""
        entry = self._failures.get(ip)
        if entry is None:
            return None
        _count, window_start, locked_until = entry
        if locked_until is not None:
            expired = now >= locked_until
        else:
            expired = now - window_start > self.lockout_duration
        if expired:
            del self._failures[ip]
            return None
        return entry

    def is_locked_out(self, ip) -> bool:
        with self._lock:
            entry = self._entry(ip, self._clock())
            return entry is not None and entry[2] is not None

    def record_failed_attempt(self, ip) -> None:
        with self._lock:
            now = self._clock()
            entry = self._entry(ip, now)
            if entry is None:
                entry = [0, now, None]
                self._failures[ip] = entry
            entry[0] += 1
            if entry[0] >= self.max_attempts and entry[2] is None:
                entry[2] = now + self.lockout_duration
                logger.warning(
                    "Login from %s locked for %ss after %s failed attempts",
                    ip,
                    round(self.lockout_duration),
                    entry[0],
                )
            if len(self._failures) > _PRUNE_THRESHOLD:
                self._prune(now)

    def _prune(self, now):
        for ip in list(self._failures):
            self._entry(ip, now)

    def record_successful_login(self, ip) -> None:
        with self._lock:
            self._failures.pop(ip, None)

    def get_remaining_attempts(self, ip) -> int:
        with self._lock:
            entry = self._entry(ip, self._clock())
            if entry is None:
                return self.max_attempts
            if entry[2] is not None:
                return 0
            return max(0, self.max_attempts - entry[0])

    def get_lockout_time_remaining(self, ip) -> int | None:
        """Whole seconds (rounded up) until the lockout ends; None if not locked."""
        with self._lock:
            now = self._clock()
            entry = self._entry(ip, now)
            if entry is None or entry[2] is None:
                return None
            return max(0, math.ceil(entry[2] - now))


# ----------------------------------------------------------------------
# Server-side sessions and remember-me tokens
# ----------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass
class _SessionRecord:
    fingerprint: str  # of the credential epoch the session was opened under
    deadline: float  # absolute end of the session
    idle: float  # seconds of inactivity that end it
    last_seen: float
    token_id: str | None  # the remember-me token it was opened with


class SessionStore:
    """Dashboard sessions and remember-me tokens, kept on the server.

    The signed session cookie only carries a random session id: logout
    deletes the id here, so a copied cookie stops working. Sessions live in
    memory (a monitor restart ends them) and have both an idle timeout and an
    absolute lifetime. Remember-me tokens are random per login and stored
    only as SHA-256 hashes with an expiry, in a 0600 JSON file so they
    survive restarts. Both are bound to a fingerprint of the credential
    epoch: changing the dashboard credentials revokes everything.
    Thread-safe; the clock is injectable for tests.
    """

    def __init__(self, path: str | None = None, clock: Callable[[], float] = time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionRecord] = {}
        self._tokens: dict[str, dict] = self._load()

    # -- sessions --------------------------------------------------------

    def open_session(self, epoch, idle: float, lifetime: float, token_id=None) -> str:
        sid = secrets.token_urlsafe(24)
        now = self._clock()
        record = _SessionRecord(
            _sha256(str(epoch)), now + lifetime, idle, now, token_id
        )
        with self._lock:
            if len(self._sessions) >= _PRUNE_THRESHOLD:
                self._prune_sessions(now)
            self._sessions[sid] = record
        return sid

    def check_session(self, sid, epoch, touch: bool = True) -> bool:
        """True while ``sid`` is open, unexpired and under the current epoch."""
        if not sid:
            return False
        with self._lock:
            record = self._sessions.get(sid)
            if record is None:
                return False
            now = self._clock()
            if not self._session_valid(record, epoch, now):
                del self._sessions[sid]
                return False
            if touch:
                record.last_seen = now
            return True

    @staticmethod
    def _session_valid(record: _SessionRecord, epoch, now: float) -> bool:
        return (
            hmac.compare_digest(record.fingerprint, _sha256(str(epoch)))
            and now < record.deadline
            and now - record.last_seen <= record.idle
        )

    def _prune_sessions(self, now: float) -> None:
        for sid, record in list(self._sessions.items()):
            if now >= record.deadline or now - record.last_seen > record.idle:
                del self._sessions[sid]

    def close_session(self, sid) -> str | None:
        """End one session; returns the remember-me token id it came with."""
        with self._lock:
            record = self._sessions.pop(sid, None) if sid else None
        return record.token_id if record else None

    def close_all(self) -> None:
        """Log out everywhere: every session and every remember-me token."""
        with self._lock:
            self._sessions.clear()
            self._tokens.clear()
            self._save()

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    # -- remember-me tokens ----------------------------------------------

    def issue_token(self, epoch, lifetime: float) -> tuple[str, str]:
        """(token id, cookie value). Only a hash of the secret is stored."""
        token_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._prune_tokens(now)
            self._tokens[token_id] = {
                "hash": _sha256(secret),
                "fingerprint": _sha256(str(epoch)),
                "expires": now + lifetime,
            }
            self._save()
        return token_id, f"{token_id}.{secret}"

    def redeem_token(self, cookie_value, epoch) -> str | None:
        """The token id if the cookie holds a live token for this epoch."""
        token_id, separator, secret = str(cookie_value or "").partition(".")
        if not separator or not token_id or not secret:
            return None
        with self._lock:
            entry = self._tokens.get(token_id)
            if entry is None:
                return None
            if self._clock() >= entry["expires"] or not hmac.compare_digest(
                entry["fingerprint"], _sha256(str(epoch))
            ):
                del self._tokens[token_id]
                self._save()
                return None
            if not hmac.compare_digest(entry["hash"], _sha256(secret)):
                return None
            return token_id

    def revoke_token(self, token_id) -> None:
        """Forget a token and end every session that was restored from it."""
        if not token_id:
            return
        with self._lock:
            for sid, record in list(self._sessions.items()):
                if record.token_id == token_id:
                    del self._sessions[sid]
            if self._tokens.pop(token_id, None) is not None:
                self._save()

    def token_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    def _prune_tokens(self, now: float) -> None:
        for token_id, entry in list(self._tokens.items()):
            if now >= entry["expires"]:
                del self._tokens[token_id]

    # -- persistence -----------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if not self._path or not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
            tokens = data["tokens"]
            now = self._clock()
            return {
                str(token_id): {
                    "hash": str(entry["hash"]),
                    "fingerprint": str(entry["fingerprint"]),
                    "expires": float(entry["expires"]),
                }
                for token_id, entry in tokens.items()
                if float(entry["expires"]) > now
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            logger.warning(
                "Ignoring unreadable remember-me store %s: %s", self._path, error
            )
            return {}

    def _save(self) -> None:
        """Atomic, owner-only write (called with the lock held)."""
        if not self._path:
            return
        temporary = f"{self._path}.tmp"
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "tokens": self._tokens}, handle)
            os.replace(temporary, self._path)
        except OSError as error:
            logger.warning(
                "Could not save remember-me tokens to %s: %s", self._path, error
            )
