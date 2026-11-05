"""MinecraftController: facade + runtime integration, moderation rules, and an
end-to-end run against the dummy Minecraft server."""

import threading
from types import SimpleNamespace

import pytest

from src.events import Event, EventBus
from src.lifecycle.messages import (
    ActionResult,
    LifecycleState,
    ProcessIdentity,
    ProcessObservation,
    ProcessSnapshot,
    Roster,
    RosterSample,
    StartCommand,
    StopResult,
)
from src.minecraft.client import RconError
from src.lifecycle.supervisor import PROCESS_PRESENT_MESSAGE
from src.minecraft_control import MinecraftController
from test.support import wait_until
from test.support.fakes import (
    FakeBanlistManager,
    FakeClient,
    FakeClock,
    FakePlayerManager,
    FakeProcessManager,
    FakeWallClock,
    HandledSpy,
    OffsetClock,
    fake_uuid,
    make_roster,
)

RUNNING = LifecycleState.RUNNING
STOPPED = LifecycleState.STOPPED
STARTING = LifecycleState.STARTING
STOPPING = LifecycleState.STOPPING
CONTROLLER_THREAD_NAMES = {
    "minecraft-lifecycle",
    "minecraft-detection",
    "minecraft-poll",
    "minecraft-process",
}
LIFECYCLE_EVENTS = (
    Event.SERVER_STARTED,
    Event.SERVER_STOPPED,
    Event.SERVER_STATUS,
    Event.LIFECYCLE_CHANGED,
)


class Harness:
    def __init__(self, settings):
        self.settings = settings
        self.bus = EventBus()
        self.client = FakeClient()
        self.pm = FakeProcessManager()
        self.players = FakePlayerManager(bus=self.bus)
        self.bans = FakeBanlistManager(bus=self.bus)
        self.client.banlist = self.bans
        self.clock = FakeClock()
        self.wall = FakeWallClock()
        self.controller = MinecraftController(
            client=self.client,
            process_manager=self.pm,
            player_manager=self.players,
            banlist_manager=self.bans,
            bus=self.bus,
            clock=self.clock,
            wall_clock=self.wall,
        )
        self.controller.BAN_VERIFY_DELAY = 0
        self.spy = HandledSpy(self.controller._supervisor)
        supervisor = self.controller._supervisor
        supervisor.server_starting_cooldown = 0
        supervisor.server_stopping_cooldown = 0
        supervisor.poll_interval = 0
        self.events = []
        for event_type in LIFECYCLE_EVENTS:
            self.bus.subscribe(
                event_type,
                lambda data, kind=event_type: self.events.append((kind, data)),
            )

    @property
    def supervisor(self):
        return self.controller._supervisor

    def start(self):
        self.controller.start()

    def close(self):
        self.controller.close()

    def snapshot(self):
        return self.controller.get_snapshot()

    def start_with_process(self, identity=None):
        identity = identity or ProcessIdentity(1001, 10.0)
        self.pm.found = identity
        self.pm.alive.add(identity.pid)
        self.start()
        wait_until(lambda: self.controller.state == RUNNING)
        return identity

    def events_of(self, event_type):
        return [data for kind, data in self.events if kind == event_type]

    def settle(self, kind, more=2):
        """Wait until the lifecycle thread has consumed `more` further messages
        of `kind` (fresh polls / scans), instead of sleeping."""
        target = self.spy.count(kind) + more
        wait_until(lambda: self.spy.count(kind) >= target)

    def online_names(self):
        return {p["name"] for p in self.snapshot()["players"] if p["currently_online"]}


@pytest.fixture
def harness(mock_settings):
    mock_settings.pollingRate = 0.01
    mock_settings.autoStop = False
    mock_settings.autoStopDelay = 60
    h = Harness(mock_settings)
    yield h
    h.close()


