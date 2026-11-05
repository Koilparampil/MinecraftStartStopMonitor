"""MinecraftClient: the one object the app uses to talk to a running server.

RCON is preferred (`list uuids`, falling back to `list`); the status ping is
the roster fallback and the source of version/MOTD. See docs/ARCHITECTURE.md,
"MinecraftClient".
"""

import dataclasses
import logging
import re
import threading
import time
import uuid as uuid_module
from collections.abc import Callable

from src.lifecycle.messages import ActionResult, OnlinePlayer, Roster
from src.minecraft.ping import PingError, ServerStatus, ping_status
from src.minecraft.protocol import strip_formatting
from src.minecraft.rcon import RconAuthError, RconClient, RconError, RconUnavailable

__all__ = [
    "MinecraftClient",
    "RconAuthError",
    "RconError",
    "RconUnavailable",
    "normalize_uuid",
    "parse_list_response",
    "roster_from_status",
]

logger = logging.getLogger(__name__)

DEFAULT_GAME_PORT = 25565
DEFAULT_RCON_PORT = 25575
SERVER_INFO_TTL = 60.0
NIL_UUID = "00000000-0000-0000-0000-000000000000"
ANONYMOUS_PLAYER = "anonymous player"

# 1.13+: "There are 2 of a max of 20 players online: A, B"
# 1.7-1.12: "There are 2/20 players online:" + names (RCON joins with \n or nothing)
# Bukkit/Essentials: "There are 2 out of maximum 20 players online."
# EssentialsX with vanished players: "There are 1/1 out of maximum 20 ..."
# (visible/hidden: both are online).
_MAXIMUM = r"out\s+of\s+(?:a\s+)?max(?:imum)?(?:\s+of)?"
_LIST_HEADER = re.compile(
    r"There\s+(?:are|is)\s+(?P<online>\d+)\s*"
    rf"(?:/\s*(?P<hidden>\d+)\s*{_MAXIMUM}|/|of\s+a\s+max(?:imum)?(?:\s+of)?|{_MAXIMUM})"
    r"\s*(?P<max>\d+)\s+players?\s+online\s*[:.]?",
    re.IGNORECASE,
)
_UUID_ENTRY = re.compile(
    r"^(?P<name>\S+?)\s*\((?P<uuid>[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
    r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})\)$"
)
_GROUP_LABEL = re.compile(r"^[^,:()]+:\s+")  # Essentials-style "Admins: Alex, Bob"
_TAG = re.compile(r"^\[[^\]]*\]\s*")  # "[AFK]Alex", "[HIDDEN] Bob"
_NAME_ARGUMENT = re.compile(r"^[^\s§\x00-\x1f\x7f]{1,64}$")

_KICKED = re.compile(r"\bkicked\b", re.IGNORECASE)
_BANNED = re.compile(r"\bbanned\b", re.IGNORECASE)
_UNBANNED = re.compile(
    r"\bunbanned\b|\bisn['’]?t banned\b|\bis not banned\b", re.IGNORECASE
)


