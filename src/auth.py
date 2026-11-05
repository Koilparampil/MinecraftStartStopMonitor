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

import hashlib
import hmac
import logging
import math
import threading
import time
from collections.abc import Callable
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
