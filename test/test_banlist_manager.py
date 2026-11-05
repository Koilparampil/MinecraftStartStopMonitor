"""BanlistManager: vanilla banned-players.json reading (mtime cache), refresh
diffs -> BAN_ADDED/BAN_REMOVED, UUID lookup and atomic offline edits."""

import json
import os
import re
import threading

import pytest

from src.banlist_manager import BanlistManager, ban_is_active
from src.events import Event, EventBus
from src.minecraft import server_files
from src.minecraft.server_files import offline_uuid

GRIEFER = {
    "uuid": "0b4f4c21-1111-2222-3333-444455556666",
    "name": "Griefer",
    "created": "2026-11-05 10:00:00 +0000",
    "source": "Server",
    "expires": "forever",
    "reason": "Griefing",
}
STEVE_UUID = "8667ba71-b85a-4004-af54-457a9734eed7"


class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.events = []

    def publish(self, event_type, data):
        self.events.append((event_type, data))
        super().publish(event_type, data)


def write(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def bans(server_dir):
    return server_dir / "banned-players.json"


@pytest.fixture
def bus():
    return RecordingBus()


class TestReading:
    def test_missing_file_is_empty(self, bus, server_dir):
        assert BanlistManager(bus).get_banned_players() == []

    def test_vanilla_entries_are_normalized(self, bus, bans):
        write(bans, [GRIEFER, {"name": "Bare"}])
        players = BanlistManager(bus).get_banned_players()
        assert players[0] == {
            "name": "Griefer",
            "uuid": GRIEFER["uuid"],
            "reason": "Griefing",
            "source": "Server",
            "created": "2026-11-05 10:00:00 +0000",
            "expires": "forever",
        }
        assert players[1]["reason"] == "Banned by an operator."
        assert players[1]["expires"] == "forever"

    def test_invalid_file_reads_as_empty(self, bus, bans):
        bans.write_text("{oops", encoding="utf-8")
        assert BanlistManager(bus).get_banned_players() == []

    def test_explicit_server_dir_wins_over_settings(self, bus, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        write(other / "banned-players.json", [GRIEFER])
        assert BanlistManager(bus, server_dir=str(other)).is_banned("griefer")

    def test_cache_picks_up_external_changes(self, bus, bans):
        manager = BanlistManager(bus)
        assert manager.get_banned_players() == []
        write(bans, [GRIEFER])
        os.utime(bans, ns=(1, 1_000_000_000))  # force a different mtime
        assert [p["name"] for p in manager.get_banned_players()] == ["Griefer"]

    def test_cached_reads_do_not_reparse(self, bus, bans, monkeypatch):
        write(bans, [GRIEFER])
        manager = BanlistManager(bus)
        calls = []
        original = server_files.read_json_list
        monkeypatch.setattr(
            server_files,
            "read_json_list",
            lambda path: calls.append(path) or original(path),
        )
        for _ in range(3):
            manager.get_banned_players()
        assert calls == []

    def test_is_banned_is_case_insensitive_and_ignores_expired(self, bus, bans):
        write(bans, [GRIEFER, {"name": "Temp", "expires": "2001-01-01 00:00:00 +0000"}])
        manager = BanlistManager(bus)
        assert manager.is_banned("GRIEFER")
        assert not manager.is_banned("Temp")
        assert not manager.is_banned("Steve")
        assert not manager.is_banned("")

    @pytest.mark.parametrize(
        ("expires", "active"),
        [
            ("forever", True),
            ("2999-01-01 00:00:00 +0000", True),
            ("garbage", True),
            ("2001-01-01 00:00:00 +0000", False),
        ],
    )
    def test_ban_is_active(self, expires, active):
        assert ban_is_active({"expires": expires}) is active


class TestRefresh:
    def test_initial_content_publishes_nothing(self, bus, bans):
        write(bans, [GRIEFER])
        manager = BanlistManager(bus)
        manager.refresh()
        assert bus.events == []

    def test_diffs_publish_added_and_removed(self, bus, bans):
        write(bans, [GRIEFER])
        manager = BanlistManager(bus)
        write(bans, [{"uuid": STEVE_UUID, "name": "Steve"}])
        manager.refresh()
        assert bus.events == [
            (Event.BAN_ADDED, {"name": "Steve", "uuid": STEVE_UUID}),
            (Event.BAN_REMOVED, {"name": "Griefer", "uuid": GRIEFER["uuid"]}),
        ]

    def test_refresh_rereads_even_when_mtime_is_unchanged(self, bus, bans):
        write(bans, [GRIEFER])
        manager = BanlistManager(bus)
        stat = bans.stat()
        write(bans, [GRIEFER | {"name": "Grieferr"}])
        os.utime(bans, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        manager.refresh()
        assert manager.is_banned("Grieferr")

    def test_events_published_outside_the_lock(self, bus, bans):
        manager = BanlistManager(bus)
        seen = []

        def subscriber(_data):
            done = threading.Event()
            threading.Thread(
                target=lambda: (manager.get_banned_players(), done.set())
            ).start()
            seen.append(done.wait(2))

        bus.subscribe(Event.BAN_ADDED, subscriber)
        write(bans, [GRIEFER])
        manager.refresh()
        assert seen == [True]


class TestLookupUuid:
    def test_usercache_then_banlist(self, bus, server_dir, bans):
        write(
            server_dir / "usercache.json",
            [{"name": "Steve", "uuid": STEVE_UUID.upper()}],
        )
        write(bans, [GRIEFER])
        manager = BanlistManager(bus)
        assert manager.lookup_uuid("steve") == STEVE_UUID
        assert manager.lookup_uuid("griefer") == GRIEFER["uuid"]
        assert manager.lookup_uuid("nobody") is None


class TestOfflineEdits:
    def test_ban_writes_a_vanilla_entry_and_publishes(self, bus, bans):
        manager = BanlistManager(bus)
        result = manager.add_ban_offline("Steve", uuid=STEVE_UUID, reason="Spam")
        assert result.success, result.message
        entry = read(bans)[0]
        assert list(entry) == ["uuid", "name", "created", "source", "expires", "reason"]
        assert (entry["uuid"], entry["name"], entry["expires"], entry["reason"]) == (
            STEVE_UUID,
            "Steve",
            "forever",
            "Spam",
        )
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}", entry["created"]
        )
        assert bus.events == [(Event.BAN_ADDED, {"name": "Steve", "uuid": STEVE_UUID})]
        assert manager.is_banned("steve")

    def test_ban_keeps_other_entries_untouched(self, bus, bans):
        write(bans, [GRIEFER])
        BanlistManager(bus).add_ban_offline("Steve", uuid=STEVE_UUID)
        assert read(bans)[0] == GRIEFER
        assert len(read(bans)) == 2

    def test_uuid_from_usercache_when_not_given(self, bus, server_dir, bans):
        write(server_dir / "usercache.json", [{"name": "Steve", "uuid": STEVE_UUID}])
        assert BanlistManager(bus).add_ban_offline("steve").success
        assert read(bans)[0]["uuid"] == STEVE_UUID

    def test_offline_uuid_only_in_offline_mode(self, bus, server_dir, bans):
        manager = BanlistManager(bus)
        result = manager.add_ban_offline("Stranger")
        assert not result.success
        assert "start the server to ban this player" in result.message
        assert not bans.exists()

        (server_dir / "server.properties").write_text(
            "online-mode=false\n", encoding="utf-8"
        )
        assert manager.add_ban_offline("Stranger").success
        assert read(bans)[0]["uuid"] == offline_uuid("Stranger")

    def test_already_banned_is_success_without_rewrite(self, bus, bans):
        write(bans, [GRIEFER])
        before = bans.read_text(encoding="utf-8")
        result = BanlistManager(bus).add_ban_offline("griefer", uuid=GRIEFER["uuid"])
        assert result.success
        assert "already banned" in result.message
        assert bans.read_text(encoding="utf-8") == before

    def test_unban_removes_only_that_player(self, bus, bans):
        write(bans, [GRIEFER, {"uuid": STEVE_UUID, "name": "Steve"}])
        manager = BanlistManager(bus)
        result = manager.remove_ban_offline("GRIEFER")
        assert result.success
        assert [e["name"] for e in read(bans)] == ["Steve"]
        assert bus.events == [
            (Event.BAN_REMOVED, {"name": "Griefer", "uuid": GRIEFER["uuid"]})
        ]

    def test_unban_of_unknown_player_is_success_with_message(self, bus, bans):
        write(bans, [GRIEFER])
        result = BanlistManager(bus).remove_ban_offline("Steve")
        assert result.success
        assert "not banned" in result.message
        assert bus.events == []

    @pytest.mark.parametrize("action", ["add_ban_offline", "remove_ban_offline"])
    def test_unreadable_file_is_never_overwritten(self, bus, bans, action):
        bans.write_text('{"not": "a list"}', encoding="utf-8")
        result = getattr(BanlistManager(bus), action)("Steve")
        assert not result.success
        assert bans.read_text(encoding="utf-8") == '{"not": "a list"}'

    @pytest.mark.parametrize("name", ["", "bad name", "x" * 33, "semi;colon"])
    def test_invalid_names_are_rejected(self, bus, bans, name):
        manager = BanlistManager(bus)
        assert not manager.add_ban_offline(name, uuid=STEVE_UUID).success
        assert not manager.remove_ban_offline(name).success
        assert not bans.exists()

    def test_write_failure_reports_and_keeps_the_file(self, bus, bans, monkeypatch):
        write(bans, [GRIEFER])

        def fail(*_args):
            raise OSError("read-only file system")

        monkeypatch.setattr("src.minecraft.server_files.write_json_list", fail)
        result = BanlistManager(bus).add_ban_offline("Steve", uuid=STEVE_UUID)
        assert not result.success
        assert "read-only" in result.message
        assert read(bans) == [GRIEFER]

    def test_without_server_dir_fails_cleanly(self, bus, mock_settings):
        mock_settings.serverDir = None
        manager = BanlistManager(bus)
        assert manager.get_banned_players() == []
        assert not manager.add_ban_offline("Steve", uuid=STEVE_UUID).success
