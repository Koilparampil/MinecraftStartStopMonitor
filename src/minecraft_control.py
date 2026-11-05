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

"""Public application facade for Minecraft server lifecycle control.

Start/stop/restart go through the lifecycle runtime to the supervisor, and
snapshots come from the supervisor. Moderation follows the committed state:

- RUNNING with RCON: kick, ban, unban and console commands over RCON. Bans are
  verified against ``banned-players.json``, which the server writes on
  ``/ban`` and ``/pardon``.
- STOPPED: ban/unban edit ``banned-players.json`` offline; the server reads it
  on its next start. The edit runs as a lifecycle command, so no start (an
  auto-start wake) can launch the server between the check and the write.
- Anything else fails with a clear message: the file is never edited while a
  server process may be running.

Start/stop/restart return an ``ActionResult`` (truthy when accepted) whose
message says what happens next or why the command was refused.

No method raises: every failure is ``ActionResult(success=False, message=...)``.
"""

import logging
import re
import time

from src.events import EventBus
from src.lifecycle.messages import (
    ActionResult,
    LifecycleState,
    RestartCommand,
    StartCommand,
    StopCommand,
)
from src.lifecycle.runtime import LifecycleRuntime
from src.lifecycle.supervisor import OfflineEditCommand, Supervisor
from src.minecraft.client import RconError

logger = logging.getLogger(__name__)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_NAME_LENGTH = 64


def _failure(message: str) -> ActionResult:
    return ActionResult(success=False, message=message)


def _clean_name(name) -> tuple[str | None, ActionResult | None]:
    if not isinstance(name, str) or not name.strip():
        return None, _failure("A player name is required.")
    name = name.strip()
    if (
        len(name) > _MAX_NAME_LENGTH
        or _CONTROL_CHARACTERS.search(name)
        or any(character.isspace() for character in name)
    ):
        return None, _failure(f"Invalid player name: {name!r}")
    return name, None


def _clean_reason(reason) -> str | None:
    if reason is None:
        return None
    text = " ".join(_CONTROL_CHARACTERS.sub(" ", str(reason)).split())
    return text or None


def _as_result(value, failure_message: str) -> ActionResult:
    if isinstance(value, ActionResult):
        return value
    if value:
        return ActionResult(success=True)
    return _failure(failure_message)