def normalize_uuid(value: object) -> str | None:
    """Dashed lowercase UUID, or None when `value` is not a UUID."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid_module.UUID(value.strip()))
    except ValueError:
        return None


def parse_list_response(text: str) -> Roster | None:
    """Parse the reply of `list` / `list uuids`; None when unrecognised.

    The count in the header is authoritative; names are best effort.
    """
    if not isinstance(text, str):
        return None
    plain = strip_formatting(text)
    match = _LIST_HEADER.search(plain)
    if match is None:
        return None
    players = _parse_names(plain[match.end() :])
    counted = int(match["online"]) + int(match["hidden"] or 0)
    online = max(counted, len(players))
    return Roster(online=online, players=tuple(players), max_players=int(match["max"]))


def _parse_names(tail: str) -> list[OnlinePlayer]:
    players = []
    for raw_line in tail.replace("\r", "\n").split("\n"):
        line = _GROUP_LABEL.sub("", raw_line.strip())
        for token in line.split(","):
            player = _parse_player(token)
            if player is not None:
                players.append(player)
    return players


def _parse_player(token: str) -> OnlinePlayer | None:
    token = token.strip()
    while _TAG.match(token):
        token = _TAG.sub("", token, count=1).strip()
    if not token:
        return None
    match = _UUID_ENTRY.match(token)
    if match:
        return OnlinePlayer(name=match["name"], uuid=normalize_uuid(match["uuid"]))
    if any(char.isspace() for char in token) or len(token) > 64:
        return None  # not a player name (e.g. "and 3 more")
    return OnlinePlayer(name=token)


def roster_from_status(status: ServerStatus) -> Roster:
    """Roster from a status ping: the count plus the named sample entries
    (anonymous / zero-UUID / decorative entries are excluded)."""
    players = []
    for name, raw_id in status.sample:
        player_uuid = normalize_uuid(raw_id)
        if player_uuid is None or player_uuid == NIL_UUID:
            continue
        if not name or "§" in name or name.strip().lower() == ANONYMOUS_PLAYER:
            continue
        players.append(OnlinePlayer(name=name, uuid=player_uuid))
    return Roster(
        # Never below the players we can name, as on the RCON path.
        online=max(status.online, len(players)),
        players=tuple(players),
        max_players=status.max_players,
        source="ping",
        version=status.version_name or None,
        motd=status.motd or None,
    )


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


class MinecraftClient:
    """Thread-safe facade over RCON (preferred) and the status ping (fallback).

    Constructed with explicit values (main passes resolved settings) so tests
    never depend on the settings singleton. For tests, `rcon` (an RconClient
    stand-in), `pinger` (``pinger(timeout) -> ServerStatus``, raising
    PingError) and `clock` (monotonic seconds) can be injected.
    """

    def __init__(
        self,
        host: str,
        port: int,
        rcon_port: int,
        rcon_password: str | None,
        timeout: float = 5.0,
        *,
        rcon: RconClient | None = None,
        pinger: Callable[[float], ServerStatus] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.host = host or "127.0.0.1"
        self.port = int(port or DEFAULT_GAME_PORT)
        self.timeout = float(timeout)
        if rcon is None and rcon_password:
            rcon = RconClient(
                self.host,
                int(rcon_port or DEFAULT_RCON_PORT),
                rcon_password,
                connect_timeout=self.timeout,
                read_timeout=self.timeout,
            )
        self._rcon = rcon
        self._pinger = pinger or self._ping
        self._clock = clock
        self._state_lock = threading.Lock()
        self._list_uuids: bool | None = None  # does `list uuids` work here?
        self._info: ServerStatus | None = None
        self._info_checked_at: float | None = None
        self._auth_failed = False

    @property
    def rcon_enabled(self) -> bool:
        """True when an RCON password is configured."""
        return self._rcon is not None

    # ------------------------------------------------------------------
    # Roster
    # ------------------------------------------------------------------

    def get_roster(self) -> Roster | None:
        """RCON `list uuids` (falling back to `list`), else the status ping.
        None when neither answers or the answer cannot be parsed."""
        try:
            roster = self._rcon_roster()
            if roster is not None:
                return self._with_server_info(roster)
            status = self._try_ping(self.timeout)
            if status is None:
                return None
            self._remember_info(status)
            return roster_from_status(status)
        except Exception:
            logger.exception("Unexpected error while reading the player list")
            return None

    def _rcon_roster(self) -> Roster | None:
        if self._rcon is None:
            return None
        try:
            roster = self._list_via_rcon()
        except RconAuthError as exc:
            if not self._auth_failed:
                logger.warning("%s", exc)
            self._auth_failed = True
            return None
        except RconError as exc:
            logger.debug("RCON roster unavailable: %s", exc)
            return None
        except Exception:
            logger.exception("Unexpected RCON error; using the status ping")
            return None
        self._auth_failed = False
        if roster is None:
            logger.debug("Unrecognised `list` reply over RCON; using the status ping")
        return roster

    def _list_via_rcon(self) -> Roster | None:
        if self._list_uuids is not False:
            roster = parse_list_response(self._rcon.command("list uuids"))
            if roster is not None:
                if any(player.uuid for player in roster.players):
                    self._list_uuids = True
                elif roster.players:  # names without UUIDs: the argument is ignored
                    self._list_uuids = False
                return roster
        roster = parse_list_response(self._rcon.command("list"))
        if roster is not None and self._list_uuids is None:
            self._list_uuids = False
        return roster

    def _with_server_info(self, roster: Roster) -> Roster:
        info = self._server_info()
        if info is None:
            return roster
        return dataclasses.replace(
            roster, version=info.version_name or None, motd=info.motd or None
        )

    def _server_info(self) -> ServerStatus | None:
        """Status ping cached for SERVER_INFO_TTL; attempts are rate-limited
        too, and a failed refresh keeps the last known info."""
        with self._state_lock:
            now = self._clock()
            if (
                self._info_checked_at is not None
                and now - self._info_checked_at < SERVER_INFO_TTL
            ):
                return self._info
            self._info_checked_at = now
        status = self._try_ping(min(self.timeout, 2.0))
        if status is not None:
            self._remember_info(status)
        with self._state_lock:
            return self._info

    def _remember_info(self, status: ServerStatus) -> None:
        with self._state_lock:
            self._info = status
            self._info_checked_at = self._clock()

    def _try_ping(self, timeout: float) -> ServerStatus | None:
        try:
            return self._pinger(timeout)
        except PingError as exc:
            logger.debug("Status ping failed: %s", exc)
        except Exception:
            logger.exception("Unexpected error during the status ping")
        return None

    def _ping(self, timeout: float) -> ServerStatus:
        return ping_status(self.host, self.port, timeout=timeout)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def run_command(self, command: str) -> str:
        """Run a console command over RCON and return its (color-stripped) output.
        Raises RconUnavailable / RconError."""
        if self._rcon is None:
            raise RconUnavailable(
                "RCON is not configured (enable-rcon=true and rcon.password)"
            )
        return strip_formatting(self._rcon.command(command))

    def kick_player(self, name: str, reason: str | None = None) -> ActionResult:
        return self._moderate("kick", name, reason, _KICKED)

    def ban_player(self, name: str, reason: str | None = None) -> ActionResult:
        return self._moderate("ban", name, reason, _BANNED)

    def unban_player(self, name: str) -> ActionResult:
        """RCON `pardon <name>`."""
        return self._moderate("pardon", name, None, _UNBANNED)

    def _moderate(
        self, verb: str, name: str, reason: str | None, success: re.Pattern
    ) -> ActionResult:
        if not isinstance(name, str) or not _NAME_ARGUMENT.match(name):
            return ActionResult(False, f"Invalid player name: {name!r}")
        command = f"{verb} {name}"
        if _one_line(reason):
            command += f" {_one_line(reason)}"
        try:
            output = self.run_command(command)
        except RconError as exc:
            return ActionResult(False, f"Could not {verb} {name}: {exc}")
        message = output.strip() or f"No reply to `{verb} {name}`"
        return ActionResult(bool(success.search(output)), message, output=output)

    def say(self, message: str) -> bool:
        text = _one_line(message)
        if not text:
            return False
        try:
            self.run_command(f"say {text}")
        except RconError as exc:
            logger.debug("say failed: %s", exc)
            return False
        return True

    def request_stop(self) -> bool:
        """Send `stop` over RCON. True if the command was sent (the server may
        drop the connection before answering; that still counts as sent)."""
        if self._rcon is None:
            return False
        try:
            return self._rcon.send_final("stop")
        except RconError as exc:
            logger.warning("Could not send `stop` over RCON: %s", exc)
            return False

    def close(self) -> None:
        if self._rcon is not None:
            self._rcon.close()
