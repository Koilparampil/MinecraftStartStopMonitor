"""Sleeping-server responder: speaks just enough of the Minecraft Java
protocol on the game port while the real server is stopped.

OWNER: protocol agent. STUB: replace the body, keep the API
(see docs/ARCHITECTURE.md, "sleeping").
"""

import socket
from dataclasses import dataclass
from typing import Callable, Optional


class ConnectionKind:
    STATUS = "status"  # server-list ping (handshake next_state=1)
    LOGIN = "login"  # join attempt (next_state=2, or 3 = transfer)
    LEGACY_PING = "legacy_ping"  # 0xFE pre-1.7 style ping
    INVALID = "invalid"  # garbage, timeout, disconnect mid-handshake


@dataclass(frozen=True)
class SleepingServerConfig:
    motd: str  # may contain section-sign formatting codes
    version_name: str  # shown in the server list, e.g. "Sleeping"
    protocol_version: Optional[int]  # None -> echo the client's protocol
    max_players: int
    favicon: Optional[str]  # "data:image/png;base64,..." or None
    kick_message: str  # shown to an allowed player whose login wakes the server
    whitelist_message: str = "You are not white-listed on this server!"
    banned_message: str = "You are banned from this server."


@dataclass(frozen=True)
class ConnectionOutcome:
    kind: str
    player_name: Optional[str] = None
    protocol_version: Optional[int] = None
    wake: bool = False  # True only for an allowed LOGIN


# is_allowed(player_name, client_ip) -> (allowed, deny_message_or_None)
AccessCheck = Callable[[str, str], tuple[bool, Optional[str]]]


def handle_connection(
    conn: socket.socket,
    client_ip: str,
    config: SleepingServerConfig,
    is_allowed: AccessCheck,
    timeout: float = 5.0,
) -> ConnectionOutcome:
    """Serve one accepted client connection as the sleeping server.

    Never raises for client/protocol errors (returns INVALID). Does not close
    the listening socket; closes nothing but may shut down `conn` gracefully
    after the response is sent. The caller closes `conn`.
    """
    raise NotImplementedError
