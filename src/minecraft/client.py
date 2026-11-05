"""MinecraftClient: the one object the app uses to talk to a running server.

OWNER: protocol agent. STUB: replace the bodies, keep the API
(see docs/ARCHITECTURE.md, "MinecraftClient").
"""

from typing import Optional

from src.lifecycle.messages import ActionResult, Roster


class RconError(Exception):
    """RCON connection/auth/protocol failure."""


class RconUnavailable(RconError):
    """RCON is not configured (no password) or the server is not answering."""


class MinecraftClient:
    """Thread-safe facade over RCON (preferred) and the status ping (fallback).

    Constructed with explicit values (main passes resolved settings) so tests
    never depend on the settings singleton.
    """

    def __init__(
        self,
        host: str,
        port: int,
        rcon_port: int,
        rcon_password: Optional[str],
        timeout: float = 5.0,
    ):
        raise NotImplementedError

    @property
    def rcon_enabled(self) -> bool:
        """True when an RCON password is configured."""
        raise NotImplementedError

    def get_roster(self) -> Optional[Roster]:
        """RCON `list uuids` (falling back to `list`), else the status ping.
        None when neither answers or the answer cannot be parsed."""
        raise NotImplementedError

    def run_command(self, command: str) -> str:
        """Run a console command over RCON and return its (color-stripped) output.
        Raises RconUnavailable / RconError."""
        raise NotImplementedError

    def kick_player(self, name: str, reason: Optional[str] = None) -> ActionResult:
        raise NotImplementedError

    def ban_player(self, name: str, reason: Optional[str] = None) -> ActionResult:
        raise NotImplementedError

    def unban_player(self, name: str) -> ActionResult:
        """RCON `pardon <name>`."""
        raise NotImplementedError

    def say(self, message: str) -> bool:
        raise NotImplementedError

    def request_stop(self) -> bool:
        """Send `stop` over RCON. True if the command was sent (the server may
        drop the connection before answering; that still counts as sent)."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