class TestControllerLifecycle:
    def test_constructor_does_not_start_workers(self, harness):
        assert not any(t.name in CONTROLLER_THREAD_NAMES for t in threading.enumerate())
        assert harness.controller.start_server().success is False

    def test_stale_pid_startup_reconciles_and_does_not_block_start(self, harness):
        harness.players.seed("Steve", online=True)
        harness.pm.identity = ProcessIdentity(999, 1.0)
        harness.pm.launch_identity = ProcessIdentity(2001, 20.0)
        harness.start()

        wait_until(lambda: harness.pm.tracked_identity is None)
        wait_until(lambda: harness.players.get_player_count() == 0)
        snapshot = harness.snapshot()
        assert snapshot["state"] == STOPPED
        assert snapshot["playerData"] == "unknown"
        assert [p["name"] for p in snapshot["players"]] == ["Steve"]
        assert snapshot["players"][0]["currently_online"] is False

        assert harness.controller.start_server().success is True
        wait_until(lambda: harness.controller.state == RUNNING)

    def test_start_command_commits_starting_then_running(self, harness):
        harness.pm.launch_identity = ProcessIdentity(2001, 20.0)
        harness.start()

        assert harness.controller.start_server().success is True
        wait_until(lambda: harness.controller.state == RUNNING)

        states = [data["state"] for data in harness.events_of(Event.LIFECYCLE_CHANGED)]
        assert states[0] == STARTING
        assert harness.events_of(Event.SERVER_STARTED)[0]["state"] == RUNNING
        assert harness.pm.tracked_identity == ProcessIdentity(2001, 20.0)
        assert harness.snapshot()["startedAt"] == harness.wall()

    def test_tracking_is_recorded_before_the_started_notification(self, harness):
        identity = ProcessIdentity(2001, 20.0)
        harness.pm.launch_identity = identity
        tracked_at_notification = []
        harness.bus.subscribe(
            Event.SERVER_STARTED,
            lambda _data: tracked_at_notification.append(harness.pm.tracked_identity),
        )
        harness.start()

        assert harness.controller.start_server().success is True
        wait_until(lambda: harness.controller.state == RUNNING)

        assert tracked_at_notification == [identity]

    def test_failed_launch_returns_to_stopped_without_started_notification(
        self, harness
    ):
        harness.pm.launch_identity = None
        harness.start()

        assert harness.controller.start_server().success is True
        wait_until(
            lambda: harness.controller.state == STOPPED and harness.pm.launch_calls
        )
        assert harness.events_of(Event.SERVER_STARTED) == []

    def test_adopts_an_existing_process_as_running(self, harness):
        identity = harness.start_with_process()

        assert harness.pm.tracked_identity == identity
        assert harness.snapshot()["state"] == RUNNING
        assert harness.events_of(Event.SERVER_STARTED)[0]["pid"] == identity.pid

    def test_ready_once_the_server_answers(self, harness):
        harness.client.available = False
        harness.start_with_process()
        harness.settle(RosterSample, more=1)  # a failed poll was consumed
        assert harness.snapshot()["ready"] is False  # process up, still loading

        harness.client.available = True
        wait_until(lambda: harness.snapshot()["ready"] is True)

    def test_stop_commits_down_and_releases_players_before_notification(self, harness):
        harness.client.roster = make_roster("Steve")
        identity = harness.start_with_process()
        wait_until(lambda: harness.snapshot()["playerData"] == "ok")
        online_at_stop = []
        harness.bus.subscribe(
            Event.SERVER_STOPPED,
            lambda _data: online_at_stop.append(harness.players.get_player_count()),
        )

        assert harness.controller.stop_server().success is True
        wait_until(lambda: harness.controller.state == STOPPED)

        snapshot = harness.snapshot()
        assert snapshot["playerData"] == "unknown"
        assert snapshot["playerCount"] == 0
        assert [p["name"] for p in snapshot["players"]] == ["Steve"]
        assert snapshot["players"][0]["currently_online"] is False
        assert online_at_stop == [0]
        assert harness.pm.tracked_identity is None
        assert harness.events_of(Event.SERVER_STOPPED)[0]["pid"] == identity.pid
        states = [data["state"] for data in harness.events_of(Event.LIFECYCLE_CHANGED)]
        assert states[-1] == STOPPING

    def test_failed_stop_returns_to_running_and_resumes_polling(self, harness):
        harness.client.roster = make_roster("Steve")
        harness.start_with_process()
        wait_until(lambda: harness.snapshot()["playerData"] == "ok")

        harness.client.roster = make_roster("Alex")
        harness.pm.terminate_result = False
        assert harness.controller.stop_server().success is True
        wait_until(lambda: harness.controller.state == RUNNING)
        wait_until(lambda: "Alex" in harness.online_names())

        harness.pm.terminate_result = True
        assert harness.controller.stop_server().success is True
        wait_until(lambda: harness.controller.state == STOPPED)

    def test_pending_stop_stays_stopping_until_the_process_exits(self, harness):
        identity = harness.start_with_process()
        harness.pm.terminate_result = None

        assert harness.controller.stop_server().success is True
        wait_until(lambda: harness.spy.count(StopResult) == 1)
        harness.settle(ProcessObservation, more=1)  # a scan after the result
        assert harness.controller.state == STOPPING

        harness.pm.exit(identity)
        wait_until(lambda: harness.controller.state == STOPPED)

    def test_blocked_poll_after_stop_is_discarded(self, harness):
        harness.start_with_process()
        wait_until(lambda: harness.snapshot()["playerData"] == "ok")
        gate = harness.client.hold(make_roster("Ghost"))
        wait_until(harness.client.entered.is_set)

        assert harness.controller.stop_server().success is True
        wait_until(lambda: harness.controller.state == STOPPED)
        handled = harness.spy.count(RosterSample)
        gate.set()
        wait_until(
            lambda: harness.spy.count(RosterSample) > handled
        )  # late answer consumed

        snapshot = harness.snapshot()
        assert snapshot["state"] == STOPPED
        assert snapshot["playerCount"] == 0
        assert snapshot["players"] == []
        assert harness.pm.identity is None

    def test_stop_and_restart_discards_the_previous_generations_roster(self, harness):
        harness.start_with_process(ProcessIdentity(1001, 10.0))
        wait_until(lambda: harness.snapshot()["playerData"] == "ok")
        gate = harness.client.hold(make_roster("Ghost"))
        wait_until(harness.client.entered.is_set)

        assert harness.controller.stop_server().success is True
        wait_until(lambda: harness.controller.state == STOPPED)
        harness.pm.launch_identity = ProcessIdentity(2002, 20.0)
        harness.client.roster = make_roster()
        assert harness.controller.start_server().success is True
        wait_until(lambda: harness.controller.state == RUNNING)

        handled = harness.spy.count(RosterSample)
        gate.set()
        # The late Ghost answer comes first, then this generation's polls.
        wait_until(lambda: harness.spy.count(RosterSample) >= handled + 2)
        assert harness.snapshot()["playerData"] == "ok"
        assert harness.snapshot()["playerCount"] == 0
        assert harness.players.get_player_count() == 0

    def test_delayed_process_observation_cannot_adopt_after_down(self, harness):
        identity = harness.start_with_process()
        gate = threading.Event()
        harness.pm.gated_snapshot = ProcessSnapshot(
            tracked=identity, tracked_running=True, found=ProcessIdentity(2002, 20.0)
        )
        harness.pm.observe_entered.clear()
        harness.pm.observe_gate = gate
        assert harness.pm.observe_entered.wait(1)

        assert harness.controller.stop_server().success is True
        wait_until(lambda: harness.controller.state == STOPPED)
        started_before = len(harness.events_of(Event.SERVER_STARTED))
        scans = harness.spy.count(ProcessObservation)
        gate.set()
        wait_until(lambda: harness.spy.count(ProcessObservation) > scans)  # consumed

        assert harness.controller.state == STOPPED
        assert len(harness.events_of(Event.SERVER_STARTED)) == started_before

    def test_unadopted_found_process_blocks_a_start_until_detection_adopts(
        self, harness
    ):
        identity = ProcessIdentity(2002, 20.0)
        harness.pm.found = identity
        harness.pm.alive.add(identity.pid)
        harness.pm.gated_snapshot = ProcessSnapshot(
            tracked=None, tracked_running=False, found=identity
        )
        harness.pm.launch_identity = ProcessIdentity(3003, 30.0)
        gate = threading.Event()
        harness.pm.observe_entered.clear()
        harness.pm.observe_gate = gate
        harness.start()
        assert harness.pm.observe_entered.wait(1)
        assert harness.controller.state == STOPPED

        assert harness.controller.start_server().success is False
        assert harness.pm.launch_calls == []

        gate.set()
        wait_until(lambda: harness.controller.state == RUNNING)
        assert harness.pm.tracked_identity == identity
        assert harness.pm.launch_calls == []

    def test_reused_pid_is_replaced_by_the_actual_found_server(self, harness):
        reused = ProcessIdentity(999, 1.0)
        actual = ProcessIdentity(2002, 20.0)
        harness.pm.identity = reused
        harness.pm.alive.add(reused.pid)
        harness.pm.found = actual
        harness.pm.alive.add(actual.pid)
        harness.start()

        wait_until(lambda: harness.controller.state == RUNNING)
        assert harness.pm.tracked_identity == actual
        assert harness.pm.terminate_calls == []
        assert harness.events_of(Event.SERVER_STARTED)[-1]["pid"] == actual.pid
        assert harness.events_of(Event.SERVER_STOPPED) == []

    def test_duplicate_process_observations_commit_down_once(self, harness):
        identity = harness.start_with_process()

        harness.pm.exit(identity)
        wait_until(lambda: harness.controller.state == STOPPED)
        harness.settle(ProcessObservation)  # more scans of the same exit

        assert len(harness.events_of(Event.SERVER_STOPPED)) == 1

    def test_external_replacement_is_adopted_as_a_new_generation(self, harness):
        first = harness.start_with_process(ProcessIdentity(1001, 10.0))
        generation = harness.controller.generation
        replacement = ProcessIdentity(2002, 20.0)
        harness.pm.alive.add(replacement.pid)
        harness.pm.exit(first)
        harness.pm.found = replacement

        wait_until(lambda: harness.pm.tracked_identity == replacement)
        assert harness.controller.state == RUNNING
        assert harness.controller.generation > generation
        assert harness.events_of(Event.SERVER_STOPPED)[0]["pid"] == first.pid
        assert harness.events_of(Event.SERVER_STARTED)[-1]["pid"] == replacement.pid

    def test_restart_waits_for_old_stop_and_discards_its_stale_result(self, harness):
        first = harness.start_with_process(ProcessIdentity(1001, 10.0))
        harness.pm.terminate_block = threading.Event()
        harness.pm.terminate_mutates = False
        assert harness.controller.stop_server().success is True
        wait_until(lambda: harness.controller.state == STOPPING)
        assert harness.pm.terminate_entered.wait(1)

        # The old process exits on its own and the supervisor commits DOWN.
        harness.pm.exit(first)
        wait_until(lambda: harness.controller.state == STOPPED)

        harness.pm.launch_identity = ProcessIdentity(2002, 20.0)
        assert harness.controller.start_server().success is True
        assert harness.controller.state == STARTING
        assert harness.pm.launch_calls == []

        # The shared worker finishes the old stop before launching a replacement.
        harness.pm.terminate_block.set()
        wait_until(lambda: harness.controller.state == RUNNING)
        assert len(harness.events_of(Event.SERVER_STOPPED)) == 1

    def test_restart_server_stops_then_starts(self, harness):
        harness.controller._supervisor.server_starting_cooldown = 60
        harness.controller._supervisor.server_stopping_cooldown = 60
        first = harness.start_with_process()
        second = ProcessIdentity(2002, 20.0)
        harness.pm.launch_identity = second
        harness.events.clear()

        assert harness.controller.restart_server().success is True

        wait_until(lambda: harness.pm.tracked_identity == second)
        wait_until(lambda: harness.controller.state == RUNNING)
        lifecycle = [
            (kind, data["state"])
            for kind, data in harness.events
            if kind != Event.SERVER_STATUS
        ]
        assert lifecycle == [
            (Event.LIFECYCLE_CHANGED, STOPPING),
            (Event.SERVER_STOPPED, STOPPED),
            (Event.LIFECYCLE_CHANGED, STARTING),
            (Event.SERVER_STARTED, RUNNING),
        ]
        assert harness.pm.terminate_calls == [first]
        assert harness.snapshot()["restartPending"] is False

    def test_restart_server_from_stopped_starts(self, harness):
        harness.pm.launch_identity = ProcessIdentity(2002, 20.0)
        harness.start()

        assert harness.controller.restart_server().success is True
        wait_until(lambda: harness.controller.state == RUNNING)

    def test_repeated_start_and_close_leave_no_threads_or_subscriptions(self, harness):
        baseline = harness.bus.subscriber_count()
        for _ in range(3):
            harness.controller.start()
            harness.controller.close()

        assert harness.bus.subscriber_count() == baseline
        assert not any(t.name in CONTROLLER_THREAD_NAMES for t in threading.enumerate())
        assert harness.pm.closed is True


