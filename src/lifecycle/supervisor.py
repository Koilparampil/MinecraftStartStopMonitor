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

"""Lifecycle supervisor: the only writer of lifecycle, roster and auto-stop state.

Message handling is synchronous. The runtime calls ``handle``/``on_tick`` only
from its single lifecycle thread; this module owns no threads or queues.

Locking: ``_state_lock`` guards the supervisor's fields for readers on other
threads (``get_snapshot`` and the properties). It is a *leaf* lock: the
process manager, the player/banlist managers and the event bus are never
called while it is held, so a subscriber or a web request can read a snapshot
at any moment without a lock-order deadlock. That is safe because the
lifecycle thread is the only writer: nothing can change the state between a
check and the commit that follows it.

Every commit changes its data first and bumps the snapshot revision last,
right before its notification is published, so a snapshot that carries a new
revision is always complete.

Every command gets an outcome (``command.outcome``, an ``ActionResult``) that
says why it was refused, or what is happening, before its waiter is released.
"""

import logging
import math
import threading
import time
from dataclasses import dataclass

from src.events import Event, EventBus
from src.lifecycle import auto_stop
from src.lifecycle.messages import (
    ActionResult,
    Command,
    LaunchRequest,
    LaunchResult,
    LifecycleState,
    PollRequest,
    ProcessIdentity,
    ProcessObservation,
    ProcessSnapshot,
    RestartCommand,
    Roster,
    RosterSample,
    StartCommand,
    StopCommand,
    StopRequest,
    StopResult,
)
from src.settings import settings

logger = logging.getLogger(__name__)

PLAYER_DATA_UNKNOWN = "unknown"  # no sample yet in this generation
PLAYER_DATA_OK = "ok"
PLAYER_DATA_UNAVAILABLE = "unavailable"  # the last sample failed

# Keys of get_snapshot(), in the documented order (docs/ARCHITECTURE.md).
SNAPSHOT_KEYS = (
    "revision",
    "state",
    "ready",
    "playerData",
    "playerCount",
    "maxPlayers",
    "players",
    "banned_players",
    "server",
    "startedAt",
    "autoStopAt",
    "restartPending",
)

# Messages carried by command outcomes (shown to the admin as-is).
STARTING_MESSAGE = "Starting the server."
STOPPING_MESSAGE = "Stopping the server."
RESTARTING_MESSAGE = "Restarting the server."
RESTART_CANCELLED_MESSAGE = "Restart cancelled: the server will stay stopped."
NOT_RUNNING_MESSAGE = "The server is not running."
RESTART_IN_PROGRESS_MESSAGE = "A restart is already in progress."
PROCESS_PRESENT_MESSAGE = (
    "A Minecraft server process is already running in the server directory; "
    "the monitor will adopt it within a few seconds."
)
DISCOVERY_FAILED_MESSAGE = (
    "Could not check for a running server process (see the monitor log)."
)
_START_REFUSED = {
    LifecycleState.STARTING: "The server is already starting.",
    LifecycleState.RUNNING: "The server is already running.",
    LifecycleState.STOPPING: "The server is stopping; start it again once it has stopped.",
}
_STOP_REFUSED = {
    LifecycleState.STOPPED: NOT_RUNNING_MESSAGE,
    LifecycleState.STARTING: "The server is still launching; stop it once it is running.",
    LifecycleState.STOPPING: "The server is already stopping.",
}
_RESTART_REFUSED = {
    LifecycleState.STARTING: "The server is still launching; restart it once it is running.",
    LifecycleState.STOPPING: _START_REFUSED[LifecycleState.STOPPING],
}


@dataclass(frozen=True)
class AutoStopRequest(StopRequest):
    """A stop decided by the auto-stop policy. Nobody is waiting for it, so the
    process backend must never force-kill a server it cannot stop gracefully."""


class OfflineEditCommand(Command):
    """Run ``edit()`` (an offline banned-players.json change) on the lifecycle
    thread, only while the server is STOPPED and no server process exists.

    Commands are handled one at a time, so a start (an auto-start wake) can
    never launch the server between that check and the write.
    """

    def __init__(self, edit):
        super().__init__()
        self.edit = edit


def settle(command, success: bool, message: str = "", outcome=None) -> None:
    """Record a command's outcome and release whoever waits for it."""
    command.result = success
    command.outcome = outcome if outcome is not None else ActionResult(success, message)
    command.done.set()


