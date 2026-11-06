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

"""Lifecycle runtime: queues, workers, command waiting and thread lifecycle.

Workers perform blocking I/O and submit messages. Only the single lifecycle
thread calls the supervisor; this module never mutates lifecycle state itself.

Threads: ``minecraft-lifecycle`` (the supervisor), ``minecraft-detection``
(process scans every ``pollingRate``; about every second while STOPPED, so a
server started by hand is adopted, and the sleeping listener releases the
game port, before it binds), ``minecraft-poll`` (roster polls over RCON /
status ping) and ``minecraft-process`` (launch and stop, serialized).
"""

import logging
import queue
import threading

from src.lifecycle.messages import (
    ActionResult,
    LaunchRequest,
    LaunchResult,
    LifecycleState,
    PollRequest,
    ProcessObservation,
    RosterSample,
    StopRequest,
    StopResult,
)
from src.lifecycle.supervisor import AutoStopRequest, Supervisor
from src.process_manager import ProcessStateUnknown
from src.settings import settings

logger = logging.getLogger(__name__)

LIFECYCLE_THREAD = "minecraft-lifecycle"
DETECTION_THREAD = "minecraft-detection"
POLL_THREAD = "minecraft-poll"
PROCESS_THREAD = "minecraft-process"
THREAD_NAMES = (LIFECYCLE_THREAD, DETECTION_THREAD, POLL_THREAD, PROCESS_THREAD)

_SHUTDOWN = object()
_WORKER_JOIN_TIMEOUT = 5
_COMMAND_TIMEOUT = 5
# A graceful stop may wait stopTimeout, then max(30, stopTimeout) after SIGTERM
# and 10 s after SIGKILL. A launch or stop must finish before close() returns,
# or it would act on a server the monitor no longer supervises. (A launch
# still waiting for the previous launch script is skipped instead.)
_PROCESS_WORKER_JOIN_MARGIN = 60
_MIN_TERMINATE_WAIT = 30.0
STOPPED_DETECTION_INTERVAL = 1.0  # seconds between scans while STOPPED
NOT_RUNNING_MESSAGE = "The monitor's lifecycle controller is not running."
TIMED_OUT_MESSAGE = (
    "The lifecycle controller did not answer in time; see the monitor log."
)


def _process_worker_join_timeout() -> float:
    try:
        stop_timeout = max(0.0, float(settings.stopTimeout))
    except (TypeError, ValueError):
        stop_timeout = 120.0
    return (
        stop_timeout
        + max(_MIN_TERMINATE_WAIT, stop_timeout)
        + _PROCESS_WORKER_JOIN_MARGIN
    )