class TestControllerAutoStop:
    @pytest.fixture(autouse=True)
    def auto_stop_on(self, harness):
        harness.settings.autoStop = True

    def arm(self, harness):
        harness.clock.advance(31)  # past the startup guard
        wait_until(lambda: harness.supervisor.auto_stop_deadline is not None)
        assert harness.snapshot()["autoStopAt"] is not None

    def test_blocked_poll_at_deadline_does_not_stop_on_an_old_empty_sample(
        self, harness
    ):
        harness.start_with_process()
        wait_until(lambda: harness.snapshot()["playerData"] == "ok")
        self.arm(harness)

        # A poll is already in flight when the deadline expires.
        gate = harness.client.hold()
        wait_until(harness.client.entered.is_set)
        harness.clock.advance(harness.settings.autoStopDelay + 1)
        # The expiry was handled: it always asks for one more sample.
        wait_until(lambda: harness.supervisor.auto_stop_deadline is None)
        assert harness.controller.state == RUNNING

        # The blocked poll answers empty; a fresh sample shows a player.
        harness.client.roster = make_roster("Steve")
        gate.set()
        wait_until(lambda: "Steve" in harness.online_names())
        assert harness.controller.state == RUNNING
        assert harness.snapshot()["autoStopAt"] is None

    def test_auto_stop_waits_for_a_fresh_empty_sample_to_confirm(self, harness):
        harness.start_with_process()
        wait_until(lambda: harness.snapshot()["playerData"] == "ok")
        self.arm(harness)
        gate = harness.client.hold()
        wait_until(harness.client.entered.is_set)
        harness.clock.advance(harness.settings.autoStopDelay + 1)
        # The expiry was handled: it always asks for one more sample.
        wait_until(lambda: harness.supervisor.auto_stop_deadline is None)
        assert harness.controller.state == RUNNING

        gate.set()
        wait_until(lambda: harness.controller.state == STOPPED)

    def test_hidden_players_keep_the_server_up(self, harness):
        harness.client.roster = Roster(
            online=3, players=(), max_players=20, source="ping"
        )
        harness.start_with_process()
        wait_until(lambda: harness.snapshot()["playerCount"] == 3)

        harness.clock.advance(31 + harness.settings.autoStopDelay * 3)
        harness.settle(RosterSample)

        assert harness.controller.state == RUNNING
        assert harness.snapshot()["autoStopAt"] is None

    def test_unavailable_roster_keeps_players_and_cancels_auto_stop(self, harness):
        harness.client.roster = make_roster("Steve")
        harness.start_with_process()
        wait_until(lambda: harness.snapshot()["playerData"] == "ok")
        left = []
        harness.bus.subscribe(Event.PLAYER_LEFT, left.append)

        harness.client.available = False
        wait_until(lambda: harness.snapshot()["playerData"] == "unavailable")
        snapshot = harness.snapshot()
        assert snapshot["playerCount"] == 1
        assert snapshot["players"][0]["currently_online"] is True
        assert left == []

        harness.clock.advance(120)
        harness.settle(RosterSample)
        assert harness.controller.state == RUNNING

        harness.client.available = True
        harness.client.roster = make_roster()
        wait_until(lambda: harness.snapshot()["playerCount"] == 0)
        wait_until(lambda: harness.supervisor.auto_stop_deadline is not None)
        harness.clock.advance(harness.settings.autoStopDelay + 5)
        wait_until(lambda: harness.controller.state == STOPPED)

    def test_old_auto_stop_deadline_cannot_stop_a_restarted_generation(self, harness):
        harness.start_with_process(ProcessIdentity(1001, 10.0))
        wait_until(lambda: harness.snapshot()["playerData"] == "ok")
        harness.clock.advance(30)
        wait_until(lambda: harness.supervisor.auto_stop_deadline is not None)

        assert harness.controller.stop_server().success is True
        wait_until(lambda: harness.controller.state == STOPPED)
        harness.pm.launch_identity = ProcessIdentity(2002, 20.0)
        assert harness.controller.start_server().success is True
        wait_until(lambda: harness.controller.state == RUNNING)

        # The first generation's deadline would have passed by now.
        harness.clock.advance(31)
        harness.settle(RosterSample)
        assert harness.controller.state == RUNNING

        harness.clock.advance(harness.settings.autoStopDelay + 5)
        wait_until(lambda: harness.controller.state == STOPPED)


