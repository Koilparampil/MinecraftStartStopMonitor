"""PlayerManager: name-keyed records, join/leave events, partial rosters,
persistence (save on change, throttled timestamp-only saves)."""

import json

import pytest

from src.events import Event, EventBus
from src.lifecycle.messages import OnlinePlayer, Roster
from src.player_manager import SAVE_INTERVAL, PlayerManager

STEVE = OnlinePlayer("Steve", "8667ba71-b85a-4004-af54-457a9734eed7")
ALEX = OnlinePlayer("Alex", "ec561538-f3fd-461d-aff5-086b22154bce")


def roster(*players, online=None):
    return Roster(
        online=len(players) if online is None else online, players=tuple(players)
    )


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.events = []

    def publish(self, event_type, data):
        self.events.append((event_type, data))
        super().publish(event_type, data)


@pytest.fixture
def env(tmp_path, mock_settings):
    class Env:
        bus = RecordingBus()
        wall = Clock()
        mono = Clock(50.0)
        path = tmp_path / "data" / "players.json"

        def manager(self):
            return PlayerManager(
                self.bus, str(self.path), clock=self.wall, monotonic=self.mono
            )

        def saved(self):
            return json.loads(self.path.read_text(encoding="utf-8"))

    return Env()


class TestRoster:
    def test_new_players_are_online_and_published(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE, ALEX))
        assert manager.get_player_count() == 2
        steve = next(p for p in manager.get_all_players() if p["name"] == "Steve")
        assert steve == {
            "name": "Steve",
            "uuid": STEVE.uuid,
            "currently_online": True,
            "last_online": 1_000.0,
            "first_seen": 1_000.0,
        }
        assert (
            Event.PLAYER_JOINED,
            {"name": "Steve", "uuid": STEVE.uuid},
        ) in env.bus.events

    def test_missing_players_go_offline_once(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE, ALEX))
        env.wall.now = 1_010.0
        manager.update_from_roster(roster(ALEX))
        manager.update_from_roster(roster(ALEX))
        left = [e for e in env.bus.events if e[0] == Event.PLAYER_LEFT]
        assert left == [(Event.PLAYER_LEFT, {"name": "Steve", "uuid": STEVE.uuid})]
        assert [p["name"] for p in manager.get_online_players()] == ["Alex"]
        assert [p["name"] for p in manager.get_offline_players()] == ["Steve"]

    def test_rejoin_publishes_joined_again_and_keeps_first_seen(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE))
        manager.update_from_roster(roster())
        env.wall.now = 2_000.0
        manager.update_from_roster(roster(STEVE))
        joined = [e for e in env.bus.events if e[0] == Event.PLAYER_JOINED]
        assert len(joined) == 2
        record = manager.get_all_players()[0]
        assert (record["first_seen"], record["last_online"]) == (1_000.0, 2_000.0)

    def test_names_are_case_insensitive_keys_and_latest_casing_wins(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(OnlinePlayer("steve")))
        manager.update_from_roster(roster(STEVE))
        players = manager.get_all_players()
        assert len(players) == 1
        assert players[0]["name"] == "Steve"
        assert players[0]["uuid"] == STEVE.uuid
        assert manager.find_uuid("STEVE") == STEVE.uuid
        assert manager.find_uuid("nobody") is None

    def test_known_uuid_is_kept_when_a_sample_has_none(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE))
        manager.update_from_roster(roster(OnlinePlayer("Steve")))
        assert manager.find_uuid("steve") == STEVE.uuid

    def test_renamed_player_moves_to_the_new_name(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE))
        manager.update_from_roster(roster())
        manager.update_from_roster(roster(OnlinePlayer("Steve2", STEVE.uuid)))
        players = manager.get_all_players()
        assert [p["name"] for p in players] == ["Steve2"]
        assert players[0]["first_seen"] == 1_000.0

    def test_partial_roster_only_adds(self, env):
        """Ping samples are capped and can hide names: unlisted players stay online."""
        manager = env.manager()
        manager.update_from_roster(roster(STEVE, ALEX))
        manager.update_from_roster(roster(ALEX, online=2))
        assert manager.get_player_count() == 2
        assert not [e for e in env.bus.events if e[0] == Event.PLAYER_LEFT]
        manager.update_from_roster(roster(online=0))
        assert manager.get_player_count() == 0

    def test_mark_all_offline_keeps_records_and_is_idempotent(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE, ALEX))
        env.wall.now = 1_500.0
        manager.mark_all_offline()
        manager.mark_all_offline()
        assert manager.get_player_count() == 0
        assert manager.get_total_player_count() == 2
        assert all(p["last_online"] == 1_500.0 for p in manager.get_all_players())
        assert len([e for e in env.bus.events if e[0] == Event.PLAYER_LEFT]) == 2

    def test_events_are_published_outside_the_lock(self, env):
        manager = env.manager()
        seen = []
        env.bus.subscribe(
            Event.PLAYER_JOINED, lambda _: seen.append(manager.get_player_count())
        )
        manager.update_from_roster(roster(STEVE))
        assert seen == [1]

    def test_all_players_sorted_online_first_then_recent(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE))
        env.wall.now = 1_100.0
        manager.update_from_roster(roster(ALEX))
        env.wall.now = 1_200.0
        manager.update_from_roster(roster(OnlinePlayer("Notch")))
        assert [p["name"] for p in manager.get_all_players()] == [
            "Notch",
            "Alex",
            "Steve",
        ]


