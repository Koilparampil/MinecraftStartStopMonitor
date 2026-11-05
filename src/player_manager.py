"""Tracked players (online + last-seen), persisted to data/players.json.

OWNER: app-shell agent. STUB: replace the bodies, keep the API
(see docs/ARCHITECTURE.md, "PlayerManager").
"""

from typing import Any, Dict, List, Optional

from src.events import EventBus
from src.lifecycle.messages import Roster


class PlayerManager:
    def __init__(self, bus: EventBus, data_file: Optional[str] = None):
        raise NotImplementedError

    def update_from_roster(self, roster: Roster) -> None:
        """Apply a successful roster: mark listed players online (publishing
        PLAYER_JOINED for newly-online ones) and the rest offline (PLAYER_LEFT)."""
        raise NotImplementedError

    def mark_all_offline(self) -> None:
        raise NotImplementedError

    def get_all_players(self) -> List[Dict[str, Any]]:
        """[{name, uuid, currently_online, last_online, first_seen}] copies."""
        raise NotImplementedError

    def get_online_players(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_player_count(self) -> int:
        """Tracked players currently online (may be < Roster.online)."""
        raise NotImplementedError

    def find_uuid(self, name: str) -> Optional[str]:
        raise NotImplementedError
