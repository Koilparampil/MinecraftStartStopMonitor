"""Reads the server's banned-players.json; offline ban/unban edits.

The file is vanilla format (``[{uuid, name, created, source, expires, reason}]``)
and belongs to the server: it rewrites it on ``/ban`` and ``/pardon``. The
monitor only edits it while the server is STOPPED (the controller guarantees
that), atomically and in vanilla format.
"""

import json
import logging
import os
import re
import threading
import uuid as uuid_module
from datetime import datetime

from src.events import Event, EventBus
from src.lifecycle.messages import ActionResult
from src.minecraft import server_files
from src.settings import settings

logger = logging.getLogger(__name__)

BANNED_PLAYERS_FILE = "banned-players.json"
USERCACHE_FILE = "usercache.json"
DEFAULT_REASON = "Banned by an operator."
DEFAULT_SOURCE = "Server"
PLAYER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.*-]{1,32}$")
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def _text(value, default=None):
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def normalize_ban_entry(entry):
    """Dashboard form of a vanilla entry, or None for unusable entries."""
    if not isinstance(entry, dict):
        return None
    uuid = _text(entry.get("uuid"))
    name = _text(entry.get("name")) or uuid
    if not name:
        return None
    return {
        "name": name,
        "uuid": uuid.lower() if uuid else None,
        "reason": _text(entry.get("reason"), DEFAULT_REASON),
        "source": _text(entry.get("source"), DEFAULT_SOURCE),
        "created": _text(entry.get("created")),
        "expires": _text(entry.get("expires"), "forever"),
    }


def ban_is_active(entry, now=None):
    """False only for bans whose ``expires`` date has passed."""
    expires = _text(entry.get("expires"), "forever")
    if expires.lower() == "forever":
        return True
    try:
        moment = datetime.strptime(expires, _DATE_FORMAT)
    except ValueError:
        return True
    now = datetime.now(moment.tzinfo) if now is None else now
    return moment > now


def canonical_uuid(value):
    """Dashed lowercase UUID (``str(uuid.UUID(...))``), or None if invalid."""
    text = _text(value)
    if not text:
        return None
    try:
        return str(uuid_module.UUID(text))
    except ValueError:
        return None


def _vanilla_timestamp():
    return datetime.now().astimezone().strftime(_DATE_FORMAT)


