"""Runtime tests: real threads, blocking I/O, stale responses, restart and cleanup.

No fixed sleeps: a HandledSpy on the supervisor confirms when the lifecycle
thread has consumed a message.
"""

import threading
from types import SimpleNamespace

import pytest

from src.events import Event, EventBus
from src.lifecycle import runtime as runtime_module
from src.lifecycle.messages import (
    LifecycleState,
    ProcessIdentity,
    ProcessObservation,
    RestartCommand,
    RosterSample,
    StartCommand,
    StopCommand,
    StopResult,
)
from src.lifecycle.runtime import LifecycleRuntime
from src.lifecycle.supervisor import Supervisor
from test.support import wait_until
from test.support.fakes import (
    FakeBanlistManager,
    FakeClient,
    FakeClock,
    FakePlayerManager,
    FakeProcessManager,
    HandledSpy,
    make_roster,
)

CONTROLLER_THREAD_NAMES = {
    "minecraft-lifecycle",
    "minecraft-detection",
    "minecraft-poll",
    "minecraft-process",
}
STARTING = LifecycleState.STARTING


@pytest.fixture
def env(mock_settings):
    mock_settings.pollingRate = 0.01
    mock_settings.autoStop = False
    mock_settings.autoStopDelay = 60
    bus = EventBus()
    pm = FakeProcessManager()
    client = FakeClient()
    players = FakePlayerManager(bus=bus)
    bans = FakeBanlistManager(bus=bus)
    clock = FakeClock()
    supervisor = Supervisor(pm, players, bans, bus, clock=clock)
    supervisor.poll_interval = 0
    spy = HandledSpy(supervisor)
    runtime = LifecycleRuntime(supervisor, client, pm)
    events = []
    for event_type in (
        Event.SERVER_STARTED,
        Event.SERVER_STOPPED,
        Event.LIFECYCLE_CHANGED,
    ):
        bus.subscribe(
            event_type, lambda data, kind=event_type: events.append((kind, data))
        )
    yield SimpleNamespace(
        runtime=runtime,
        supervisor=supervisor,
        client=client,
        pm=pm,
        players=players,
        bus=bus,
        clock=clock,
        spy=spy,
        events=events,
        settings=mock_settings,
    )
    runtime.close()


def adopt_running(env, identity=None):
    identity = identity or ProcessIdentity(1001, 10.0)
    env.pm.found = identity
    env.pm.alive.add(identity.pid)
    env.runtime.start()
    wait_until(lambda: env.supervisor.state == LifecycleState.RUNNING)
    return identity


def launches_at_starting(env):
    """How many launches had begun each time STARTING was delivered."""
    seen = []
    env.bus.subscribe(
        Event.LIFECYCLE_CHANGED,
        lambda data: (
            data["state"] == STARTING and seen.append(len(env.pm.launch_calls))
        ),
    )
    return seen


