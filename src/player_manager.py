"""Tracked players (online + last-seen), persisted to data/players.json.

Players are keyed by lowercase name. Each record is
``{name, uuid, currently_online, last_online, first_seen}`` (wall-clock epoch
seconds). The file is ``{"version": 1, "players": {...}}`` and is rewritten
when membership/online state/name/uuid change, otherwise at most once per
``SAVE_INTERVAL`` while only ``last_online`` moves.
"""

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from src.events import Event, EventBus
from src.lifecycle.messages import OnlinePlayer, Roster
from src.settings import settings

logger = logging.getLogger(__name__)

DATA_VERSION = 1
SAVE_INTERVAL = 60.0  # seconds; throttles saves that only move last_online


def write_json_atomic(path, payload):
    """Write JSON next to *path* and atomically replace it."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".players-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())  # a crash never leaves a half-written file
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


def _clean_record(record):
    """A well-formed player record from loaded JSON, or None."""
    if not isinstance(record, dict):
        return None
    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    uuid = record.get("uuid")

    def _time(key):
        value = record.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    first_seen = _time("first_seen")
    last_online = _time("last_online")
    return {
        "name": name.strip(),
        "uuid": uuid if isinstance(uuid, str) and uuid else None,
        "currently_online": bool(record.get("currently_online", False)),
        "last_online": last_online,
        "first_seen": first_seen if first_seen is not None else last_online,
    }


def _sort_key(record):
    return (
        not record.get("currently_online", False),
        -(record.get("last_online") or 0.0),
        record["name"].lower(),
    )


class PlayerManager:
    """Online/offline players with timestamps, fed by the supervisor's rosters.

    Thread-safe. Events (PLAYER_JOINED / PLAYER_LEFT with ``{name, uuid}``)
    are published after the internal lock is released.
    """

    def __init__(
        self,
        bus: EventBus,
        data_file: str | None = None,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.bus = bus
        self._data_file = data_file
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._players: dict[str, dict[str, Any]] = {}
        self._last_save: float | None = None
        self._unsaved = False
        self._persist = True  # False after a read error: never overwrite that file
        if self._tracking():
            self._players = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @property
    def data_file(self) -> str:
        return self._data_file or settings.data_path("players.json")

    @staticmethod
    def _tracking() -> bool:
        return bool(settings.enablePlayerTracking)

    def _load(self):
        try:
            path = self.data_file
            with open(path, encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return {}
        except OSError as e:
            # Unreadable is not empty: saving now would destroy the history.
            self._persist = False
            logger.error(
                f"Cannot read player data ({e}). Player history is kept in memory "
                "only and NOT saved, so the existing file is never overwritten; fix "
                "its permissions and restart the monitor."
            )
            return {}
        except ValueError as e:
            self._quarantine(path, f"not valid JSON ({e})")
            return {}
        players = data.get("players") if isinstance(data, dict) else None
        if not isinstance(players, dict):
            self._quarantine(path, "unexpected structure")
            return {}
        loaded = {}
        for record in players.values():
            clean = _clean_record(record)
            if clean is not None:
                loaded[clean["name"].lower()] = clean
        return loaded

    @staticmethod
    def _quarantine(path, reason):
        """Keep an unreadable file for inspection instead of overwriting it."""
        target = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(path, target)
            logger.warning(f"Player data in {path} is {reason}; moved it to {target}")
        except OSError as e:
            logger.warning(
                f"Player data in {path} is {reason} and could not be moved: {e}"
            )

    def _save_locked(self, force=False):
        """Persist now when forced; otherwise flush pending timestamp-only
        changes at most once per SAVE_INTERVAL."""
        if not self._tracking() or not self._persist:
            return
        now = self._monotonic()
        if not force:
            if not self._unsaved:
                return
            if self._last_save is not None and now - self._last_save < SAVE_INTERVAL:
                return
        try:
            write_json_atomic(
                self.data_file, {"version": DATA_VERSION, "players": self._players}
            )
            self._last_save = now
            self._unsaved = False
        except OSError as e:
            logger.error(f"Could not save player data to {self.data_file}: {e}")

    # ------------------------------------------------------------------
    # Roster updates
    # ------------------------------------------------------------------

    def update_from_roster(self, roster: Roster) -> None:
        """Apply a successful roster: mark listed players online (publishing
        PLAYER_JOINED for newly-online ones) and the rest offline (PLAYER_LEFT).

        A partial roster (fewer names than ``roster.online``: status-ping
        samples are capped at 12 and servers can hide names) cannot tell who
        left, so it never marks a listed-somewhere-else player offline just
        for missing from one sample. Only when more players are tracked online
        than ``roster.online`` are the excess ones marked offline, those seen
        least recently first (players who left stop being seen).
        """
        now = self._clock()
        events = []
        with self._lock:
            listed = set()
            changed = False
            for player in roster.players:
                key, player_changed = self._apply_online(player, now, events)
                if key is not None:
                    listed.add(key)
                    changed = changed or player_changed
            if listed:
                self._unsaved = True  # last_online moved
            if len(roster.players) >= roster.online:
                changed = self._mark_offline_except(listed, events) or changed
            else:
                changed = self._trim_excess(listed, roster.online, events) or changed
            self._save_locked(force=changed)
        self._publish(events)

    def _apply_online(self, player: OnlinePlayer, now, events):
        """Mark one roster entry online. Returns (key, structurally_changed)."""
        name = (player.name or "").strip()
        if not name:
            return None, False
        key = name.lower()
        uuid = player.uuid or None
        changed = self._adopt_renamed(key, uuid)
        record = self._players.get(key)
        if record is None:
            record = {
                "name": name,
                "uuid": uuid,
                "currently_online": False,
                "last_online": now,
                "first_seen": now,
            }
            self._players[key] = record
            changed = True
        if record["name"] != name or (uuid and record.get("uuid") != uuid):
            record["name"] = name
            record["uuid"] = uuid or record.get("uuid")
            changed = True
        if not record.get("currently_online"):
            record["currently_online"] = True
            events.append((Event.PLAYER_JOINED, {"name": name, "uuid": record["uuid"]}))
            changed = True
        record["last_online"] = now
        if record.get("first_seen") is None:
            record["first_seen"] = now
        return key, changed

    def _adopt_renamed(self, key, uuid):
        """A known UUID under a new name is a renamed player: move the record."""
        if not uuid or key in self._players:
            return False
        for other_key, record in list(self._players.items()):
            if record.get("uuid") == uuid:
                self._players[key] = self._players.pop(other_key)
                return True
        return False

    def _mark_offline_except(self, keep, events):
        changed = False
        for key, record in self._players.items():
            if key in keep or not record.get("currently_online"):
                continue
            record["currently_online"] = False
            events.append(
                (
                    Event.PLAYER_LEFT,
                    {"name": record["name"], "uuid": record.get("uuid")},
                )
            )
            changed = True
        if not self._tracking():
            # Without tracking only currently-online players are kept (in memory).
            self._players = {
                k: r for k, r in self._players.items() if r.get("currently_online")
            }
        return changed

    def _trim_excess(self, listed, online, events):
        tracked = [r for r in self._players.values() if r.get("currently_online")]
        excess = len(tracked) - max(0, online)
        if excess <= 0:
            return False
        unlisted = [r for r in tracked if r["name"].lower() not in listed]
        unlisted.sort(key=lambda r: r.get("last_online") or 0.0)
        stale = {r["name"].lower() for r in unlisted[:excess]}
        keep = {r["name"].lower() for r in tracked} - stale
        return self._mark_offline_except(keep, events)

    def mark_all_offline(self) -> None:
        """Release tracked players when the server goes down (records are kept)."""
        now = self._clock()
        events = []
        with self._lock:
            for record in self._players.values():
                if record.get("currently_online"):
                    record["last_online"] = now
            changed = self._mark_offline_except(set(), events)
            if changed or self._unsaved:
                self._save_locked(force=True)
        self._publish(events)

    def _publish(self, events):
        for event_type, payload in events:
            self.bus.publish(event_type, payload)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_all_players(self) -> list[dict[str, Any]]:
        """[{name, uuid, currently_online, last_online, first_seen}] copies,
        online players first, then most recently seen."""
        with self._lock:
            records = [dict(record) for record in self._players.values()]
        return sorted(records, key=_sort_key)

    def get_online_players(self) -> list[dict[str, Any]]:
        return [p for p in self.get_all_players() if p.get("currently_online")]

    def get_offline_players(self) -> list[dict[str, Any]]:
        return [p for p in self.get_all_players() if not p.get("currently_online")]

    def get_player_count(self) -> int:
        """Tracked players currently online (may be < Roster.online)."""
        with self._lock:
            return sum(1 for r in self._players.values() if r.get("currently_online"))

    def get_total_player_count(self) -> int:
        with self._lock:
            return len(self._players)

    def find_player(self, name: str) -> dict[str, Any] | None:
        """Copy of a tracked player's record (case-insensitive name), or None."""
        if not name:
            return None
        with self._lock:
            record = self._players.get(name.strip().lower())
            return dict(record) if record else None

    def find_uuid(self, name: str) -> str | None:
        """UUID of a tracked player (case-insensitive name), or None."""
        if not name:
            return None
        with self._lock:
            record = self._players.get(name.strip().lower())
            return record.get("uuid") if record else None
