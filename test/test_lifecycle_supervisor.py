"""Direct tests for synchronous supervisor message handling (fakes + fake clocks)."""

from types import SimpleNamespace

import pytest

from src.events import Event, EventBus
from src.lifecycle.messages import (
    ActionResult,
    LaunchRequest,
    LaunchResult,
    LifecycleState,
    OnlinePlayer,
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
from src.lifecycle.supervisor import (
    DISCOVERY_FAILED_MESSAGE,
    PROCESS_PRESENT_MESSAGE,
    SNAPSHOT_KEYS,
    AutoStopRequest,
    OfflineEditCommand,
    Supervisor,
)
from test.support.fakes import (
    FakeBanlistManager,
    FakeClock,
    FakePlayerManager,
    FakeProcessManager,
    FakeWallClock,
    make_roster,
)

ALL_EVENTS = (
    Event.SERVER_STARTED,
    Event.SERVER_STOPPED,
    Event.SERVER_STATUS,
    Event.LIFECYCLE_CHANGED,
)
RUNNING = LifecycleState.RUNNING
STOPPED = LifecycleState.STOPPED
STARTING = LifecycleState.STARTING
STOPPING = LifecycleState.STOPPING


@pytest.fixture
def env(mock_settings):
    mock_settings.pollingRate = 0.01
    mock_settings.autoStop = False
    mock_settings.autoStopDelay = 60
    bus = EventBus()
    pm = FakeProcessManager()
    players = FakePlayerManager(bus=bus)
    bans = FakeBanlistManager(bus=bus)
    clock = FakeClock()
    wall = FakeWallClock()
    supervisor = Supervisor(pm, players, bans, bus, clock=clock, wall_clock=wall)
    supervisor.poll_interval = 0
    supervisor.server_starting_cooldown = 0
    supervisor.server_stopping_cooldown = 0
    events = []
    for event_type in ALL_EVENTS:
        bus.subscribe(
            event_type, lambda data, kind=event_type: events.append((kind, data))
        )
    return SimpleNamespace(
        supervisor=supervisor,
        pm=pm,
        players=players,
        bans=bans,
        bus=bus,
        clock=clock,
        wall=wall,
        events=events,
        settings=mock_settings,
    )


def start_running(env, identity=None):
    """Adopt an already-running server through a detection observation."""
    identity = identity or ProcessIdentity(1001, 10.0)
    env.pm.identity = identity
    env.pm.alive.add(identity.pid)
    env.pm.found = identity
    env.supervisor.handle(
        ProcessObservation(env.supervisor.generation, env.pm.observe())
    )
    assert env.supervisor.state == RUNNING
    return identity


def launch(env, identity=None):
    """StartCommand -> LaunchRequest -> (fake launch) -> LaunchResult."""
    identity = identity or ProcessIdentity(2001, 20.0)
    env.pm.launch_identity = identity
    command = StartCommand()
    [request] = env.supervisor.handle(command)
    assert command.result is True
    assert env.pm.launch_process() == identity
    env.supervisor.handle(LaunchResult(request.generation, identity))
    assert env.supervisor.state == RUNNING
    return identity


def stop(env, identity):
    """StopCommand -> (fake terminate) -> StopResult(True)."""
    [request] = env.supervisor.handle(StopCommand())
    assert env.pm.terminate_process(identity) is True
    env.supervisor.handle(StopResult(request.generation, identity, True))
    assert env.supervisor.state == STOPPED


def sample(env, roster, seq=1, identity=None, generation=None):
    return env.supervisor.handle(
        RosterSample(
            env.supervisor.generation if generation is None else generation,
            identity or env.pm.identity,
            seq,
            roster,
        )
    )


def observe(env):
    return env.supervisor.handle(
        ProcessObservation(env.supervisor.generation, env.pm.observe())
    )


def events_of(env, event_type):
    return [data for kind, data in env.events if kind == event_type]


def summary(env):
    return [(kind, data["state"]) for kind, data in env.events]


def snapshot(env):
    return env.supervisor.get_snapshot()


class TestTransitions:
    def test_startup_reconcile_publishes_stopped_for_auto_start(self, env):
        empty = ProcessSnapshot(tracked=None, tracked_running=False, found=None)

        env.supervisor.handle(ProcessObservation(0, empty))

        stopped = events_of(env, Event.SERVER_STOPPED)
        assert len(stopped) == 1
        assert stopped[0]["state"] == STOPPED
        assert stopped[0]["pid"] is None
        assert env.players.mark_offline_calls == 1

        # Reconciliation happens once, not on every empty scan.
        env.supervisor.handle(ProcessObservation(0, empty))
        assert len(events_of(env, Event.SERVER_STOPPED)) == 1

    def test_start_command_returns_launch_request(self, env):
        command = StartCommand()
        requests = env.supervisor.handle(command)

        assert command.result is True
        assert command.done.is_set()
        assert len(requests) == 1
        assert isinstance(requests[0], LaunchRequest)
        assert env.supervisor.state == STARTING
        assert env.pm.find_calls == 1  # discovery without a name argument
        assert events_of(env, Event.LIFECYCLE_CHANGED)[0]["state"] == STARTING

        identity = ProcessIdentity(2001, 20.0)
        env.pm.set_known_identity(identity)
        env.supervisor.handle(LaunchResult(requests[0].generation, identity))

        assert env.supervisor.state == RUNNING
        assert events_of(env, Event.SERVER_STARTED)[0]["pid"] == identity.pid
        ticks = env.supervisor.on_tick()
        assert len(ticks) == 1
        assert isinstance(ticks[0], PollRequest)

    def test_failed_launch_returns_to_stopped(self, env):
        [request] = env.supervisor.handle(StartCommand())

        env.supervisor.handle(LaunchResult(request.generation, None))

        assert env.supervisor.state == STOPPED
        assert events_of(env, Event.SERVER_STARTED) == []
        assert events_of(env, Event.SERVER_STOPPED)[-1]["pid"] is None

    def test_roster_commit_updates_snapshot_and_player_manager(self, env):
        start_running(env)
        roster = make_roster("Steve", "Alex", max_players=30)

        sample(env, roster)

        snap = snapshot(env)
        assert snap["state"] == RUNNING
        assert snap["playerData"] == "ok"
        assert snap["playerCount"] == 2
        assert snap["maxPlayers"] == 30
        assert {p["name"] for p in snap["players"] if p["currently_online"]} == {
            "Steve",
            "Alex",
        }
        assert env.players.roster_updates == [roster]
        assert events_of(env, Event.SERVER_STATUS)[-1]["playerCount"] == 2

    def test_stale_process_observation_cannot_adopt_after_down(self, env):
        identity = start_running(env)
        old_generation = env.supervisor.generation
        stale_snapshot = ProcessSnapshot(
            tracked=identity, tracked_running=True, found=identity
        )

        [stop_request] = env.supervisor.handle(StopCommand())
        assert isinstance(stop_request, StopRequest)
        env.supervisor.handle(StopResult(stop_request.generation, identity, True))
        assert env.supervisor.state == STOPPED
        started_before = len(events_of(env, Event.SERVER_STARTED))

        env.supervisor.handle(ProcessObservation(old_generation, stale_snapshot))

        assert env.supervisor.state == STOPPED
        assert len(events_of(env, Event.SERVER_STARTED)) == started_before

    def test_reused_live_pid_is_not_adopted_without_a_matching_scan(self, env):
        reused = ProcessIdentity(999, 1.0)
        env.pm.identity = reused
        env.pm.alive.add(reused.pid)
        env.pm.found = None

        observe(env)

        assert env.pm.tracked_identity is None
        assert env.supervisor.state == STOPPED
        assert events_of(env, Event.SERVER_STARTED) == []
        assert reused.pid in env.pm.alive

        actual = ProcessIdentity(2002, 20.0)
        env.pm.found = actual
        env.pm.alive.add(actual.pid)
        observe(env)

        assert env.pm.tracked_identity == actual
        assert env.supervisor.state == RUNNING
        assert reused.pid in env.pm.alive
        assert env.pm.terminate_calls == []

    def test_failed_stop_returns_to_running_and_requests_poll(self, env):
        identity = start_running(env)
        [stop_request] = env.supervisor.handle(StopCommand())

        env.supervisor.handle(StopResult(stop_request.generation, identity, False))

        assert env.supervisor.state == RUNNING
        assert events_of(env, Event.LIFECYCLE_CHANGED)[-1]["state"] == RUNNING
        ticks = env.supervisor.on_tick()
        assert len(ticks) == 1
        assert isinstance(ticks[0], PollRequest)

    def test_pending_stop_waits_for_exit(self, env):
        identity = start_running(env)
        [stop_request] = env.supervisor.handle(StopCommand())

        env.supervisor.handle(StopResult(stop_request.generation, identity, None))
        assert env.supervisor.state == STOPPING

        env.pm.exit(identity)
        observe(env)
        assert env.supervisor.state == STOPPED

    def test_start_guard_blocks_an_unadopted_found_process(self, env):
        found = ProcessIdentity(2002, 20.0)
        env.pm.found = found
        env.pm.alive.add(found.pid)

        command = StartCommand()
        requests = env.supervisor.handle(command)

        assert command.result is False
        assert command.outcome == ActionResult(False, PROCESS_PRESENT_MESSAGE)
        assert requests == []
        assert env.supervisor.state == STOPPED

        env.supervisor.handle(
            ProcessObservation(
                0, ProcessSnapshot(tracked=None, tracked_running=False, found=found)
            )
        )
        assert env.pm.tracked_identity == found
        assert env.supervisor.state == RUNNING

    def test_discovery_failure_refuses_to_start(self, env):
        env.pm.find_error = RuntimeError("scan failed")

        command = StartCommand()

        assert env.supervisor.handle(command) == []
        assert command.result is False
        assert command.outcome.message == DISCOVERY_FAILED_MESSAGE

    def test_start_during_the_stop_debounce_launches_when_it_ends(self, env):
        env.supervisor.server_stopping_cooldown = 5
        stop(env, launch(env))
        env.clock.advance(0.8)

        command = StartCommand()
        assert env.supervisor.handle(command) == []
        # Accepted at once: STARTING releases the game port; the launch waits.
        assert command.outcome == ActionResult(True, "Starting the server in 5 s.")
        assert env.supervisor.state == STARTING
        assert events_of(env, Event.LIFECYCLE_CHANGED)[-1]["state"] == STARTING
        assert env.supervisor.next_timeout() == pytest.approx(4.2)

        env.clock.advance(4.1)
        assert env.supervisor.on_tick() == []
        env.clock.advance(0.1)
        [request] = env.supervisor.on_tick()
        assert isinstance(request, LaunchRequest)
        assert request.generation == env.supervisor.generation
        assert env.supervisor.on_tick() == []  # dispatched once

        identity = ProcessIdentity(3001, 30.0)
        env.supervisor.handle(LaunchResult(request.generation, identity))
        assert env.supervisor.state == RUNNING

    def test_start_after_the_debounce_launches_at_once(self, env):
        env.supervisor.server_stopping_cooldown = 5
        stop(env, launch(env))
        env.clock.advance(5)

        command = StartCommand()
        [request] = env.supervisor.handle(command)
        assert command.outcome == ActionResult(True, "Starting the server.")
        assert isinstance(request, LaunchRequest)

    def test_crash_right_after_a_start_only_waits_for_the_stop_debounce(self, env):
        env.supervisor.server_stopping_cooldown = 5
        identity = launch(env)
        env.clock.advance(1)
        env.pm.exit(identity)
        observe(env)  # crashed one second after starting
        env.clock.advance(4.5)

        command = StartCommand()
        env.supervisor.handle(command)

        # No separate start -> start cooldown under-reporting the wait.
        assert command.outcome == ActionResult(True, "Starting the server in 1 s.")

    def test_server_started_by_hand_during_the_debounce_cancels_the_launch(self, env):
        env.supervisor.server_stopping_cooldown = 5
        stop(env, launch(env))
        env.supervisor.handle(StartCommand())
        env.pm.found = ProcessIdentity(4001, 40.0)
        env.pm.alive.add(4001)

        env.clock.advance(5)
        assert env.supervisor.on_tick() == []

        assert env.supervisor.state == STOPPED
        observe(env)
        assert env.supervisor.state == RUNNING
        assert env.pm.tracked_identity == ProcessIdentity(4001, 40.0)

    def test_deferred_launch_is_dropped_by_a_newer_generation(self, env):
        env.supervisor.server_stopping_cooldown = 5
        stop(env, launch(env))
        env.supervisor.handle(StartCommand())
        adopted = ProcessIdentity(4002, 41.0)
        env.supervisor._commit_started(adopted, adopted=True)  # e.g. adoption

        env.clock.advance(5)
        requests = env.supervisor.on_tick()
        assert not any(isinstance(r, LaunchRequest) for r in requests)
        assert env.supervisor.state == RUNNING

    def test_collaborator_failure_does_not_break_the_stop_commit(self, env):
        identity = launch(env)

        def broken():
            raise RuntimeError("players.json is read-only")

        env.players.mark_all_offline = broken
        stop(env, identity)

        assert events_of(env, Event.SERVER_STOPPED)[-1]["pid"] == identity.pid
        assert env.pm.tracked_identity is None

    def test_late_roster_from_previous_generation_is_discarded(self, env):
        first = launch(env)
        old_generation = env.supervisor.generation
        stop(env, first)
        second = launch(env, ProcessIdentity(3003, 30.0))

        sample(env, make_roster("Ghost"), identity=first, generation=old_generation)

        assert snapshot(env)["playerData"] == "unknown"
        assert env.players.roster_updates == []
        assert env.pm.identity == second


class TestSnapshot:
    def test_snapshot_has_exactly_the_documented_fields(self, env):
        snap = snapshot(env)

        assert tuple(snap) == SNAPSHOT_KEYS
        assert snap == {
            "revision": 0,
            "state": STOPPED,
            "ready": False,
            "playerData": "unknown",
            "playerCount": 0,
            "maxPlayers": None,
            "players": [],
            "banned_players": [],
            "server": {"version": None, "motd": None, "source": None},
            "startedAt": None,
            "autoStopAt": None,
            "restartPending": False,
        }

    def test_status_event_payload(self, env):
        start_running(env)
        sample(env, make_roster("Steve"))

        payload = events_of(env, Event.SERVER_STATUS)[-1]

        assert set(payload) == {
            "revision",
            "state",
            "playerData",
            "playerCount",
            "generation",
        }
        assert payload["revision"] == snapshot(env)["revision"]

    def test_revisions_increase_with_every_notification(self, env):
        identity = launch(env)
        sample(env, make_roster())
        stop(env, identity)

        revisions = [data["revision"] for _kind, data in env.events]
        assert revisions == sorted(set(revisions))
        assert snapshot(env)["revision"] == revisions[-1]

    def test_ready_is_per_generation(self, env):
        identity = launch(env)
        assert snapshot(env)["ready"] is False  # process up, still loading

        sample(env, make_roster())
        assert snapshot(env)["ready"] is True

        sample(env, None, seq=2)  # a failed sample does not unready it
        assert snapshot(env)["ready"] is True
        assert snapshot(env)["playerData"] == "unavailable"

        stop(env, identity)
        assert snapshot(env)["ready"] is False

        env.supervisor.handle(StartCommand())
        assert snapshot(env)["ready"] is False
        env.supervisor.handle(
            LaunchResult(env.supervisor.generation, ProcessIdentity(5, 5.0))
        )
        assert snapshot(env)["ready"] is False

    def test_server_info_comes_from_the_last_valid_roster(self, env):
        identity = launch(env)
        sample(
            env, make_roster("Steve", max_players=30, version="1.20.1", motd="Hello")
        )
        assert snapshot(env)["server"] == {
            "version": "1.20.1",
            "motd": "Hello",
            "source": "rcon",
        }

        # RCON answered but the cached ping failed: keep what we knew.
        sample(
            env, make_roster("Steve", max_players=None, version=None, motd=None), seq=2
        )
        snap = snapshot(env)
        assert snap["maxPlayers"] == 30
        assert snap["server"] == {
            "version": "1.20.1",
            "motd": "Hello",
            "source": "rcon",
        }

        sample(env, make_roster("Steve", source="ping", version="1.20.2"), seq=3)
        assert snapshot(env)["server"]["source"] == "ping"
        assert snapshot(env)["server"]["version"] == "1.20.2"

        stop(env, identity)
        snap = snapshot(env)
        assert snap["maxPlayers"] is None
        assert snap["server"] == {"version": None, "motd": None, "source": None}
        assert snap["playerCount"] == 0
        assert snap["startedAt"] is None

    def test_started_at_is_wall_clock_at_launch(self, env):
        env.wall.advance(42)
        launch(env)

        assert snapshot(env)["startedAt"] == env.wall()

    def test_started_at_of_an_adopted_server_is_its_process_creation(self, env):
        created = env.wall() - 3600
        env.pm.created_at[1001] = created
        # The identity's key is a boot-relative start time, not epoch seconds.
        start_running(env, ProcessIdentity(1001, 427.19))

        assert snapshot(env)["startedAt"] == created

    def test_implausible_creation_time_falls_back_to_now(self, env):
        env.pm.created_at[1001] = env.wall() + 999
        start_running(env, ProcessIdentity(1001, 10.0))

        assert snapshot(env)["startedAt"] == env.wall()

    def test_unknown_creation_time_falls_back_to_now(self, env):
        start_running(env, ProcessIdentity(1001, 10.0))

        assert snapshot(env)["startedAt"] == env.wall()

    def test_player_count_is_zero_unless_running(self, env):
        identity = launch(env)
        sample(env, make_roster("Steve"))
        [stop_request] = env.supervisor.handle(StopCommand())

        assert env.supervisor.state == STOPPING
        assert snapshot(env)["playerCount"] == 0
        env.supervisor.handle(StopResult(stop_request.generation, identity, True))
        assert snapshot(env)["playerCount"] == 0


class TestAutoStop:
    def test_auto_stop_at_is_the_deadline_in_wall_clock(self, env):
        env.settings.autoStop = True
        launch(env)
        env.clock.advance(31)
        env.wall.advance(31)

        sample(env, make_roster())

        assert env.supervisor.auto_stop_deadline == env.clock() + 60
        assert snapshot(env)["autoStopAt"] == env.wall() + 60
        expected = snapshot(env)["autoStopAt"]

        env.clock.advance(10)
        env.wall.advance(10)
        assert snapshot(env)["autoStopAt"] == expected

        sample(env, make_roster("Steve"), seq=2)
        assert snapshot(env)["autoStopAt"] is None

    def test_hidden_players_keep_the_server_up(self, env):
        env.settings.autoStop = True
        launch(env)
        env.clock.advance(31)

        # Names hidden (hide-online-players / ping sample): online is still 2.
        sample(env, Roster(online=2, players=(), max_players=20, source="ping"))

        assert snapshot(env)["playerCount"] == 2
        assert env.supervisor.auto_stop_deadline is None
        env.clock.advance(600)
        assert not any(isinstance(r, StopRequest) for r in env.supervisor.on_tick())

        sample(env, Roster(online=0, players=(), source="ping"), seq=2)
        assert env.supervisor.auto_stop_deadline is not None

    def test_named_players_count_even_if_online_says_zero(self, env):
        env.settings.autoStop = True
        launch(env)
        env.clock.advance(31)

        sample(env, Roster(online=0, players=(OnlinePlayer("Steve"),)))

        assert snapshot(env)["playerCount"] == 1
        assert env.supervisor.auto_stop_deadline is None

    def test_unavailable_keeps_last_count_and_cancels_auto_stop(self, env):
        env.settings.autoStop = True
        launch(env)
        sample(env, make_roster("Steve"))
        env.clock.advance(31)
        sample(env, make_roster(), seq=2)
        assert env.supervisor.auto_stop_deadline is not None
        sample(env, make_roster("Steve"), seq=3)

        sample(env, None, seq=4)

        snap = snapshot(env)
        assert snap["playerData"] == "unavailable"
        assert snap["playerCount"] == 1
        assert snap["players"][0]["currently_online"] is True
        assert env.supervisor.auto_stop_deadline is None
        assert len(env.players.roster_updates) == 3

    @pytest.mark.parametrize("last_roster,stops", [((), True), (("Steve",), False)])
    def test_expiry_needs_a_sample_taken_after_the_deadline(
        self, env, last_roster, stops
    ):
        env.settings.autoStop = True
        identity = launch(env)
        env.clock.advance(31)
        [poll] = env.supervisor.on_tick()
        env.supervisor.handle(
            RosterSample(poll.generation, identity, poll.seq, make_roster())
        )
        assert env.supervisor.auto_stop_deadline == env.clock() + 60

        env.clock.advance(61)
        # A poll becomes due in the same tick: it was in flight at expiry,
        # so its (empty) answer cannot confirm the stop.
        [poll] = env.supervisor.on_tick()
        assert isinstance(poll, PollRequest)
        assert (
            env.supervisor.handle(
                RosterSample(poll.generation, identity, poll.seq, make_roster())
            )
            == []
        )

        [poll] = env.supervisor.on_tick()
        requests = env.supervisor.handle(
            RosterSample(poll.generation, identity, poll.seq, make_roster(*last_roster))
        )

        if stops:
            # Unattended: the backend must never force-kill for it.
            assert [type(r) for r in requests] == [AutoStopRequest]
            assert env.supervisor.state == STOPPING
        else:
            assert requests == []
            assert env.supervisor.state == RUNNING
            assert env.supervisor.auto_stop_deadline is None


class TestRestart:
    def test_restart_from_running_stops_then_starts(self, env):
        first = launch(env)
        old_generation = env.supervisor.generation
        env.events.clear()
        at_stop = []
        env.bus.subscribe(
            Event.SERVER_STOPPED, lambda _data: at_stop.append(snapshot(env))
        )

        command = RestartCommand()
        [stop_request] = env.supervisor.handle(command)

        assert command.result is True and command.done.is_set()
        assert stop_request == StopRequest(old_generation, first)
        assert env.supervisor.state == STOPPING
        assert snapshot(env)["restartPending"] is True

        env.pm.terminate_process(first)
        [request] = env.supervisor.handle(
            StopResult(stop_request.generation, first, True)
        )

        assert isinstance(request, LaunchRequest)
        assert env.supervisor.state == STARTING
        # Generation rules as for stop + start: down (+1), start (+1).
        assert request.generation == env.supervisor.generation == old_generation + 2
        assert at_stop[0]["state"] == STOPPED
        assert at_stop[0]["restartPending"] is True
        assert snapshot(env)["restartPending"] is False

        second = ProcessIdentity(2002, 30.0)
        env.pm.launch_identity = second
        env.pm.launch_process()
        env.supervisor.handle(LaunchResult(request.generation, second))

        assert env.supervisor.state == RUNNING
        assert summary(env) == [
            (Event.LIFECYCLE_CHANGED, STOPPING),
            (Event.SERVER_STOPPED, STOPPED),
            (Event.LIFECYCLE_CHANGED, STARTING),
            (Event.SERVER_STARTED, RUNNING),
        ]
        assert events_of(env, Event.SERVER_STOPPED)[0]["pid"] == first.pid
        assert events_of(env, Event.SERVER_STARTED)[0]["pid"] == second.pid

    def test_restart_after_an_observed_exit(self, env):
        first = launch(env)
        [stop_request] = env.supervisor.handle(RestartCommand())

        # The server exits while the stop worker is still returning.
        env.pm.exit(first)
        [request] = observe(env)

        assert isinstance(request, LaunchRequest)
        assert env.supervisor.state == STARTING
        # The old stop's late result belongs to an older generation.
        assert (
            env.supervisor.handle(StopResult(stop_request.generation, first, True))
            == []
        )
        assert env.supervisor.state == STARTING
        assert [kind for kind, _data in env.events][-3:] == [
            Event.LIFECYCLE_CHANGED,
            Event.SERVER_STOPPED,
            Event.LIFECYCLE_CHANGED,
        ]

    def test_restart_bypasses_the_start_cooldowns(self, env):
        first = launch(env)
        env.supervisor.server_starting_cooldown = 60
        env.supervisor.server_stopping_cooldown = 60

        [stop_request] = env.supervisor.handle(RestartCommand())
        env.pm.terminate_process(first)
        [request] = env.supervisor.handle(
            StopResult(stop_request.generation, first, True)
        )
        assert isinstance(request, LaunchRequest)

        second = ProcessIdentity(2002, 30.0)
        env.pm.launch_identity = second
        env.pm.launch_process()
        env.supervisor.handle(LaunchResult(request.generation, second))
        assert env.supervisor.state == RUNNING

        # A plain stop + start in the same moment is still debounced: the start
        # is accepted but launches only once the debounce is over.
        env.clock.advance(61)
        stop(env, second)
        command = StartCommand()
        assert env.supervisor.handle(command) == []
        assert command.outcome == ActionResult(True, "Starting the server in 60 s.")
        env.clock.advance(60)
        [late] = env.supervisor.on_tick()
        assert isinstance(late, LaunchRequest)

    def test_restart_from_stopped_acts_like_start(self, env):
        command = RestartCommand()

        [request] = env.supervisor.handle(command)

        assert isinstance(request, LaunchRequest)
        assert command.result is True
        assert env.supervisor.state == STARTING
        assert snapshot(env)["restartPending"] is False

    def test_restart_from_stopped_honours_the_start_guard(self, env):
        env.pm.found = ProcessIdentity(2002, 20.0)
        command = RestartCommand()

        assert env.supervisor.handle(command) == []
        assert command.result is False

    @pytest.mark.parametrize(
        ("busy_state", "message"),
        [
            (STARTING, "The server is still launching; restart it once it is running."),
            (STOPPING, "The server is stopping; start it again once it has stopped."),
        ],
    )
    def test_restart_is_rejected_while_busy(self, env, busy_state, message):
        if busy_state == STARTING:
            env.supervisor.handle(StartCommand())
        else:
            launch(env)
            env.supervisor.handle(StopCommand())
        assert env.supervisor.state == busy_state
        events_before = len(env.events)

        command = RestartCommand()
        assert env.supervisor.handle(command) == []

        assert command.outcome == ActionResult(False, message)
        assert command.done.is_set()
        assert env.supervisor.state == busy_state
        assert snapshot(env)["restartPending"] is False
        assert len(env.events) == events_before

    def test_failed_stop_clears_the_pending_restart(self, env):
        first = launch(env)
        [stop_request] = env.supervisor.handle(RestartCommand())

        env.supervisor.handle(StopResult(stop_request.generation, first, False))

        assert env.supervisor.state == RUNNING
        assert snapshot(env)["restartPending"] is False
        assert events_of(env, Event.LIFECYCLE_CHANGED)[-1]["state"] == RUNNING
        # A later exit is a plain stop, not a restart.
        env.pm.exit(first)
        assert observe(env) == []
        assert env.supervisor.state == STOPPED

    def test_stop_during_a_restart_cancels_the_start(self, env):
        first = launch(env)
        [stop_request] = env.supervisor.handle(RestartCommand())

        command = StopCommand()
        assert env.supervisor.handle(command) == []
        assert command.result is True
        assert snapshot(env)["restartPending"] is False

        env.pm.terminate_process(first)
        assert (
            env.supervisor.handle(StopResult(stop_request.generation, first, True))
            == []
        )
        assert env.supervisor.state == STOPPED

    def test_restart_adopts_a_replacement_instead_of_launching(self, env):
        first = launch(env)
        env.supervisor.handle(RestartCommand())
        replacement = ProcessIdentity(3003, 40.0)
        env.pm.exit(first)
        env.pm.found = replacement
        env.pm.alive.add(replacement.pid)

        assert observe(env) == []

        assert env.supervisor.state == RUNNING
        assert env.pm.tracked_identity == replacement
        assert env.pm.launch_calls == [first]
        assert snapshot(env)["restartPending"] is False

    def test_restart_never_launches_next_to_a_surviving_server(self, env):
        first = launch(env)
        [stop_request] = env.supervisor.handle(RestartCommand())
        env.pm.terminate_process(first)
        leftover = ProcessIdentity(4004, 50.0)
        env.pm.found = leftover
        env.pm.alive.add(leftover.pid)

        assert (
            env.supervisor.handle(StopResult(stop_request.generation, first, True))
            == []
        )

        assert env.supervisor.state == STOPPED
        assert snapshot(env)["restartPending"] is False
        observe(env)  # detection adopts the survivor
        assert env.supervisor.state == RUNNING
        assert env.pm.tracked_identity == leftover

    def test_stop_right_after_a_restart_is_accepted(self, env):
        # There is no stop -> stop cooldown: a stop needs RUNNING, which
        # already proves the previous stop is over.
        env.supervisor.server_stopping_cooldown = 60
        first = launch(env)
        [stop_request] = env.supervisor.handle(RestartCommand())
        env.pm.terminate_process(first)
        [request] = env.supervisor.handle(
            StopResult(stop_request.generation, first, True)
        )
        second = ProcessIdentity(2002, 30.0)
        env.pm.launch_identity = second
        env.pm.launch_process()
        env.supervisor.handle(LaunchResult(request.generation, second))

        command = StopCommand()
        [admin_stop] = env.supervisor.handle(command)

        assert command.outcome == ActionResult(True, "Stopping the server.")
        assert type(admin_stop) is StopRequest  # an admin stop may force


class TestCommandOutcomes:
    def test_accepted_commands_say_what_happens(self, env):
        start = StartCommand()
        [launch_request] = env.supervisor.handle(start)
        assert start.outcome == ActionResult(True, "Starting the server.")
        identity = ProcessIdentity(2001, 20.0)
        env.pm.set_known_identity(identity)
        env.supervisor.handle(LaunchResult(launch_request.generation, identity))

        restart = RestartCommand()
        env.supervisor.handle(restart)
        assert restart.outcome == ActionResult(True, "Restarting the server.")

        cancel = StopCommand()
        env.supervisor.handle(cancel)
        assert cancel.outcome == ActionResult(
            True, "Restart cancelled: the server will stay stopped."
        )

    def test_refusals_explain_the_state(self, env):
        def outcome(command):
            env.supervisor.handle(command)
            assert command.done.is_set()
            return command.outcome

        assert outcome(StopCommand()) == ActionResult(
            False, "The server is not running."
        )
        env.supervisor.handle(StartCommand())
        assert outcome(StartCommand()) == ActionResult(
            False, "The server is already starting."
        )
        assert outcome(StopCommand()).message == (
            "The server is still launching; stop it once it is running."
        )
        env.supervisor.handle(
            LaunchResult(env.supervisor.generation, ProcessIdentity(2001, 20.0))
        )
        assert outcome(StartCommand()).message == "The server is already running."
        env.supervisor.handle(StopCommand())
        assert outcome(StopCommand()).message == "The server is already stopping."
        assert outcome(StartCommand()).message == (
            "The server is stopping; start it again once it has stopped."
        )

    def test_a_restart_in_progress_is_named(self, env):
        launch(env)
        env.supervisor.handle(RestartCommand())

        for command in (RestartCommand(), StartCommand()):
            env.supervisor.handle(command)
            assert command.outcome == ActionResult(
                False, "A restart is already in progress."
            )


class TestOfflineEdit:
    def edit_command(self, outcome=None, error=None):
        calls = []

        def edit():
            calls.append(True)
            if error is not None:
                raise error
            return outcome

        return OfflineEditCommand(edit), calls

    def test_runs_while_stopped_and_returns_the_edit_outcome(self, env):
        result = ActionResult(True, "Banned Griefer", details={"changed": True})
        command, calls = self.edit_command(result)

        assert env.supervisor.handle(command) == []

        assert calls == [True]
        assert command.done.is_set()
        assert command.outcome is result

    @pytest.mark.parametrize("state", [RUNNING, STARTING, STOPPING])
    def test_refused_unless_stopped(self, env, state):
        if state == STARTING:
            env.supervisor.handle(StartCommand())
        else:
            launch(env)
        if state == STOPPING:
            env.supervisor.handle(StopCommand())
        command, calls = self.edit_command(ActionResult(True))

        env.supervisor.handle(command)

        assert calls == []
        assert command.outcome.success is False
        assert state.lower() in command.outcome.message

    def test_refused_while_an_unadopted_server_process_exists(self, env):
        env.pm.found = ProcessIdentity(4242, 42.0)
        command, calls = self.edit_command(ActionResult(True))

        env.supervisor.handle(command)

        assert calls == []
        assert command.outcome == ActionResult(False, PROCESS_PRESENT_MESSAGE)

    def test_a_failing_edit_is_a_failure_not_an_exception(self, env):
        command, calls = self.edit_command(error=OSError("disk full"))

        env.supervisor.handle(command)

        assert calls == [True]
        assert command.outcome == ActionResult(
            False, "The ban list could not be changed; see the monitor log."
        )