def _whole_seconds(seconds: float) -> int:
    return max(1, math.ceil(seconds))


class Supervisor:
    def __init__(
        self,
        process_manager,
        player_manager,
        banlist_manager,
        bus: EventBus,
        clock=None,
        wall_clock=None,
    ):
        self.process_manager = process_manager
        self.player_manager = player_manager
        self.banlist_manager = banlist_manager
        self.bus = bus
        self._clock = clock or time.monotonic  # deadlines, cooldowns
        self._wall_clock = wall_clock or time.time  # snapshot timestamps

        # stop -> start debounce: a start accepted sooner commits STARTING at
        # once and launches when it ends. (No start -> start cooldown: a start
        # needs STOPPED, which comes after a stop commit, so this one is later.)
        self.server_stopping_cooldown = 5
        # None reads settings.pollingRate at call time; tests may pin a value.
        self.poll_interval: float | None = None
        self.last_server_started_time = float("-inf")
        self.last_server_stopped_time = float("-inf")
        self._deferred_launch: LaunchRequest | None = None
        self._launch_due_at: float | None = None

        self._state = LifecycleState.STOPPED
        self._identity: ProcessIdentity | None = None
        self._generation = 0
        self._revision = 0
        self._state_lock = threading.RLock()
        self._player_data = PLAYER_DATA_UNKNOWN
        self._stopped_reconciled = False
        self._restart_pending = False
        self._reset_server_info_locked()

        self._auto_stop = auto_stop.AutoStopState()
        self._poll_due_at: float | None = None
        self._poll_inflight = False
        self._poll_seq = 0

    # ------------------------------------------------------------------
    # Public read access
    # ------------------------------------------------------------------

    @property
    def generation(self) -> int:
        with self._state_lock:
            return self._generation

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def ready(self) -> bool:
        with self._state_lock:
            return self._ready

    @property
    def restart_pending(self) -> bool:
        with self._state_lock:
            return self._restart_pending

    @property
    def auto_stop_deadline(self) -> float | None:
        """The armed auto-stop deadline in the injected (monotonic) clock."""
        with self._state_lock:
            return self._auto_stop.deadline

    def get_snapshot(self) -> dict:
        """One committed view of lifecycle, roster and server info."""
        with self._state_lock:
            snapshot = {
                "revision": self._revision,
                "state": self._state,
                "ready": self._ready,
                "playerData": self._player_data,
                "playerCount": self._player_count_locked(),
                "maxPlayers": self._max_players,
                "players": [],
                "banned_players": [],
                "server": dict(self._server_info),
                "startedAt": self._started_at_wall,
                "autoStopAt": self._auto_stop_at_locked(),
                "restartPending": self._restart_pending,
            }
        snapshot["players"] = self._read_list(self.player_manager.get_all_players)
        snapshot["banned_players"] = self._read_list(
            self.banlist_manager.get_banned_players
        )
        return snapshot

    # ------------------------------------------------------------------
    # Synchronous message handling (lifecycle thread only)
    # ------------------------------------------------------------------

    def handle(self, item) -> list:
        handlers = (
            (StartCommand, self._handle_start_command),
            (StopCommand, self._handle_stop_command),
            (RestartCommand, self._handle_restart_command),
            (OfflineEditCommand, self._handle_offline_edit),
            (LaunchResult, self._handle_launch_result),
            (StopResult, self._handle_stop_result),
            (ProcessObservation, self._handle_process_observation),
            (RosterSample, self._handle_roster_sample),
        )
        for message_type, handler in handlers:
            if isinstance(item, message_type):
                return handler(item)
        logger.warning(f"Ignoring unknown lifecycle observation: {item!r}")
        if isinstance(item, Command):
            settle(item, False, "Unknown lifecycle command.")
        return []

    def next_timeout(self) -> float | None:
        now = self._clock()
        with self._state_lock:
            deadlines = self._deadlines_locked()
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def _deadlines_locked(self) -> list:
        if self._state == LifecycleState.STARTING and self._launch_due_at is not None:
            return [self._launch_due_at]
        if self._state != LifecycleState.RUNNING:
            return []
        deadlines = []
        if self._poll_due_at is not None and not self._poll_inflight:
            deadlines.append(self._poll_due_at)
        if self._auto_stop.deadline is not None:
            deadlines.append(self._auto_stop.deadline)
        return deadlines

    def on_tick(self) -> list:
        now = self._clock()
        requests = self._due_launch(now)
        with self._state_lock:
            generation = self._generation
            identity = self._identity
            poll_due = self._poll_due_at
            should_poll = (
                self._state == LifecycleState.RUNNING
                and identity is not None
                and not self._poll_inflight
                and poll_due is not None
                and now >= poll_due
            )
            if should_poll:
                self._poll_inflight = True
                self._poll_due_at = None
                self._poll_seq += 1
                seq = self._poll_seq
            auto_stop_due = self._auto_stop.deadline
        if should_poll:
            requests.append(PollRequest(generation, identity, seq))
        if auto_stop_due is not None and now >= auto_stop_due:
            requests.extend(self._expire_auto_stop(generation))
        return requests

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _handle_start_command(self, command) -> list:
        reason = self._start_rejection()
        if reason is not None:
            logger.warning(f"Start refused: {reason}")
            settle(command, False, reason)
            return []
        # Within the stop -> start debounce the start is still accepted (a
        # player told "starting" must not be lied to): STARTING is committed
        # now, so the listener releases the port, and the launch follows.
        delay = self._debounce_remaining()
        request = self._begin_start(delay)
        message = STARTING_MESSAGE
        if delay > 0:
            message = f"Starting the server in {_whole_seconds(delay)} s."
        # The command is accepted before the listener is asked to release the
        # port, so the auto-start thread never waits on this command.
        settle(command, True, message)
        logger.info(message.replace("the server", "the Minecraft server"))
        self._publish_lifecycle(LifecycleState.STARTING, request.generation)
        return [] if delay > 0 else [request]

    def _debounce_remaining(self) -> float:
        with self._state_lock:
            stopped_ago = self._clock() - self.last_server_stopped_time
        return max(0.0, self.server_stopping_cooldown - stopped_ago)

    def _due_launch(self, now: float) -> list:
        """The deferred launch of a debounced start, once it is due."""
        with self._state_lock:
            request = self._deferred_launch
            if (
                request is None
                or self._state != LifecycleState.STARTING
                or request.generation != self._generation
                or now < self._launch_due_at
            ):
                return []
            self._deferred_launch = None
            self._launch_due_at = None
        reason = self._server_process_reason()
        if reason is not None:
            # Someone started a server meanwhile: let detection adopt it.
            logger.warning(f"Delayed start cancelled: {reason}")
            self._commit_down()
            return []
        return [request]

    def _handle_stop_command(self, command: StopCommand) -> list:
        if self._cancel_pending_restart():
            settle(command, True, RESTART_CANCELLED_MESSAGE)
            return []
        return self._request_stop(command, restart=False)

    def _handle_restart_command(self, command: RestartCommand) -> list:
        if self.state == LifecycleState.STOPPED:
            return self._handle_start_command(command)
        return self._request_stop(command, restart=True)

    def _start_rejection(self) -> str | None:
        reason = self._start_state_rejection()
        if reason is None:
            reason = self._server_process_reason()
        return reason

    def _start_state_rejection(self) -> str | None:
        with self._state_lock:
            state = self._state
            restarting = self._restart_pending
        if state == LifecycleState.STOPPED:
            return None
        if restarting:
            return RESTART_IN_PROGRESS_MESSAGE
        return _START_REFUSED.get(state, _START_REFUSED[LifecycleState.RUNNING])

    def _begin_start(self, delay: float = 0.0) -> LaunchRequest:
        with self._state_lock:
            self._generation += 1
            self._state = LifecycleState.STARTING
            self._identity = None
            self._player_data = PLAYER_DATA_UNKNOWN
            self._restart_pending = False
            self._auto_stop = auto_stop.AutoStopState()
            self._poll_due_at = None
            self._poll_inflight = False
            request = LaunchRequest(self._generation)
            deferred = delay > 0
            self._deferred_launch = request if deferred else None
            self._launch_due_at = self._clock() + delay if deferred else None
            return request

    def _request_stop(self, command, restart: bool) -> list:
        with self._state_lock:
            reason = self._stop_rejection_locked(restart)
            request = None if reason else self._begin_stop_locked(restart=restart)
        if request is None:
            logger.warning(f"{'Restart' if restart else 'Stop'} refused: {reason}")
            settle(command, False, reason)
            return []
        message = RESTARTING_MESSAGE if restart else STOPPING_MESSAGE
        logger.info(message.replace("the server", "the Minecraft server"))
        self._publish_lifecycle(LifecycleState.STOPPING, request.generation)
        settle(command, True, message)
        return [request]

    def _stop_rejection_locked(self, restart: bool) -> str | None:
        # No stop -> stop cooldown: a stop needs RUNNING, which already
        # proves the previous stop is over.
        if self._state == LifecycleState.RUNNING and self._identity is not None:
            return None
        if self._state == LifecycleState.STOPPING and self._restart_pending:
            return RESTART_IN_PROGRESS_MESSAGE
        refusals = _RESTART_REFUSED if restart else _STOP_REFUSED
        return refusals.get(self._state, NOT_RUNNING_MESSAGE)

    def _begin_stop_locked(
        self, restart: bool = False, auto: bool = False
    ) -> StopRequest | None:
        identity = self._identity
        if identity is None:
            return None
        self._state = LifecycleState.STOPPING
        self._restart_pending = restart
        self._auto_stop = auto_stop.cancelled(self._auto_stop)
        self._poll_due_at = None
        self._poll_inflight = False
        request_type = AutoStopRequest if auto else StopRequest
        return request_type(self._generation, identity)

    def _begin_auto_stop(self) -> list:
        with self._state_lock:
            request = self._begin_stop_locked(auto=True)
        if request is None:
            return []
        self._publish_lifecycle(LifecycleState.STOPPING, request.generation)
        return [request]

    def _cancel_pending_restart(self) -> bool:
        """A stop requested during a restart's stop keeps the server stopped."""
        with self._state_lock:
            if self._state != LifecycleState.STOPPING or not self._restart_pending:
                return False
            self._restart_pending = False
            generation = self._generation
        logger.info("Stop requested during a restart: the server will stay stopped")
        self._publish_lifecycle(LifecycleState.STOPPING, generation)
        return True

    def _handle_offline_edit(self, command: OfflineEditCommand) -> list:
        state = self.state
        if state == LifecycleState.STOPPED:
            reason = self._server_process_reason()
        else:
            reason = (
                f"The server is {state.lower()}; bans can be changed while it is "
                "running (over RCON) or stopped."
            )
        if reason is not None:
            settle(command, False, reason)
            return []
        # Lifecycle thread, outside the state lock: no start can interleave.
        outcome = self._call(command.edit)
        if not isinstance(outcome, ActionResult):
            outcome = ActionResult(
                bool(outcome),
                ""
                if outcome
                else "The ban list could not be changed; see the monitor log.",
            )
        settle(command, outcome.success, outcome.message, outcome)
        return []

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def _handle_launch_result(self, result: LaunchResult) -> list:
        with self._state_lock:
            if (
                result.generation != self._generation
                or self._state != LifecycleState.STARTING
            ):
                return []
        if result.identity is None:
            logger.error("The Minecraft server could not be launched")
            self._commit_down()
        else:
            self._commit_started(result.identity)
        return []

    def _handle_stop_result(self, result: StopResult) -> list:
        with self._state_lock:
            if (
                result.generation != self._generation
                or self._state != LifecycleState.STOPPING
            ):
                return []
        if result.stopped is True:
            return self._finish_stop()
        if result.stopped is False:
            self._recover_from_failed_stop()
        # None means a shutdown is still pending; detection confirms the exit.
        return []

    def _recover_from_failed_stop(self):
        with self._state_lock:
            if self._state != LifecycleState.STOPPING:
                return
            self._state = LifecycleState.RUNNING
            self._restart_pending = False
            self._poll_due_at = self._clock()
            self._poll_inflight = False
            generation = self._generation
        logger.warning(
            "The server is still running after the stop attempt; resuming polling"
        )
        self._publish_lifecycle(LifecycleState.RUNNING, generation)

    def _handle_process_observation(self, observation: ProcessObservation) -> list:
        with self._state_lock:
            if observation.generation != self._generation:
                return []
            state = self._state
        if state == LifecycleState.STARTING:
            return []
        if state == LifecycleState.STOPPED:
            return self._handle_stopped_snapshot(observation.snapshot)
        return self._handle_active_snapshot(observation.snapshot)

    def _handle_stopped_snapshot(self, snapshot: ProcessSnapshot) -> list:
        replacement = None
        stale_tracking = None
        reconcile = False
        with self._state_lock:
            if self._state != LifecycleState.STOPPED:
                return []
            if snapshot.tracked is not None:
                if snapshot.found is not None and snapshot.found.matches(
                    snapshot.tracked
                ):
                    # Only adopt a tracked PID when this scan actually found
                    # it as the server; mere PID liveness is not proof.
                    replacement = snapshot.tracked
                else:
                    stale_tracking = snapshot.tracked
            if replacement is None and snapshot.found is not None:
                replacement = snapshot.found
            if replacement is None and not self._stopped_reconciled:
                reconcile = True
        if stale_tracking is not None:
            self._call(self.process_manager.clear_identity, stale_tracking)
        if replacement is not None:
            self._adopt(replacement)
        elif reconcile:
            self._reconcile_stopped()
        return []

    def _handle_active_snapshot(self, snapshot: ProcessSnapshot) -> list:
        replacement = None
        with self._state_lock:
            current = self._identity
            if current is None:
                return []
            if snapshot.tracked is None or not snapshot.tracked.matches(current):
                return []  # stale observation from an older generation
            if snapshot.tracked_running:
                return []
            if snapshot.found is not None and not snapshot.found.matches(current):
                replacement = snapshot.found
        logger.info(f"Minecraft server process {current.pid} exited")
        return self._finish_stop(replacement)

    def _finish_stop(self, replacement: ProcessIdentity | None = None) -> list:
        """Commit an exit, then adopt a replacement or carry out a pending restart."""
        restart = self.restart_pending
        if restart and (replacement is not None or self._server_process_present()):
            logger.warning(
                "Restart: another server process is running in the server "
                "directory; adopting it instead of launching a second one"
            )
            restart = False
        if not restart:
            with self._state_lock:
                self._restart_pending = False
        self._commit_down()
        if replacement is not None:
            self._adopt(replacement)
            return []
        if restart:
            return self._restart_start()
        return []

    def _restart_start(self) -> list:
        # Bypasses the start cooldowns on purpose: the stop was confirmed and
        # this start was requested together with it.
        request = self._begin_start()
        logger.info("Restart: the server stopped; starting it again")
        self._publish_lifecycle(LifecycleState.STARTING, request.generation)
        return [request]

    def _adopt(self, identity: ProcessIdentity):
        self._call(self.process_manager.set_known_identity, identity)
        logger.info(f"Adopted the running Minecraft server process {identity.pid}")
        self._commit_started(identity, adopted=True)

    def _handle_roster_sample(self, sample: RosterSample) -> list:
        with self._state_lock:
            if sample.generation != self._generation:
                return []  # late response from a previous generation
            if self._state != LifecycleState.RUNNING:
                return []
            if self._identity is None or not self._identity.matches(sample.identity):
                return []
            self._poll_inflight = False
            self._poll_due_at = self._clock() + self._poll_interval()
            generation = self._generation
        if sample.roster is None:
            return self._apply_unavailable(generation)
        # Outside the state lock: PlayerManager publishes PLAYER_JOINED/LEFT.
        self._call(self.player_manager.update_from_roster, sample.roster)
        return self._apply_roster(sample.roster, sample.seq, generation)

    def _apply_unavailable(self, generation: int) -> list:
        with self._state_lock:
            # RCON and the ping both failed: keep the last-known roster and
            # never let an empty-looking failure trigger auto-stop.
            self._player_data = PLAYER_DATA_UNAVAILABLE
            self._auto_stop, _decision = auto_stop.on_unavailable(self._auto_stop)
        self._publish_status(generation)
        return []

    def _apply_roster(self, roster: Roster, seq: int, generation: int) -> list:
        # Roster.online is authoritative (hidden players count); never trust
        # it below the number of players we can see by name.
        online = max(int(roster.online or 0), len(roster.players))
        with self._state_lock:
            armed_before = self._auto_stop.deadline is not None
            self._player_data = PLAYER_DATA_OK
            self._ready = True
            self._online_count = online
            self._remember_server_info_locked(roster)
            self._auto_stop, decision = auto_stop.on_roster(
                self._auto_stop,
                config=self._auto_stop_config(),
                now=self._clock(),
                generation=generation,
                player_count=online,
                sample_seq=seq,
                started_at=self.last_server_started_time,
            )
            if decision == auto_stop.AutoStopDecision.REVALIDATE:
                self._poll_due_at = self._clock()
            armed_after = self._auto_stop.deadline is not None
        self._log_auto_stop_change(armed_before, armed_after, online)
        self._publish_status(generation)
        if decision == auto_stop.AutoStopDecision.STOP:
            logger.info(
                "Auto-stop: a fresh sample confirmed the server is empty; stopping it"
            )
            return self._begin_auto_stop()
        return []

    # ------------------------------------------------------------------
    # State commits
    # ------------------------------------------------------------------

    def _commit_started(self, identity: ProcessIdentity, adopted: bool = False):
        now = self._clock()
        started_at = self._started_wall_time(identity, adopted)
        with self._state_lock:
            if self._state != LifecycleState.STARTING:
                self._generation += 1
            self._state = LifecycleState.RUNNING
            self._identity = identity
            self._player_data = PLAYER_DATA_UNKNOWN
            self._stopped_reconciled = False
            self._restart_pending = False
            self._auto_stop = auto_stop.AutoStopState()
            self._reset_server_info_locked()
            self._started_at_wall = started_at
            self.last_server_started_time = now
            self._poll_due_at = now
            self._poll_inflight = False
            self._deferred_launch = self._launch_due_at = None
            generation = self._generation
        revision = self._bump_revision()
        self._publish(
            Event.SERVER_STARTED,
            {
                "revision": revision,
                "pid": identity.pid,
                "generation": generation,
                "state": LifecycleState.RUNNING,
            },
        )

    def _commit_down(self):
        now = self._clock()
        with self._state_lock:
            stopped = self._identity
            self._state = LifecycleState.STOPPED
            self._identity = None
            self._generation += 1
            self._player_data = PLAYER_DATA_UNKNOWN
            self._stopped_reconciled = True
            self._auto_stop = auto_stop.AutoStopState()
            self._reset_server_info_locked()
            self.last_server_stopped_time = now
            self._poll_due_at = None
            self._poll_inflight = False
            self._deferred_launch = self._launch_due_at = None
            generation = self._generation
        # Release players and tracking before anyone hears about the stop.
        self._call(self.player_manager.mark_all_offline)
        if stopped is not None:
            self._call(self.process_manager.clear_identity, stopped)
        revision = self._bump_revision()
        self._publish(
            Event.SERVER_STOPPED,
            {
                "revision": revision,
                "pid": stopped.pid if stopped is not None else None,
                "generation": generation,
                "state": LifecycleState.STOPPED,
            },
        )

    def _reconcile_stopped(self):
        """Make the persisted roster consistent with a stopped server.

        Runs once at startup and arms auto-start: a server that exits between
        the startup scan and the first adoption would otherwise leave the game
        port unbound, because no SERVER_STOPPED would ever be committed.
        """
        with self._state_lock:
            if self._state != LifecycleState.STOPPED or self._stopped_reconciled:
                return
            self._stopped_reconciled = True
            generation = self._generation
        self._call(self.player_manager.mark_all_offline)
        revision = self._bump_revision()
        self._publish(
            Event.SERVER_STOPPED,
            {
                "revision": revision,
                "pid": None,
                "generation": generation,
                "state": LifecycleState.STOPPED,
            },
        )

    def _bump_revision(self) -> int:
        # Monotonic revision for ordered web/SSE snapshots.
        with self._state_lock:
            self._revision += 1
            return self._revision

    def _publish_lifecycle(self, state: str, generation: int):
        revision = self._bump_revision()
        self._publish(
            Event.LIFECYCLE_CHANGED,
            {"state": state, "generation": generation, "revision": revision},
        )

    def _publish_status(self, generation: int):
        with self._state_lock:
            self._revision += 1
            payload = {
                "revision": self._revision,
                "state": self._state,
                "playerData": self._player_data,
                "playerCount": self._player_count_locked(),
                "generation": generation,
            }
        self._publish(Event.SERVER_STATUS, payload)

    def _publish(self, event_type: str, payload: dict):
        try:
            self.bus.publish(event_type, payload)
        except Exception:
            logger.exception(f"Publishing {event_type} failed")

    # ------------------------------------------------------------------
    # Snapshot fields
    # ------------------------------------------------------------------

    def _reset_server_info_locked(self):
        self._ready = False
        self._online_count = 0
        self._max_players: int | None = None
        self._server_info = {"version": None, "motd": None, "source": None}
        self._started_at_wall: float | None = None

    def _remember_server_info_locked(self, roster: Roster):
        # Version/MOTD come from a cached status ping and may be missing from
        # one sample; keep the last known value within the generation.
        if roster.max_players is not None:
            self._max_players = roster.max_players
        if roster.version is not None:
            self._server_info["version"] = roster.version
        if roster.motd is not None:
            self._server_info["motd"] = roster.motd
        self._server_info["source"] = roster.source

    def _player_count_locked(self) -> int:
        if self._state != LifecycleState.RUNNING:
            return 0
        return self._online_count

    def _auto_stop_at_locked(self) -> float | None:
        deadline = self._auto_stop.deadline
        if deadline is None or self._state != LifecycleState.RUNNING:
            return None
        return self._wall_clock() + (deadline - self._clock())

    def _started_wall_time(self, identity: ProcessIdentity, adopted: bool) -> float:
        now = self._wall_clock()
        if not adopted:
            return now
        # An adopted server has been up since its process was created. The
        # identity's key is not wall-clock time (it must survive clock steps),
        # so ask the process manager, when it can tell.
        started_at = getattr(self.process_manager, "started_at", None)
        created = self._call(started_at, identity) if callable(started_at) else None
        if isinstance(created, (int, float)) and 0 < created <= now:
            return float(created)
        return now

    # ------------------------------------------------------------------
    # Discovery and auto-stop helpers
    # ------------------------------------------------------------------

    def _server_process_reason(self) -> str | None:
        """Why launching (or editing server files) now is unsafe, if it is."""
        try:
            present = self._find_server_process()
        except Exception:
            # Never risk two servers on one world.
            logger.exception("Process discovery failed")
            return DISCOVERY_FAILED_MESSAGE
        return PROCESS_PRESENT_MESSAGE if present else None

    def _server_process_present(self) -> bool:
        return self._server_process_reason() is not None

    def _find_server_process(self) -> bool:
        pm = self.process_manager
        tracked = pm.tracked_identity
        if tracked is not None and pm.identity_is_running(tracked):
            return True
        # No live tracked identity is not proof that no server exists: the
        # first detection scan may not have adopted it yet.
        if pm.find_process_identity() is not None:
            return True
        if tracked is not None:
            pm.clear_identity(tracked)
        return False

    def _poll_interval(self) -> float:
        if self.poll_interval is not None:
            return float(self.poll_interval)
        return float(settings.pollingRate)

    def _auto_stop_config(self) -> auto_stop.AutoStopConfig:
        return auto_stop.AutoStopConfig(
            enabled=bool(settings.autoStop),
            delay=float(settings.autoStopDelay),
        )

    def _log_auto_stop_change(self, armed_before: bool, armed_after: bool, online: int):
        if armed_after and not armed_before:
            logger.info(
                f"The server is empty; auto-stop in {float(settings.autoStopDelay):g}s "
                "unless a player joins"
            )
        elif armed_before and not armed_after and online > 0:
            logger.info(f"Auto-stop cancelled: {online} player(s) online")

    def _expire_auto_stop(self, generation: int) -> list:
        with self._state_lock:
            if self._state != LifecycleState.RUNNING or self._generation != generation:
                self._auto_stop = auto_stop.cancelled(self._auto_stop)
                return []
            self._auto_stop, decision = auto_stop.on_expiry(
                self._auto_stop,
                config=self._auto_stop_config(),
                generation=generation,
                poll_seq=self._poll_seq,
            )
            revalidate = decision == auto_stop.AutoStopDecision.REVALIDATE
            if revalidate and not self._poll_inflight:
                self._poll_due_at = self._clock()
        if revalidate:
            logger.info("Auto-stop delay elapsed; confirming with a fresh sample")
        return []

    # ------------------------------------------------------------------
    # Collaborator calls never break a commit
    # ------------------------------------------------------------------

    @staticmethod
    def _call(function, *args):
        try:
            return function(*args)
        except Exception:
            name = getattr(function, "__qualname__", repr(function))
            logger.exception(f"Lifecycle collaborator call {name} failed")
            return None

    @staticmethod
    def _read_list(function) -> list:
        try:
            return list(function() or [])
        except Exception:
            name = getattr(function, "__qualname__", repr(function))
            logger.exception(f"Snapshot read {name} failed")
            return []