class LifecycleRuntime:
    def __init__(self, supervisor: Supervisor, client, process_manager):
        self.supervisor = supervisor
        self.client = client
        self.process_manager = process_manager

        self._observations: queue.Queue = queue.Queue()
        self._poll_requests: queue.Queue = queue.Queue()
        self._process_requests: queue.Queue = queue.Queue()

        self._closing = threading.Event()
        # A launch still waiting for the previous launch script is skipped
        # once close() sets this, before close() waits for the process worker.
        watch_shutdown = getattr(process_manager, "watch_shutdown", None)
        if callable(watch_shutdown):
            watch_shutdown(self._closing)
        # Set on every committed state change (and on close): the detection
        # worker scans at once instead of finishing a RUNNING-cadence wait.
        self._wake_detection = threading.Event()
        self._started = False
        self._start_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def start(self):
        """Start the lifecycle thread and workers. Idempotent."""
        with self._start_lock:
            if self._started:
                return

            if any(thread.is_alive() for thread in self._threads.values()):
                raise RuntimeError("Lifecycle workers are still shutting down")

            # No workers remain. Preserve pending work, not shutdown markers.
            for requests in (
                self._observations,
                self._poll_requests,
                self._process_requests,
            ):
                for _ in range(requests.qsize()):
                    request = requests.get_nowait()
                    if request is not _SHUTDOWN:
                        requests.put(request)

            self._started = True
            self._closing.clear()
            self._wake_detection.clear()
            targets = {
                LIFECYCLE_THREAD: self._supervisor_loop,
                DETECTION_THREAD: self._detection_worker,
                POLL_THREAD: self._poll_worker,
                PROCESS_THREAD: self._process_worker,
            }
            self._threads = {
                name: threading.Thread(target=target, name=name, daemon=True)
                for name, target in targets.items()
            }
            for thread in self._threads.values():
                thread.start()

    def close(self):
        """Stop the workers and release process resources. Idempotent.

        Never stops the Minecraft server: it is adopted on the next start.
        """
        with self._start_lock:
            if not self._started:
                return
            self._started = False
            self._closing.set()
            self._wake_detection.set()
            self._observations.put(_SHUTDOWN)
            for requests in (self._poll_requests, self._process_requests):
                requests.put(_SHUTDOWN)
            threads = list(self._threads.items())

        for name, thread in threads:
            if thread is threading.current_thread():
                continue
            timeout = _WORKER_JOIN_TIMEOUT
            if name == PROCESS_THREAD:
                timeout = _process_worker_join_timeout()
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(f"Thread {thread.name} did not stop in time")

        close_process_manager = getattr(self.process_manager, "close", None)
        if close_process_manager is not None:
            try:
                close_process_manager()
            except Exception:
                logger.exception("Closing the process manager failed")

    def submit_command(self, command) -> ActionResult:
        """Hand a command to the lifecycle thread and wait for its outcome.

        The ActionResult is truthy when the command was accepted; its message
        says what happens next or why the command was refused.
        """
        if not self._started or self._closing.is_set():
            logger.warning("Controller is not running; command ignored")
            return ActionResult(False, NOT_RUNNING_MESSAGE)
        self._observations.put(command)
        if not command.done.wait(timeout=_COMMAND_TIMEOUT):
            logger.error("Lifecycle command timed out")
            return ActionResult(False, TIMED_OUT_MESSAGE)
        outcome = getattr(command, "outcome", None)
        if isinstance(outcome, ActionResult):
            return outcome
        return ActionResult(bool(command.result))

    # ------------------------------------------------------------------
    # Lifecycle thread
    # ------------------------------------------------------------------

    def _supervisor_loop(self):
        while not self._closing.is_set():
            timeout = self.supervisor.next_timeout()
            state = self.supervisor.state
            try:
                item = self._observations.get(timeout=timeout)
            except queue.Empty:
                try:
                    requests = self.supervisor.on_tick()
                except Exception:
                    logger.exception("Lifecycle supervisor tick failed")
                    continue
            else:
                if item is _SHUTDOWN:
                    break
                try:
                    requests = self.supervisor.handle(item)
                except Exception:
                    logger.exception(
                        "Lifecycle supervisor failed to handle an observation"
                    )
                    continue
            if self.supervisor.state != state:
                self._wake_detection.set()
            for request in requests:
                self._dispatch(request)

    def _dispatch(self, request):
        if isinstance(request, PollRequest):
            self._poll_requests.put(request)
        elif isinstance(request, (LaunchRequest, StopRequest)):
            self._process_requests.put(request)
        else:
            logger.warning(f"Ignoring unknown lifecycle request: {request!r}")

    # ------------------------------------------------------------------
    # Workers: blocking I/O only
    # ------------------------------------------------------------------

    def _detection_worker(self):
        unknown = False
        while not self._closing.is_set():
            try:
                generation = self.supervisor.generation
                snapshot = self.process_manager.observe()
            except ProcessStateUnknown as e:
                # Unknown is not "exited": post nothing, keep the last state.
                if not unknown:
                    logger.warning(f"Keeping the last known server state: {e}")
                unknown = True
            except Exception:
                logger.exception("Process detection failed")
            else:
                if unknown:
                    logger.info("Process detection works again")
                unknown = False
                self._observations.put(ProcessObservation(generation, snapshot))
            self._wake_detection.wait(self._detection_interval())
            self._wake_detection.clear()

    def _detection_interval(self) -> float:
        rate = _polling_rate()
        if self.supervisor.state == LifecycleState.STOPPED:
            return min(rate, STOPPED_DETECTION_INTERVAL)
        return rate

    def _poll_worker(self):
        while not self._closing.is_set():
            request = self._poll_requests.get()
            if request is _SHUTDOWN:
                break
            try:
                roster = self.client.get_roster()
            except Exception:
                logger.exception("Roster poll failed")
                roster = None
            self._observations.put(
                RosterSample(request.generation, request.identity, request.seq, roster)
            )

    def _process_worker(self):
        # Serialize process changes, including a restart after an observed exit
        # while the previous stop command is still returning.
        while not self._closing.is_set():
            request = self._process_requests.get()
            if request is _SHUTDOWN:
                break

            if self._closing.is_set():
                self._process_requests.put(request)
                break

            if isinstance(request, LaunchRequest):
                self._observations.put(self._launch(request))
            else:
                self._observations.put(self._terminate(request))

    def _launch(self, request: LaunchRequest) -> LaunchResult:
        try:
            identity = self.process_manager.launch_process()
        except Exception:
            logger.exception("Server launch failed")
            identity = None
        return LaunchResult(request.generation, identity)

    def _terminate(self, request: StopRequest) -> StopResult:
        try:
            # Nobody waits for an auto-stop: never force-kill what cannot be
            # stopped gracefully (Windows has no graceful signal).
            force = not isinstance(request, AutoStopRequest)
            stopped = self.process_manager.terminate_process(
                request.identity, force=force
            )
        except Exception:
            logger.exception("Server termination failed")
            stopped = False
        return StopResult(request.generation, request.identity, stopped)


def _polling_rate() -> float:
    try:
        rate = float(settings.pollingRate)
    except (TypeError, ValueError):
        return 5.0
    return rate if rate > 0 else 5.0
