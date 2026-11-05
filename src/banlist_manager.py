"""Reads the server's banned-players.json; offline ban/unban edits.

OWNER: app-shell agent. STUB: replace the bodies, keep the API
(see docs/ARCHITECTURE.md, "BanlistManager").
"""

from typing import Optional

from src.events import EventBus
from src.lifecycle.messages import ActionResult


class BanlistManager:
    def __init__(self, bus: EventBus, server_dir: Optional[str] = None):
        raise NotImplementedError

    def get_banned_players(self) -> list[dict]:
        """[{name, uuid, reason, source, created, expires}] from
        banned-players.json (mtime-cached; [] if missing/unreadable)."""
        raise NotImplementedError

    def is_banned(self, name: str) -> bool:
        """Case-insensitive name match."""
        raise NotImplementedError

    def refresh(self) -> None:
        """Re-read the file now; publish BAN_ADDED / BAN_REMOVED for changes
        since the last read."""
        raise NotImplementedError

    def lookup_uuid(self, name: str) -> Optional[str]:
        """UUID for a name from usercache.json (then the ban list), else None."""
        raise NotImplementedError

    def add_ban_offline(
        self, name: str, uuid: Optional[str] = None, reason: Optional[str] = None
    ) -> ActionResult:
        """Write an entry while the server is STOPPED (caller guarantees it)."""
        raise NotImplementedError

    def remove_ban_offline(self, name: str) -> ActionResult:
        """Remove an entry while the server is STOPPED (caller guarantees it)."""
        raise NotImplementedError