# ----------------------------------------------------------------------
# Moderation rules (supervisor driven directly; no worker threads)
# ----------------------------------------------------------------------


@pytest.fixture
def moderation(mock_settings):
    """enter(state) -> a Harness whose running controller is held in `state`
    (offline ban edits run on the lifecycle thread, so the runtime runs)."""
    mock_settings.pollingRate = 0.01
    h = Harness(mock_settings)
    gates = []

    def hold(attribute):
        gate = threading.Event()
        gates.append(gate)
        setattr(h.pm, attribute, gate)

    def enter(state):
        if state in (RUNNING, STOPPING):
            h.start_with_process()
        else:
            h.start()
            wait_until(lambda: h.events_of(Event.SERVER_STOPPED))  # reconciled
        if state == STARTING:
            hold("launch_block")
            h.pm.launch_identity = ProcessIdentity(2002, 20.0)
            assert h.controller.start_server().success
            wait_until(h.pm.launch_entered.is_set)
        if state == STOPPING:
            hold("terminate_block")
            assert h.controller.stop_server().success
            wait_until(h.pm.terminate_entered.is_set)
        assert h.controller.state == state
        return h

    yield enter
    for gate in gates:
        gate.set()
    h.close()


def hold_detection(h):
    """Park the detection worker (it would adopt any process it finds);
    returns the gate that releases it."""
    gate = threading.Event()
    h.pm.gated_snapshot = ProcessSnapshot(
        tracked=None, tracked_running=False, found=None
    )
    h.pm.observe_entered.clear()
    h.pm.observe_gate = gate
    assert h.pm.observe_entered.wait(2)
    return gate