def _read_entries_strict(path):
    """The raw list from *path* ([] if missing/blank). Raises ValueError (bad
    JSON) or TypeError (not a list), so an edit never overwrites a file it
    could not read."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as file:
        text = file.read()
    if not text.strip():
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise TypeError("expected a JSON list")
    return data


class BanlistManager:
    """banned-players.json reader (mtime-cached) with offline edits.

    ``refresh()`` publishes BAN_ADDED / BAN_REMOVED (``{name, uuid}``) for
    changes since the previous refresh; reads through
    ``get_banned_players()`` only update the cache. Events are published
    after the internal lock is released.
    """

    def __init__(
        self, bus: EventBus, server_dir: str | None = None, *, known_players=None
    ):
        """*known_players*: optional PlayerManager-like object with
        ``find_player(name) -> {name, uuid} | None`` (tracked players), used
        for the canonical name and UUID of offline bans."""
        self.bus = bus
        self._server_dir = server_dir
        self._known_players = known_players
        self._lock = threading.RLock()
        self._cache_key = None
        self._cache: list[dict] = []
        with self._lock:
            self._known = self._index(self._read_cached())

    # ------------------------------------------------------------------
    # Paths and reading
    # ------------------------------------------------------------------

    @property
    def server_dir(self) -> str | None:
        return self._server_dir or settings.serverDir

    def _path(self, file_name):
        directory = self.server_dir
        return os.path.join(directory, file_name) if directory else None

    @property
    def banlist_path(self) -> str | None:
        return self._path(BANNED_PLAYERS_FILE)

    def _read_cached(self):
        path = self.banlist_path
        if not path:
            return []
        try:
            stat = os.stat(path)
        except OSError:
            self._cache_key, self._cache = None, []
            return self._cache
        key = (path, stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if key != self._cache_key:
            entries = server_files.read_json_list(path)
            self._cache = [
                e for e in map(normalize_ban_entry, entries) if e is not None
            ]
            self._cache_key = key
        return self._cache

    @staticmethod
    def _index(entries):
        return {
            entry["name"].lower(): {"name": entry["name"], "uuid": entry["uuid"]}
            for entry in entries
        }

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_banned_players(self) -> list[dict]:
        """[{name, uuid, reason, source, created, expires}] from
        banned-players.json (mtime-cached; [] if missing/unreadable)."""
        with self._lock:
            return [dict(entry) for entry in self._read_cached()]

    def is_banned(self, name: str) -> bool:
        """Case-insensitive name match (expired bans do not count)."""
        if not name:
            return False
        lowered = name.strip().lower()
        with self._lock:
            entries = list(self._read_cached())
        return any(e["name"].lower() == lowered and ban_is_active(e) for e in entries)

    def refresh(self) -> None:
        """Re-read the file now; publish BAN_ADDED / BAN_REMOVED for changes
        since the last read."""
        with self._lock:
            self._cache_key = None
            current = self._index(self._read_cached())
            added = [current[k] for k in current if k not in self._known]
            removed = [self._known[k] for k in self._known if k not in current]
            self._known = current
        for payload in added:
            self.bus.publish(Event.BAN_ADDED, dict(payload))
        for payload in removed:
            self.bus.publish(Event.BAN_REMOVED, dict(payload))

    def lookup_uuid(self, name: str) -> str | None:
        """UUID for a name from usercache.json (then the ban list), else None."""
        cached = self._usercache_entry(name)
        if cached is not None and cached[1]:
            return cached[1]
        lowered = (name or "").strip().lower()
        for entry in self.get_banned_players():
            if lowered and entry["name"].lower() == lowered and entry["uuid"]:
                return canonical_uuid(entry["uuid"])
        return None

    def _usercache_entry(self, name):
        """(canonical name, uuid or None) from usercache.json, or None."""
        lowered = (name or "").strip().lower()
        path = self._path(USERCACHE_FILE)
        if not lowered or not path:
            return None
        for entry in server_files.read_json_list(path):
            entry_name = _text(entry.get("name"))
            if entry_name and entry_name.lower() == lowered:
                return entry_name, canonical_uuid(entry.get("uuid"))
        return None

    def _identity(self, name, uuid):
        """Canonical (name, uuid): tracked players, then usercache.json, then
        the offline-mode UUID (online-mode=false only); the typed name last."""
        uuid = canonical_uuid(uuid)
        tracked = self._known_players.find_player(name) if self._known_players else None
        if tracked:
            name, uuid = tracked["name"], uuid or canonical_uuid(tracked.get("uuid"))
        cached = self._usercache_entry(name)
        if cached is not None:
            name = name if tracked else cached[0]
            uuid = uuid or cached[1]
        return name, uuid or self.lookup_uuid(name) or self._offline_mode_uuid(name)

    def _offline_mode_uuid(self, name):
        path = self._path("server.properties")
        props = server_files.read_properties(path) if path else {}
        if str(props.get("online-mode", "true")).strip().lower() == "false":
            return server_files.offline_uuid(name)
        return None

    # ------------------------------------------------------------------
    # Offline edits (server STOPPED)
    # ------------------------------------------------------------------

    def add_ban_offline(
        self, name: str, uuid: str | None = None, reason: str | None = None
    ) -> ActionResult:
        """Write an entry while the server is STOPPED (caller guarantees it)."""
        name = (name or "").strip()
        if not PLAYER_NAME_PATTERN.match(name):
            return ActionResult(False, f"Invalid player name: {name!r}")
        path = self.banlist_path
        if not path:
            return ActionResult(False, "minecraft.serverDir is not configured")
        with self._lock:
            result = self._add_entry_locked(path, name, uuid, reason)
        if result.success and result.details.get("changed"):
            self.refresh()
        return result

    def _add_entry_locked(self, path, name, uuid, reason):
        try:
            entries = _read_entries_strict(path)
        except (OSError, ValueError, TypeError) as e:
            return ActionResult(
                False, f"Cannot read {path} ({e}); fix or remove it first"
            )
        name, uuid = self._identity(name, uuid)
        if self._find(entries, name, uuid) is not None:
            return ActionResult(
                True, f"{name} is already banned", details={"changed": False}
            )
        if not uuid:
            return ActionResult(
                False,
                f"The UUID of {name} is unknown (they never joined this server): "
                "start the server to ban this player.",
            )
        entries.append(
            {
                "uuid": uuid,
                "name": name,
                "created": _vanilla_timestamp(),
                "source": DEFAULT_SOURCE,
                "expires": "forever",
                "reason": _text(reason, DEFAULT_REASON),
            }
        )
        try:
            server_files.write_json_list(path, entries)
        except OSError as e:
            return ActionResult(False, f"Could not write {path}: {e}")
        logger.info(f"Banned {name} ({uuid}) in {path} while the server is stopped")
        return ActionResult(
            True, f"Banned {name}", details={"changed": True, "uuid": uuid}
        )

    @staticmethod
    def _find(entries, name, uuid):
        lowered = name.lower()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name", "")).lower() == lowered:
                return entry
            if uuid and str(entry.get("uuid", "")).lower() == uuid.lower():
                return entry
        return None

    def remove_ban_offline(self, name: str) -> ActionResult:
        """Remove an entry while the server is STOPPED (caller guarantees it)."""
        name = (name or "").strip()
        if not PLAYER_NAME_PATTERN.match(name):
            return ActionResult(False, f"Invalid player name: {name!r}")
        path = self.banlist_path
        if not path:
            return ActionResult(False, "minecraft.serverDir is not configured")
        with self._lock:
            result = self._remove_entry_locked(path, name)
        if result.success and result.details.get("changed"):
            self.refresh()
        return result

    @staticmethod
    def _remove_entry_locked(path, name):
        try:
            entries = _read_entries_strict(path)
        except (OSError, ValueError, TypeError) as e:
            return ActionResult(
                False, f"Cannot read {path} ({e}); fix or remove it first"
            )
        lowered = name.lower()
        remaining = [
            entry
            for entry in entries
            if not (
                isinstance(entry, dict)
                and str(entry.get("name", "")).lower() == lowered
            )
        ]
        if len(remaining) == len(entries):
            return ActionResult(
                True, f"{name} is not banned", details={"changed": False}
            )
        try:
            server_files.write_json_list(path, remaining)
        except OSError as e:
            return ActionResult(False, f"Could not write {path}: {e}")
        logger.info(f"Unbanned {name} in {path} while the server is stopped")
        return ActionResult(True, f"Unbanned {name}", details={"changed": True})
