"""Public application facade for Minecraft server lifecycle control.

OWNER: lifecycle agent. STUB: replace the bodies, keep the API
(see docs/ARCHITECTURE.md, "MinecraftController").
"""

from typing import Optional

from src.lifecycle.messages import ActionResult


class MinecraftController:
    def __init__(
        self,
        client,
        process_manager,
        player_manager,
        banlist_manager,
        bus,
        clock=None,
    ):
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def start_server(self) -> bool:
        raise NotImplementedError

    def stop_server(self) -> bool:
        raise NotImplementedError

    def restart_server(self) -> bool:
        raise NotImplementedError

    @property
    def generation(self) -> int:
        raise NotImplementedError

    @property
    def state(self) -> str:
        raise NotImplementedError

    def get_snapshot(self) -> dict:
        raise NotImplementedError

    def get_banned_players(self) -> list[dict]:
        raise NotImplementedError

    def kick_player(self, name: str, reason: Optional[str] = None) -> ActionResult:
        raise NotImplementedError

    def ban_player(self, name: str, reason: Optional[str] = None) -> ActionResult:
        raise NotImplementedError

    def unban_player(self, name: str) -> ActionResult:
        raise NotImplementedError

    def run_console_command(self, command: str) -> ActionResult:
        raise NotImplementedError