class TestPersistence:
    def test_saved_in_versioned_format_and_reloaded(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE))
        data = env.saved()
        assert data["version"] == 1
        assert data["players"]["steve"]["uuid"] == STEVE.uuid
        reloaded = env.manager()
        assert reloaded.find_uuid("Steve") == STEVE.uuid

    def test_default_location_is_the_data_dir(self, tmp_path, mock_settings):
        manager = PlayerManager(EventBus())
        manager.update_from_roster(roster(STEVE))
        assert (tmp_path / "data" / "players.json").exists()

    def test_unchanged_polls_do_not_rewrite_the_file(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE))
        env.path.write_text("sentinel", encoding="utf-8")  # detects any rewrite
        for _ in range(5):  # 25 s of polls, inside SAVE_INTERVAL
            env.wall.now += 5
            env.mono.now += 5
            manager.update_from_roster(roster(STEVE))
        assert env.path.read_text(encoding="utf-8") == "sentinel"

    def test_timestamp_only_changes_are_flushed_after_the_interval(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE))
        env.wall.now, env.mono.now = 1_030.0, env.mono.now + 30
        manager.update_from_roster(roster(STEVE))
        assert env.saved()["players"]["steve"]["last_online"] == 1_000.0
        env.wall.now, env.mono.now = 1_061.0, env.mono.now + SAVE_INTERVAL
        manager.update_from_roster(roster(STEVE))
        assert env.saved()["players"]["steve"]["last_online"] == 1_061.0

    def test_structural_changes_save_immediately(self, env):
        manager = env.manager()
        manager.update_from_roster(roster(STEVE))
        env.mono.now += 1
        manager.update_from_roster(roster(STEVE, ALEX))
        assert set(env.saved()["players"]) == {"steve", "alex"}

    def test_corrupt_file_starts_empty(self, env):
        env.path.parent.mkdir(parents=True, exist_ok=True)
        env.path.write_text("{not json", encoding="utf-8")
        manager = env.manager()
        assert manager.get_all_players() == []

    def test_malformed_records_are_skipped(self, env):
        env.path.parent.mkdir(parents=True, exist_ok=True)
        env.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "players": {
                        "steve": {
                            "name": "Steve",
                            "uuid": STEVE.uuid,
                            "last_online": 5,
                        },
                        "bad": {"uuid": "x"},
                        "worse": "nope",
                    },
                }
            ),
            encoding="utf-8",
        )
        players = env.manager().get_all_players()
        assert [(p["name"], p["first_seen"]) for p in players] == [("Steve", 5.0)]

    def test_save_failure_is_logged_not_raised(self, env, monkeypatch):
        manager = env.manager()

        def fail(*_args):
            raise OSError("disk full")

        monkeypatch.setattr("src.player_manager.write_json_atomic", fail)
        manager.update_from_roster(roster(STEVE))
        assert manager.get_player_count() == 1


class TestTrackingDisabled:
    def test_only_online_players_kept_and_nothing_written(self, env, mock_settings):
        mock_settings.enablePlayerTracking = False
        manager = env.manager()
        manager.update_from_roster(roster(STEVE, ALEX))
        assert manager.get_player_count() == 2
        manager.update_from_roster(roster(ALEX))
        assert [p["name"] for p in manager.get_all_players()] == ["Alex"]
        manager.mark_all_offline()
        assert manager.get_all_players() == []
        assert not env.path.exists()