def assert_failure(result, *fragments):
    assert isinstance(result, ActionResult)
    assert result.success is False
    for fragment in fragments:
        assert fragment.lower() in result.message.lower()


class TestModerationRules:
    def test_kick_over_rcon_while_running(self, moderation):
        h = moderation(RUNNING)

        result = h.controller.kick_player("Steve", "AFK\ntoo long")

        assert result.success is True
        assert h.client.kick_calls == [("Steve", "AFK too long")]

    @pytest.mark.parametrize("state", [STOPPED, STARTING, STOPPING])
    def test_kick_needs_a_running_server(self, moderation, state):
        h = moderation(state)

        assert_failure(h.controller.kick_player("Steve"), state.lower(), "running")
        assert h.client.kick_calls == []

    def test_kick_needs_rcon(self, moderation):
        h = moderation(RUNNING)
        h.client.rcon_enabled = False

        assert_failure(h.controller.kick_player("Steve"), "RCON")
        assert h.client.kick_calls == []

    def test_ban_over_rcon_is_verified_against_the_file(self, moderation):
        h = moderation(RUNNING)
        added = []
        h.bus.subscribe(Event.BAN_ADDED, added.append)

        result = h.controller.ban_player("Griefer", "griefing")

        assert result.success is True
        assert h.client.ban_calls == [("Griefer", "griefing")]
        assert h.bans.is_banned("Griefer")
        assert h.bans.refresh_calls >= 1
        assert added == [{"name": "Griefer", "uuid": fake_uuid("Griefer")}]
        assert h.bans.offline_add_calls == []

    def test_ban_fails_when_the_server_did_not_write_it(self, moderation):
        h = moderation(RUNNING)
        h.client.ban_writes_file = False

        assert_failure(h.controller.ban_player("Griefer"), "did not record")
        assert h.bans.refresh_calls == h.controller.BAN_VERIFY_ATTEMPTS

    def test_ban_reports_the_server_reply_when_rcon_refuses(self, moderation):
        h = moderation(RUNNING)
        h.client.ban_writes_file = False
        h.client.ban_result = ActionResult(False, "That player does not exist")

        assert_failure(h.controller.ban_player("Nobody"), "does not exist")
        assert h.bans.refresh_calls == 1

    def test_already_banned_counts_as_success(self, moderation):
        h = moderation(RUNNING)
        h.bans.server_ban("Griefer", fake_uuid("Griefer"))
        h.client.ban_writes_file = False
        h.client.ban_result = ActionResult(
            False, "Nothing changed. The player is already banned"
        )

        assert h.controller.ban_player("Griefer").success is True

    def test_unban_over_rcon_is_verified_against_the_file(self, moderation):
        h = moderation(RUNNING)
        h.bans.server_ban("Griefer", fake_uuid("Griefer"))

        assert h.controller.unban_player("Griefer").success is True
        assert h.client.unban_calls == ["Griefer"]
        assert not h.bans.is_banned("Griefer")

        h.bans.server_ban("Griefer", fake_uuid("Griefer"))
        h.client.ban_writes_file = False
        assert_failure(h.controller.unban_player("Griefer"), "did not record")

    def test_ban_offline_while_stopped_uses_the_tracked_uuid(self, moderation):
        h = moderation(STOPPED)
        h.players.seed("Griefer", uuid="11111111-2222-3333-4444-555555555555")

        result = h.controller.ban_player("griefer", "x-ray")

        assert result.success is True
        assert h.bans.offline_add_calls == [
            ("griefer", "11111111-2222-3333-4444-555555555555", "x-ray")
        ]
        assert h.client.ban_calls == []

    def test_ban_offline_falls_back_to_the_usercache_uuid(self, moderation):
        h = moderation(STOPPED)
        h.bans.usercache["griefer"] = "99999999-2222-3333-4444-555555555555"

        assert h.controller.ban_player("Griefer").success is True
        assert h.bans.offline_add_calls[0][1] == "99999999-2222-3333-4444-555555555555"

    def test_ban_offline_without_a_uuid_reports_the_banlist_failure(self, moderation):
        h = moderation(STOPPED)

        assert_failure(h.controller.ban_player("Stranger"), "start the server")
        assert not h.bans.is_banned("Stranger")

    def test_unban_offline_while_stopped(self, moderation):
        h = moderation(STOPPED)
        h.bans.server_ban("Griefer", fake_uuid("Griefer"))

        assert h.controller.unban_player("Griefer").success is True
        assert h.bans.offline_remove_calls == ["Griefer"]
        assert h.client.unban_calls == []

    def test_no_offline_edit_while_an_unadopted_server_runs(self, moderation):
        h = moderation(STOPPED)
        h.players.seed("Griefer", uuid=fake_uuid("Griefer"))
        gate = hold_detection(h)
        try:
            h.pm.found = ProcessIdentity(4242, 42.0)

            assert h.controller.ban_player("Griefer") == ActionResult(
                False, PROCESS_PRESENT_MESSAGE
            )
            assert h.controller.unban_player("Griefer") == ActionResult(
                False, PROCESS_PRESENT_MESSAGE
            )
        finally:
            gate.set()
        assert h.bans.offline_add_calls == []
        assert h.bans.offline_remove_calls == []

    def test_an_offline_edit_and_a_start_never_interleave(self, moderation):
        h = moderation(STOPPED)
        h.players.seed("Griefer", uuid=fake_uuid("Griefer"))
        h.pm.launch_identity = ProcessIdentity(2002, 20.0)
        order = []
        entered, release = threading.Event(), threading.Event()
        add_ban = h.bans.add_ban_offline
        launch = h.pm.launch_process

        def slow_add(*args):
            entered.set()
            release.wait(5)
            result = add_ban(*args)
            order.append("ban written")
            return result

        def recorded_launch():
            order.append("launch")
            return launch()

        h.bans.add_ban_offline = slow_add
        h.pm.launch_process = recorded_launch
        results = []
        banner = threading.Thread(
            target=lambda: results.append(h.controller.ban_player("Griefer"))
        )
        banner.start()
        assert entered.wait(2)  # the edit runs on the lifecycle thread

        # A wake arrives meanwhile: its start waits behind the edit.
        start = StartCommand()
        starter = threading.Thread(
            target=h.controller._runtime.submit_command, args=(start,)
        )
        starter.start()
        queue = h.controller._runtime._observations
        wait_until(lambda: _queued(queue, start))
        release.set()
        banner.join(5)
        starter.join(5)

        wait_until(lambda: h.controller.state == RUNNING)
        assert results[0].success is True
        assert start.result is True
        assert order == ["ban written", "launch"]

    @pytest.mark.parametrize("state", [STARTING, STOPPING])
    def test_bans_cannot_change_while_starting_or_stopping(self, moderation, state):
        h = moderation(state)

        assert_failure(h.controller.ban_player("Griefer"), state.lower())
        assert_failure(h.controller.unban_player("Griefer"), state.lower())
        assert h.client.ban_calls == []
        assert h.client.unban_calls == []
        assert h.bans.offline_add_calls == []
        assert h.bans.offline_remove_calls == []

    def test_bans_cannot_change_while_running_without_rcon(self, moderation):
        h = moderation(RUNNING)
        h.client.rcon_enabled = False

        assert_failure(h.controller.ban_player("Griefer"), "RCON", "stop the server")
        assert_failure(h.controller.unban_player("Griefer"), "RCON")
        assert h.bans.offline_add_calls == []

    def test_console_command_over_rcon(self, moderation):
        h = moderation(RUNNING)
        h.client.command_output = "There are 0 of a max of 20 players online:"

        result = h.controller.run_console_command("  list  ")

        assert result.success is True
        assert result.output == "There are 0 of a max of 20 players online:"
        assert h.client.commands == ["list"]

    def test_console_command_failures(self, moderation):
        h = moderation(RUNNING)
        h.client.command_error = RconError("connection refused")

        assert_failure(h.controller.run_console_command("list"), "connection refused")
        assert_failure(h.controller.run_console_command("   "), "command")
        assert_failure(h.controller.run_console_command("say a\nop me"), "single line")
        h.client.rcon_enabled = False
        assert_failure(h.controller.run_console_command("list"), "RCON")

    def test_console_command_needs_a_running_server(self, moderation):
        h = moderation(STOPPED)

        assert_failure(h.controller.run_console_command("list"), "stopped")
        assert h.client.commands == []

    @pytest.mark.parametrize(
        "name", ["", "   ", None, "two words", "evil\nop", "x" * 65]
    )
    def test_invalid_names_never_reach_the_server(self, moderation, name):
        h = moderation(RUNNING)

        for action in (
            h.controller.kick_player,
            h.controller.ban_player,
            h.controller.unban_player,
        ):
            assert action(name).success is False
        assert h.client.kick_calls == h.client.ban_calls == h.client.unban_calls == []

    def test_nothing_raises(self, moderation):
        h = moderation(RUNNING)

        def boom(*_args, **_kwargs):
            raise RuntimeError("unexpected")

        h.client.kick_player = h.client.ban_player = h.client.unban_player = boom
        h.client.run_command = boom
        h.bans.refresh = h.bans.is_banned = h.bans.get_banned_players = boom

        assert h.controller.kick_player("Steve").success is False
        assert h.controller.ban_player("Steve").success is False
        assert h.controller.unban_player("Steve").success is False
        assert h.controller.run_console_command("list").success is False
        assert h.controller.get_banned_players() == []
        assert h.controller.get_snapshot()["banned_players"] == []


