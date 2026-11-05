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
"""

import logging
import threading
import time

from src.events import Event, EventBus
from src.lifecycle import auto_stop
from src.lifecycle.messages import (
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

        self.server_starting_cooldown = 5
        self.server_stopping_cooldown = 5
        # None reads settings.pollingRate at call time; tests may pin a value.
        self.poll_interval: float | None = None
        self.last_server_started_time = float("-inf")
        self.last_server_stopped_time = float("-inf")

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
            (LaunchResult, self._handle_launch_result),
            (StopResult, self._handle_stop_result),
            (ProcessObservation, self._handle_process_observation),
            (RosterSample, self._handle_roster_sample),
        )
        for message_type, handler in handlers:
            if isinstance(item, message_type):
                return handler(item)
        logger.warning(f"Ignoring unknown lifecycle observation: {item!r}")
        return []

    def next_timeout(self) -> float | None:
        now = self._clock()
        with self._state_lock:
            if self._state != LifecycleState.RUNNING:
                return None
            deadlines = []
            if self._poll_due_at is not None and not self._poll_inflight:
                deadlines.append(self._poll_due_at)
            if self._auto_stop.deadline is not None:
                deadlines.append(self._auto_stop.deadline)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def on_tick(self) -> list:
        requests = []
        now = self._clock()
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
        request = self._begin_start() if self._start_allowed() else None
        command.result = request is not None
        # The command is accepted before the listener is asked to release the
        # port, so the auto-start thread never waits on this command.
        command.done.set()
        if request is None:
            return []
        logger.info("Starting the Minecraft server")
        self._publish_lifecycle(LifecycleState.STARTING, request.generation)
        return [request]

    def _handle_stop_command(self, command: StopCommand) -> list:
        if self._cancel_pending_restart():
            command.result = True
            command.done.set()
            return []
        return self._request_stop(command, restart=False)

    def _handle_restart_command(self, command: RestartCommand) -> list:
        state = self.state
        if state == LifecycleState.STOPPED:
            return self._handle_start_command(command)
        if state == LifecycleState.RUNNING:
            return self._request_stop(command, restart=True)
        logger.warning(f"Cannot restart the Minecraft server while it is {state}.")
        command.result = False
        command.done.set()
        return []

    def _start_allowed(self) -> bool:
        reason = self._start_rejection()
        if reason is None and self._server_process_present():
            reason = (
                "A Minecraft server process is already running in the server directory."
            )
        if reason is not None:
            logger.warning(reason)
            return False
        return True

    def _start_rejection(self) -> str | None:
        now = self._clock()
        with self._state_lock:
            if self._state != LifecycleState.STOPPED:
                return "The Minecraft server is already running or starting."
            if now - self.last_server_started_time < self.server_starting_cooldown:
                return "Tried to start the server too quickly multiple times."
            if now - self.last_server_stopped_time < self.server_stopping_cooldown:
                return "Tried to start the server too quickly after stopping it."
        return None

    def _begin_start(self) -> LaunchRequest:
        with self._state_lock:
            self._generation += 1
            self._state = LifecycleState.STARTING
            self._identity = None
            self._player_data = PLAYER_DATA_UNKNOWN
            self._restart_pending = False
            self._auto_stop = auto_stop.AutoStopState()
            self._poll_due_at = None
            self._poll_inflight = False
            return LaunchRequest(self._generation)

    def _request_stop(self, command, restart: bool) -> list:
        with self._state_lock:
            reason = self._stop_rejection_locked()
            request = None if reason else self._begin_stop_locked(restart=restart)
        if reason:
            logger.warning(reason)
        command.result = request is not None
        if request is not None:
            verb = "Restarting" if restart else "Stopping"
            logger.info(f"{verb} the Minecraft server")
            self._publish_lifecycle(LifecycleState.STOPPING, request.generation)
        command.done.set()
        return [request] if request is not None else []

    def _stop_rejection_locked(self) -> str | None:
        if self._state != LifecycleState.RUNNING or self._identity is None:
            return "An attempt to stop the Minecraft server was made, but it was not running."
        if (
            self._clock() - self.last_server_stopped_time
            < self.server_stopping_cooldown
        ):
            return "Tried to stop the server too quickly after the previous stop."
        return None

    def _begin_stop_locked(self, restart: bool = False) -> StopRequest | None:
        identity = self._identity
        if identity is None:
            return None
        self._state = LifecycleState.STOPPING
        self._restart_pending = restart
        self._auto_stop = auto_stop.cancelled(self._auto_stop)
        self._poll_due_at = None
        self._poll_inflight = False
        return StopRequest(self._generation, identity)

    def _begin_stop(self) -> list:
        with self._state_lock:
            request = self._begin_stop_locked()
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
        logger.warning("Stop failed while the process is still alive; resuming polling")
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
            logger.info("Auto-stop: the server stayed empty; stopping it")
            return self._begin_stop()
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
        created = identity.create_time
        # An adopted server has been up since its process was created.
        if adopted and created is not None and 0 < created <= now:
            return float(created)
        return now

    # ------------------------------------------------------------------
    # Discovery and auto-stop helpers
    # ------------------------------------------------------------------

    def _server_process_present(self) -> bool:
        """True when a tracked or discoverable server process is alive."""
        pm = self.process_manager
        try:
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
        except Exception:
            # Never risk two servers on one world: refuse to start.
            logger.exception("Process discovery failed")
            return True

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
        stop_now = False
        revalidate = False
        with self._state_lock:
            if self._state != LifecycleState.RUNNING or self._generation != generation:
                self._auto_stop = auto_stop.cancelled(self._auto_stop)
                return []
            self._auto_stop, decision = auto_stop.on_expiry(
                self._auto_stop,
                config=self._auto_stop_config(),
                now=self._clock(),
                generation=generation,
                poll_inflight=self._poll_inflight,
                poll_due_at=self._poll_due_at,
                poll_seq=self._poll_seq,
                poll_interval=self._poll_interval(),
            )
            if decision == auto_stop.AutoStopDecision.REVALIDATE:
                revalidate = True
                if not self._poll_inflight:
                    self._poll_due_at = self._clock()
            elif decision == auto_stop.AutoStopDecision.STOP:
                stop_now = True
        if stop_now:
            logger.info("Auto-stop delay elapsed with a fresh empty sample")
            return self._begin_stop()
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