class TestLifecycleRuntime:
    def test_starts_only_four_named_threads(self, env):
        env.runtime.start()

        assert set(env.runtime._threads) == CONTROLLER_THREAD_NAMES
        assert {
            t.name for t in env.runtime._threads.values()
        } == CONTROLLER_THREAD_NAMES
        assert all(thread.is_alive() for thread in env.runtime._threads.values())

    def test_blocked_roster_response_after_stop_is_discarded(self, env):
        adopt_running(env)
        wait_until(lambda: env.supervisor.get_snapshot()["playerData"] == "ok")

        gate = env.client.hold(make_roster("Ghost"))
        wait_until(env.client.entered.is_set)

        assert env.runtime.submit_command(StopCommand()).success
        wait_until(lambda: env.supervisor.state == LifecycleState.STOPPED)

        handled = env.spy.count(RosterSample)
        gate.set()
        wait_until(
            lambda: env.spy.count(RosterSample) > handled
        )  # late answer consumed

        snapshot = env.supervisor.get_snapshot()
        assert snapshot["state"] == LifecycleState.STOPPED
        assert snapshot["playerCount"] == 0
        assert snapshot["players"] == []

    def test_poll_worker_survives_client_errors(self, env):
        def broken():
            raise OSError("connection reset")

        env.client.get_roster = broken
        adopt_running(env)

        wait_until(lambda: env.supervisor.get_snapshot()["playerData"] == "unavailable")
        assert env.supervisor.state == LifecycleState.RUNNING

    def test_repeated_start_close_leaves_no_threads_or_subscriptions(self, env):
        baseline = env.bus.subscriber_count()

        for _ in range(3):
            env.runtime.start()
            env.runtime.close()

        assert env.bus.subscriber_count() == baseline
        assert not any(t.name in CONTROLLER_THREAD_NAMES for t in threading.enumerate())
        assert env.pm.closed is True

    def test_commands_work_after_restarting_the_runtime(self, env):
        env.supervisor.server_starting_cooldown = 0
        env.supervisor.server_stopping_cooldown = 0

        for pid in range(1001, 1004):
            env.pm.launch_identity = ProcessIdentity(pid, 10.0)
            env.runtime.start()
            try:
                assert env.runtime.submit_command(StartCommand()).success
                wait_until(lambda: env.supervisor.state == LifecycleState.RUNNING)
                assert env.runtime.submit_command(StopCommand()).success
                wait_until(lambda: env.supervisor.state == LifecycleState.STOPPED)
            finally:
                env.runtime.close()

    def test_cannot_restart_while_a_worker_is_still_blocked(self, env, monkeypatch):
        monkeypatch.setattr(runtime_module, "_WORKER_JOIN_TIMEOUT", 0.01)
        env.pm.found = ProcessIdentity(1001, 10.0)
        env.pm.alive.add(1001)
        gate = env.client.hold()
        env.runtime.start()
        assert env.client.entered.wait(1)

        try:
            env.runtime.close()
            with pytest.raises(RuntimeError, match="still shutting down"):
                env.runtime.start()
        finally:
            gate.set()
            wait_until(
                lambda: not any(t.is_alive() for t in env.runtime._threads.values())
            )

        env.runtime.start()
        wait_until(lambda: env.supervisor.get_snapshot()["playerData"] == "ok")

    def test_process_worker_survives_launch_and_stop_errors(self, env, monkeypatch):
        env.supervisor.server_starting_cooldown = 0
        env.supervisor.server_stopping_cooldown = 0
        env.pm.launch_identity = ProcessIdentity(1001, 10.0)

        def fail(*_args, **_kwargs):
            raise OSError("process operation failed")

        env.runtime.start()
        with monkeypatch.context() as patch:
            patch.setattr(env.pm, "launch_process", fail)
            assert env.runtime.submit_command(StartCommand()).success
            wait_until(lambda: env.supervisor.state == LifecycleState.STOPPED)

        assert env.runtime.submit_command(StartCommand()).success
        wait_until(lambda: env.supervisor.state == LifecycleState.RUNNING)
        with monkeypatch.context() as patch:
            patch.setattr(env.pm, "terminate_process", fail)
            assert env.runtime.submit_command(StopCommand()).success
            wait_until(lambda: env.supervisor.state == LifecycleState.RUNNING)

        assert env.runtime.submit_command(StopCommand()).success
        wait_until(lambda: env.supervisor.state == LifecycleState.STOPPED)

    def test_detection_confirms_a_pending_stop(self, env):
        identity = adopt_running(env)
        env.pm.terminate_result = None  # shutdown still in progress

        assert env.runtime.submit_command(StopCommand()).success
        wait_until(lambda: env.spy.count(StopResult) == 1)
        scans = env.spy.count(ProcessObservation)
        wait_until(lambda: env.spy.count(ProcessObservation) > scans)
        assert env.supervisor.state == LifecycleState.STOPPING  # alive: still stopping

        env.pm.exit(identity)
        wait_until(lambda: env.supervisor.state == LifecycleState.STOPPED)

    def test_command_before_start_is_rejected(self, env):
        result = env.runtime.submit_command(StopCommand())

        assert result.success is False
        assert result.message == runtime_module.NOT_RUNNING_MESSAGE

    def test_refusal_reasons_reach_the_caller(self, env):
        env.runtime.start()
        wait_until(lambda: env.events)  # startup reconcile

        result = env.runtime.submit_command(StopCommand())

        assert not result
        assert result.message == "The server is not running."

    def test_close_waits_for_an_in_flight_process_command(self, env, monkeypatch):
        env.supervisor.server_starting_cooldown = 0
        monkeypatch.setattr(runtime_module, "_WORKER_JOIN_TIMEOUT", 0.01)
        entered = threading.Event()
        release = threading.Event()
        closer = threading.Thread(target=env.runtime.close, daemon=True)
        closer_alive_when_launch_returned = []

        def slow_launch():
            entered.set()
            release.wait(30)
            closer_alive_when_launch_returned.append(closer.is_alive())
            return ProcessIdentity(1001, 10.0)

        monkeypatch.setattr(env.pm, "launch_process", slow_launch)
        env.runtime.start()
        threading.Thread(
            target=env.runtime.submit_command, args=(StartCommand(),), daemon=True
        ).start()
        assert entered.wait(2)

        closer.start()
        wait_until(env.runtime._closing.is_set)
        release.set()

        closer.join(5)
        assert not closer.is_alive()
        assert closer_alive_when_launch_returned == [True]  # close() waited for it
        assert not env.runtime._threads["minecraft-process"].is_alive()

    @pytest.mark.parametrize("stop_timeout", [10, 300])
    def test_process_worker_join_timeout_covers_the_stop_chain(self, env, stop_timeout):
        env.settings.stopTimeout = stop_timeout

        timeout = runtime_module._process_worker_join_timeout()

        assert timeout >= stop_timeout + 60
        # graceful wait + max(30, stopTimeout) after SIGTERM + 10 s after SIGKILL
        assert timeout > stop_timeout + max(30, stop_timeout) + 10

    def test_starting_is_delivered_before_the_launch(self, env):
        seen = launches_at_starting(env)
        env.pm.launch_identity = ProcessIdentity(2002, 20.0)
        env.runtime.start()

        assert env.runtime.submit_command(StartCommand()).success
        wait_until(lambda: env.supervisor.state == LifecycleState.RUNNING)

        assert seen == [0]  # the port is released before the server starts

    def test_restart_through_the_runtime(self, env):
        env.supervisor.server_starting_cooldown = 60
        env.supervisor.server_stopping_cooldown = 60
        first = adopt_running(env)
        second = ProcessIdentity(2002, 20.0)
        env.pm.launch_identity = second
        seen = launches_at_starting(env)
        env.events.clear()

        assert env.runtime.submit_command(RestartCommand()).success

        wait_until(
            lambda: (
                env.supervisor.state == LifecycleState.RUNNING
                and env.pm.tracked_identity == second
            )
        )
        assert seen == [0]
        assert env.pm.terminate_calls == [first]
        assert [(kind, data["state"]) for kind, data in env.events] == [
            (Event.LIFECYCLE_CHANGED, LifecycleState.STOPPING),
            (Event.SERVER_STOPPED, LifecycleState.STOPPED),
            (Event.LIFECYCLE_CHANGED, LifecycleState.STARTING),
            (Event.SERVER_STARTED, LifecycleState.RUNNING),
        ]
        assert env.supervisor.get_snapshot()["restartPending"] is False

    def test_stop_right_after_a_restart_is_accepted(self, env):
        adopt_running(env)
        env.pm.launch_identity = ProcessIdentity(2002, 20.0)
        assert env.runtime.submit_command(RestartCommand()).success
        wait_until(lambda: env.pm.tracked_identity == ProcessIdentity(2002, 20.0))
        wait_until(lambda: env.supervisor.state == LifecycleState.RUNNING)

        result = env.runtime.submit_command(StopCommand())

        assert result.success, result.message
        wait_until(lambda: env.supervisor.state == LifecycleState.STOPPED)

    def test_only_an_admin_stop_may_force(self, env):
        env.settings.autoStop = True
        adopt_running(env)
        assert env.runtime.submit_command(StopCommand()).success
        wait_until(lambda: env.supervisor.state == LifecycleState.STOPPED)

        # Auto-stop: past the startup guard, then past the empty delay.
        env.pm.found = ProcessIdentity(3003, 30.0)
        env.pm.alive.add(3003)
        wait_until(lambda: env.supervisor.state == LifecycleState.RUNNING)
        env.clock.advance(31)
        wait_until(lambda: env.supervisor.auto_stop_deadline is not None)
        env.clock.advance(61)
        wait_until(lambda: env.supervisor.state == LifecycleState.STOPPED)

        assert env.pm.terminate_forced == [True, False]