# ----------------------------------------------------------------------
# End to end: real process manager, client and managers + dummy server
# ----------------------------------------------------------------------


def _real_stack_available():
    try:
        from test.support import dummy_server  # noqa: F401
    except ImportError as e:
        return f"dummy server helper unavailable: {e}"
    return None


@pytest.mark.skipif(
    _real_stack_available() is not None, reason=str(_real_stack_available())
)
class TestEndToEndWithDummyServer:
    """The monitor launches the dummy, restarts it and auto-stops it."""

    @pytest.fixture
    def stack(self, mock_settings, monkeypatch, tmp_path):
        from src.banlist_manager import BanlistManager
        from src.minecraft.client import MinecraftClient
        from src.player_manager import PlayerManager
        from src.process_manager import create_process_manager
        from test.support import free_tcp_ports
        from test.support.dummy_server import (
            DUMMY_PROCESS_NAME,
            dummy_rcon,
            dummy_start_command,
            write_server_files,
        )

        game_port, rcon_port = free_tcp_ports(2)
        server_dir = mock_settings.serverDir
        write_server_files(server_dir, game_port, rcon_port, "dummy-rcon")
        monkeypatch.setenv("DUMMY_PLAYERS", "Steve")
        monkeypatch.setenv("DUMMY_STOP_DELAY", "0.2")
        mock_settings.startCommand = dummy_start_command()
        mock_settings.processName = DUMMY_PROCESS_NAME
        mock_settings.serverPort = game_port
        mock_settings.rconPort = rcon_port
        mock_settings.rconPassword = "dummy-rcon"
        mock_settings.pollingRate = 0.2
        mock_settings.stopTimeout = 15
        mock_settings.autoStop = True
        mock_settings.autoStopDelay = 5

        bus = EventBus()
        client = MinecraftClient(
            "127.0.0.1", game_port, rcon_port, "dummy-rcon", timeout=2.0
        )
        process_manager = create_process_manager(graceful_stopper=client.request_stop)
        players = PlayerManager(bus=bus, data_file=str(tmp_path / "players.json"))
        bans = BanlistManager(bus=bus, server_dir=server_dir)
        clock = OffsetClock()  # real time flows; the test jumps over the guard
        controller = MinecraftController(
            client, process_manager, players, bans, bus, clock=clock
        )
        controller._supervisor.poll_interval = 0.1
        events = []
        for event_type in LIFECYCLE_EVENTS:
            if event_type != Event.SERVER_STATUS:
                bus.subscribe(
                    event_type,
                    lambda data, kind=event_type: events.append((kind, data["state"])),
                )
        launched = []

        def rcon(command):
            return dummy_rcon(rcon_port, "dummy-rcon", command, timeout=2.0)

        try:
            yield SimpleNamespace(
                controller=controller,
                process_manager=process_manager,
                clock=clock,
                events=events,
                rcon=rcon,
                launched=launched,
                bans=bans,
                settings=mock_settings,
            )
        finally:
            controller.close()
            client.close()
            _kill_launched(process_manager, launched)

    def wait_ready(self, stack, timeout=20):
        wait_until(lambda: stack.controller.get_snapshot()["ready"], timeout=timeout)
        identity = stack.process_manager.tracked_identity
        if identity is not None and identity.pid not in stack.launched:
            stack.launched.append(identity.pid)
        return identity

    def test_start_restart_moderate_and_auto_stop(self, stack):
        controller = stack.controller
        controller.start()
        wait_until(
            lambda: (Event.SERVER_STOPPED, STOPPED) in stack.events
        )  # reconciled

        assert controller.start_server().success is True
        first = self.wait_ready(stack)
        wait_until(lambda: controller.get_snapshot()["playerCount"] == 1)
        snapshot = controller.get_snapshot()
        assert snapshot["state"] == RUNNING
        assert snapshot["server"]["source"] == "rcon"
        assert [p["name"] for p in snapshot["players"] if p["currently_online"]] == [
            "Steve"
        ]

        # Moderation over RCON, verified against the file the server writes.
        assert controller.ban_player("Griefer", "testing").success is True
        assert stack.bans.is_banned("Griefer")
        assert controller.unban_player("Griefer").success is True
        assert controller.run_console_command("list").success is True

        stack.events.clear()
        assert controller.restart_server().success is True
        wait_until(
            lambda: controller.state == RUNNING and stack.events[-1][1] == RUNNING,
            timeout=30,
        )
        second = self.wait_ready(stack)
        assert second.pid != first.pid
        assert stack.events == [
            (Event.LIFECYCLE_CHANGED, STOPPING),
            (Event.SERVER_STOPPED, STOPPED),
            (Event.LIFECYCLE_CHANGED, STARTING),
            (Event.SERVER_STARTED, RUNNING),
        ]

        # Everyone leaves; after the startup guard and the delay, auto-stop.
        stack.rcon("dummy players ")
        wait_until(lambda: controller.get_snapshot()["playerCount"] == 0)
        stack.clock.advance(31)
        wait_until(lambda: controller.get_snapshot()["autoStopAt"] is not None)
        stack.clock.advance(stack.settings.autoStopDelay + 1)
        wait_until(lambda: controller.state == STOPPED, timeout=30)
        assert stack.process_manager.tracked_identity is None

        # Stopped: bans are edited offline (Steve's UUID is known).
        assert controller.ban_player("Steve").success is True
        assert stack.bans.is_banned("Steve")


def _kill_launched(process_manager, pids):
    """Kill only the dummy servers this test launched (if still alive)."""
    import psutil

    tracked = process_manager.tracked_identity
    if tracked is not None and tracked.pid not in pids:
        pids.append(tracked.pid)
    for pid in pids:
        try:
            process = psutil.Process(pid)
            for member in [process, *process.children(recursive=True)]:
                member.kill()
        except psutil.Error:
            continue


def _queued(observations, item) -> bool:
    with observations.mutex:
        return item in observations.queue