class MinecraftController:
    # The server writes banned-players.json while it handles /ban; give
    # servers that save it asynchronously a moment before failing the check.
    BAN_VERIFY_ATTEMPTS = 5
    BAN_VERIFY_DELAY = 0.2

    def __init__(
        self,
        client,
        process_manager,
        player_manager,
        banlist_manager,
        bus: EventBus,
        clock=None,
        wall_clock=None,
    ):
        self.client = client
        self.process_manager = process_manager
        self.player_manager = player_manager
        self.banlist_manager = banlist_manager
        self.bus = bus
        self._supervisor = Supervisor(
            process_manager=process_manager,
            player_manager=player_manager,
            banlist_manager=banlist_manager,
            bus=bus,
            clock=clock,
            wall_clock=wall_clock,
        )
        self._runtime = LifecycleRuntime(
            supervisor=self._supervisor,
            client=client,
            process_manager=process_manager,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the supervisor and its workers. Idempotent."""
        self._runtime.start()

    def close(self) -> None:
        """Stop the workers and release process resources. Idempotent.

        Never stops the Minecraft server."""
        self._runtime.close()

    def start_server(self) -> ActionResult:
        logger.info("Minecraft server start requested")
        return self._runtime.submit_command(StartCommand())

    def stop_server(self) -> ActionResult:
        logger.info("Minecraft server stop requested")
        return self._runtime.submit_command(StopCommand())

    def restart_server(self) -> ActionResult:
        logger.info("Minecraft server restart requested")
        return self._runtime.submit_command(RestartCommand())

    @property
    def generation(self) -> int:
        return self._supervisor.generation

    @property
    def state(self) -> str:
        return self._supervisor.state

    def get_snapshot(self) -> dict:
        return self._supervisor.get_snapshot()

    def get_banned_players(self) -> list[dict]:
        try:
            return list(self.banlist_manager.get_banned_players() or [])
        except Exception:
            logger.exception("Reading the ban list failed")
            return []

    # ------------------------------------------------------------------
    # Moderation and console
    # ------------------------------------------------------------------

    def kick_player(self, name: str, reason: str | None = None) -> ActionResult:
        name, error = _clean_name(name)
        if error is not None:
            return error
        blocked = self._rcon_required("kick players")
        if blocked is not None:
            return blocked
        result = self._client_action(
            self.client.kick_player,
            name,
            _clean_reason(reason),
            failure=f"Could not kick {name}.",
        )
        if result.success:
            logger.info(f"Kicked {name}")
        return result

    def ban_player(self, name: str, reason: str | None = None) -> ActionResult:
        name, error = _clean_name(name)
        if error is not None:
            return error
        reason = _clean_reason(reason)
        if self.state == LifecycleState.STOPPED:
            return self._edit_offline(self._ban_offline, name, reason)
        blocked = self._ban_change_blocked()
        if blocked is not None:
            return blocked
        result = self._client_action(
            self.client.ban_player, name, reason, failure=f"Could not ban {name}."
        )
        return self._verify_ban(name, result, banned=True)

    def unban_player(self, name: str) -> ActionResult:
        name, error = _clean_name(name)
        if error is not None:
            return error
        if self.state == LifecycleState.STOPPED:
            return self._edit_offline(self._unban_offline, name)
        blocked = self._ban_change_blocked()
        if blocked is not None:
            return blocked
        result = self._client_action(
            self.client.unban_player, name, failure=f"Could not unban {name}."
        )
        return self._verify_ban(name, result, banned=False)

    def run_console_command(self, command: str) -> ActionResult:
        command = command.strip() if isinstance(command, str) else ""
        if not command:
            return _failure("Enter a command to run.")
        if _CONTROL_CHARACTERS.search(command):
            return _failure("Console commands must be a single line.")
        blocked = self._rcon_required("run console commands")
        if blocked is not None:
            return blocked
        try:
            output = self.client.run_command(command)
        except RconError as e:
            return _failure(f"The command failed: {e}")
        except Exception:
            logger.exception("Console command failed")
            return _failure("The command failed unexpectedly; see the monitor log.")
        logger.info(f"Ran console command: {command}")
        return ActionResult(
            success=True,
            message=f"Ran: {command}",
            output="" if output is None else str(output),
        )

    # ------------------------------------------------------------------
    # Moderation helpers
    # ------------------------------------------------------------------

    def _rcon_enabled(self) -> bool:
        try:
            return bool(self.client.rcon_enabled)
        except Exception:
            return False

    def _rcon_required(self, action: str) -> ActionResult | None:
        state = self.state
        if state != LifecycleState.RUNNING:
            return _failure(
                f"The server is {state.lower()}; it must be running to {action}."
            )
        if not self._rcon_enabled():
            return _failure(
                f"RCON is not configured, so the monitor cannot {action}. Set "
                "enable-rcon=true and rcon.password in server.properties."
            )
        return None

    def _ban_change_blocked(self) -> ActionResult | None:
        state = self.state
        if state != LifecycleState.RUNNING:
            return _failure(
                f"The server is {state.lower()}; bans can be changed while it is "
                "running (over RCON) or stopped."
            )
        if not self._rcon_enabled():
            return _failure(
                "RCON is not configured, so bans cannot be changed while the server "
                "is running. Enable RCON (enable-rcon=true and rcon.password in "
                "server.properties) or stop the server first."
            )
        return None

    def _client_action(self, method, *args, failure: str) -> ActionResult:
        try:
            result = method(*args)
        except RconError as e:
            return _failure(f"{failure} {e}")
        except Exception:
            logger.exception(failure)
            return _failure(f"{failure} See the monitor log.")
        return _as_result(result, failure)

    def _verify_ban(
        self, name: str, result: ActionResult, banned: bool
    ) -> ActionResult:
        """Success iff banned-players.json agrees with the requested state."""
        attempts = self.BAN_VERIFY_ATTEMPTS if result.success else 1
        for attempt in range(attempts):
            if attempt:
                time.sleep(self.BAN_VERIFY_DELAY)
            self._call(self.banlist_manager.refresh)
            if self._call(self.banlist_manager.is_banned, name) is banned:
                verb = "Banned" if banned else "Unbanned"
                logger.info(f"{verb} {name}")
                return ActionResult(
                    success=True,
                    message=result.message or f"{verb} {name}.",
                    output=result.output,
                    details=dict(result.details),
                )
        if not result.success:
            return result
        action = "ban" if banned else "unban"
        return ActionResult(
            success=False,
            message=f"The server did not record the {action} of {name} in banned-players.json.",
            output=result.output,
        )

    def _edit_offline(self, edit, *args) -> ActionResult:
        # On the lifecycle thread: it re-checks STOPPED and that no server
        # process exists, and no start can run until the edit is written.
        return self._runtime.submit_command(OfflineEditCommand(lambda: edit(*args)))

    def _ban_offline(self, name: str, reason: str | None) -> ActionResult:
        uuid = self._call(self.player_manager.find_uuid, name) or self._call(
            self.banlist_manager.lookup_uuid, name
        )
        result = self._call(self.banlist_manager.add_ban_offline, name, uuid, reason)
        result = _as_result(result, f"Could not ban {name}.")
        if result.success:
            logger.info(f"Banned {name} (offline edit)")
        return result

    def _unban_offline(self, name: str) -> ActionResult:
        result = self._call(self.banlist_manager.remove_ban_offline, name)
        result = _as_result(result, f"Could not unban {name}.")
        if result.success:
            logger.info(f"Unbanned {name} (offline edit)")
        return result

    @staticmethod
    def _call(function, *args):
        try:
            return function(*args)
        except Exception:
            name = getattr(function, "__qualname__", repr(function))
            logger.exception(f"{name} failed")
            return None
