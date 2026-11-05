"""Unit tests for the web layer: committed-snapshot cache, SSE ordering,
input helpers, info cache and the run()/shutdown() contract."""

import json
import queue
import threading
from unittest.mock import MagicMock

import pytest

from flask_login import login_user

from src.auth import User
from src.events import Event, EventBus
from src.lifecycle.messages import ActionResult
from src.minecraft import server_files
from src.web_server import (
    COMMITTED_EVENTS,
    MAX_COMMAND_LENGTH,
    WebServer,
    _put_latest,
    clean_command,
    clean_reason,
    status_label,
    strip_formatting,
)


def make_snapshot(**overrides):
    """A committed snapshot with every documented field."""
    snapshot = {
        "revision": 0,
        "state": "STOPPED",
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
    snapshot.update(overrides)
    return snapshot


def make_server(snapshot=None):
    bus = EventBus()
    controller = MagicMock()
    controller.get_snapshot.return_value = snapshot or make_snapshot()
    server = WebServer(controller, bus=bus)
    server.app.config["WTF_CSRF_ENABLED"] = False
    return server, controller, bus


def open_stream(server):
    """_handle_stream() as the logged-in admin (the route is login_required)."""
    from src.settings import settings

    with server.app.test_request_context():
        login_user(User(str(settings.webUsername), server._credential_epoch()))
        return server._handle_stream()


def decode(message):
    """Payload of one SSE `data:` message (ignores a leading retry: line)."""
    data_line = next(line for line in message.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


def attach_client(server):
    client = server._streams.add()
    assert client is not None
    return client


PLAYER = {
    "name": "Steve",
    "uuid": "8667ba71-b85a-4004-af54-457a9734eed7",
    "currently_online": True,
    "last_online": 1762300000.0,
    "first_seen": 1762200000.0,
}
BAN = {
    "name": "Griefer",
    "uuid": "11111111-2222-4333-8444-555555555555",
    "reason": "Banned by an operator.",
    "source": "Server",
    "created": "2026-11-05 10:00:00 +0000",
    "expires": "forever",
}


class TestCommittedSnapshots:
    def test_initial_cache_is_one_controller_snapshot(self, mock_settings):
        snapshot = make_snapshot(
            state="RUNNING", ready=True, players=[PLAYER], banned_players=[BAN]
        )
        server, controller, _ = make_server(snapshot)

        assert controller.get_snapshot.call_count == 1
        assert server.snapshot == snapshot

    def test_subscribes_to_committed_events_only(self, mock_settings):
        _, _, bus = make_server()
        for event in COMMITTED_EVENTS:
            assert bus.subscriber_count(event) == 1
        assert bus.subscriber_count(Event.PLAYER_JOINED) == 0
        assert bus.subscriber_count() == 6

    def test_committed_event_refreshes_and_broadcasts_the_same_snapshot(
        self, mock_settings
    ):
        server, controller, bus = make_server()
        client = attach_client(server)
        updated = make_snapshot(
            revision=1,
            state="RUNNING",
            ready=True,
            playerData="ok",
            playerCount=1,
            players=[PLAYER],
        )
        controller.get_snapshot.return_value = updated
        controller.get_snapshot.reset_mock()

        bus.publish(Event.SERVER_STATUS, {"state": "RUNNING", "generation": 3})

        assert controller.get_snapshot.call_count == 1
        assert server.snapshot == updated
        payload = decode(client.get_nowait())
        assert payload["data"] == updated
        assert payload["autoStopDelay"] == round(mock_settings.autoStopDelay)
        assert isinstance(payload["serverTime"], float)

    @pytest.mark.parametrize(
        "event", [Event.BAN_ADDED, Event.BAN_REMOVED, Event.LIFECYCLE_CHANGED]
    )
    def test_other_committed_events_refresh(self, mock_settings, event):
        server, controller, bus = make_server()
        controller.get_snapshot.return_value = make_snapshot(
            state="STARTING", banned_players=[BAN]
        )

        bus.publish(event, {})

        assert server.snapshot["state"] == "STARTING"
        assert server.snapshot["banned_players"] == [BAN]

    def test_stale_revision_is_not_cached_or_broadcast(self, mock_settings):
        server, controller, _ = make_server(make_snapshot(revision=3, state="RUNNING"))
        client = attach_client(server)
        controller.get_snapshot.return_value = make_snapshot(
            revision=2, state="STOPPED"
        )

        server._on_committed({})

        assert client.empty()
        assert server.snapshot["revision"] == 3
        assert server.snapshot["state"] == "RUNNING"

    def test_equal_revision_ban_refresh_is_applied(self, mock_settings):
        server, controller, bus = make_server(make_snapshot(revision=3))
        client = attach_client(server)
        controller.get_snapshot.return_value = make_snapshot(
            revision=3, banned_players=[BAN]
        )

        bus.publish(Event.BAN_ADDED, {"name": "Griefer"})

        assert server.snapshot["banned_players"] == [BAN]
        assert decode(client.get_nowait())["data"]["banned_players"] == [BAN]

    def test_json_state_reads_exactly_one_snapshot(self, mock_settings):
        snapshot = make_snapshot(
            revision=2, state="STOPPING", playerData="unavailable", players=[PLAYER]
        )
        server, controller, _ = make_server(snapshot)
        controller.get_snapshot.reset_mock()

        with server.app.test_request_context():
            response, status = server._json_state(success=True)

        assert status == 200
        assert controller.get_snapshot.call_count == 1
        body = response.get_json()
        assert body["data"] == snapshot
        assert body["success"] is True

    def test_close_unsubscribes_idempotently(self, mock_settings):
        server, _, bus = make_server()
        server.close()
        assert bus.subscriber_count() == 0
        server.close()
        assert bus.subscriber_count() == 0


class TestServerSentEvents:
    def test_initial_payload_is_the_committed_snapshot(self, mock_settings):
        snapshot = make_snapshot(
            revision=3, state="RUNNING", playerData="unavailable", players=[PLAYER]
        )
        server, controller, _ = make_server(snapshot)
        controller.get_snapshot.reset_mock()

        response = open_stream(server)
        first = next(response.response)
        response.response.close()

        assert first.startswith("retry: ")
        assert controller.get_snapshot.call_count == 1
        assert decode(first)["data"] == snapshot
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.mimetype == "text/event-stream"

    def test_initial_snapshot_precedes_queued_updates(self, mock_settings):
        server, controller, bus = make_server(make_snapshot(revision=1))
        response = open_stream(server)

        bus.publish(Event.BAN_ADDED, {"name": "x"})  # same revision -> queued
        controller.get_snapshot.return_value = make_snapshot(
            revision=2, state="RUNNING"
        )
        bus.publish(Event.SERVER_STARTED, {"pid": 42})

        revisions = [
            decode(next(response.response))["data"]["revision"] for _ in range(3)
        ]
        response.response.close()
        assert revisions == [1, 1, 2]

    def test_concurrent_publishers_never_reorder_revisions(self, mock_settings):
        server, controller, bus = make_server(make_snapshot(revision=0))
        client = attach_client(server)
        counter = {"value": 0}
        lock = threading.Lock()

        def next_snapshot():
            with lock:
                counter["value"] += 1
                revision = counter["value"]
            return make_snapshot(revision=revision)

        controller.get_snapshot.side_effect = next_snapshot
        threads = [
            threading.Thread(
                target=lambda: [bus.publish(Event.SERVER_STATUS, {}) for _ in range(25)]
            )
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        received = []
        while not client.empty():
            received.append(decode(client.get_nowait())["data"]["revision"])
        assert received == sorted(received)
        assert received[-1] == counter["value"]

    def test_keepalive_comment_when_idle(self, mock_settings):
        server, _, _ = make_server()
        server.sse_keepalive_interval = 0.01
        response = open_stream(server)
        next(response.response)
        assert next(response.response) == ": keepalive\n\n"
        response.response.close()
        assert len(server._streams) == 0

    def test_close_ends_open_streams_and_refuses_new_ones(self, mock_settings):
        server, _, _ = make_server()
        response = open_stream(server)
        next(response.response)

        server.close()

        with pytest.raises(StopIteration):
            next(response.response)
        _, status = open_stream(server)
        assert status == 503

    def test_client_limit(self, mock_settings, monkeypatch):
        server, _, _ = make_server()
        monkeypatch.setattr(server._streams, "_max_clients", 1)
        first = open_stream(server)
        _, status = open_stream(server)
        assert status == 503
        first.response.close()

    def test_full_queue_keeps_the_newest_messages_in_order(self):
        client = queue.Queue(maxsize=2)
        for message in ("r1", "r2", "r3"):
            _put_latest(client, message)
        assert [client.get_nowait(), client.get_nowait()] == ["r2", "r3"]


class TestStreamLifetime:
    def test_stream_ends_when_the_session_would_expire(self, mock_settings):
        mock_settings.sessionTimeout = 0.05
        server, _, _ = make_server()
        server.sse_keepalive_interval = 0.01
        response = open_stream(server)
        next(response.response)
        rest = list(response.response)  # terminates on its own
        assert all(chunk == ": keepalive\n\n" for chunk in rest)
        assert len(server._streams) == 0

    def test_stream_ends_when_credentials_change(self, mock_settings):
        server, _, _ = make_server()
        server.sse_keepalive_interval = 0.01
        response = open_stream(server)
        next(response.response)
        assert next(response.response) == ": keepalive\n\n"
        mock_settings.webPassword = "rotated-password"
        assert list(response.response) == []

    def test_no_snapshot_is_sent_after_the_grant_lapses(self, mock_settings):
        server, controller, bus = make_server()
        response = open_stream(server)
        next(response.response)
        mock_settings.webPassword = "rotated-password"
        controller.get_snapshot.return_value = make_snapshot(
            revision=9, state="RUNNING"
        )
        bus.publish(Event.SERVER_STARTED, {})
        assert list(response.response) == []

    def test_close_owner_ends_only_that_sessions_streams(self, mock_settings):
        server, _, _ = make_server()
        mine = server._streams.add("sid-a")
        other = server._streams.add("sid-b")
        assert server._streams.close_owner("sid-a") == 1
        assert mine.get_nowait() is None
        assert other.empty()

    def test_load_user_requires_the_current_credential_epoch(self, mock_settings):
        server, _, _ = make_server()
        epoch = server._credential_epoch()
        assert server._load_user(f"test_admin|{epoch}").username == "test_admin"
        assert server._load_user("test_admin") is None  # pre-epoch session
        assert server._load_user("test_admin|" + "0" * 32) is None
        assert server._load_user(f"intruder|{epoch}") is None
        mock_settings.webPassword = "changed"
        assert server._load_user(f"test_admin|{epoch}") is None


class TestHelpers:
    def test_restart_frame_reads_as_starting(self):
        assert status_label({"state": "STOPPED", "restartPending": True}) == "STARTING"
        assert status_label({"state": "STOPPING", "restartPending": True}) == "STOPPING"
        assert status_label({"state": "STOPPED", "restartPending": False}) == "OFFLINE"

    @pytest.mark.parametrize(
        "state,ready,label",
        [
            ("STOPPED", False, "OFFLINE"),
            ("STARTING", False, "STARTING"),
            ("RUNNING", False, "LOADING"),
            ("RUNNING", True, "ONLINE"),
            ("STOPPING", True, "STOPPING"),
            (None, False, "OFFLINE"),
        ],
    )
    def test_status_label(self, state, ready, label):
        assert status_label({"state": state, "ready": ready}) == label

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("list", "list"),
            ("  /say hi  ", "say hi"),
            ("//wand", "/wand"),
            ("x" * MAX_COMMAND_LENGTH, "x" * MAX_COMMAND_LENGTH),
        ],
    )
    def test_clean_command_accepts(self, raw, expected):
        assert clean_command(raw) == (expected, None)

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "/",
            "say a\nop me",
            "say a\rstop",
            "a\x00b",
            "x" * (MAX_COMMAND_LENGTH + 1),
        ],
    )
    def test_clean_command_rejects(self, raw):
        command, error = clean_command(raw)
        assert command is None
        assert error

    def test_clean_reason(self):
        assert clean_reason(None) == (None, None)
        assert clean_reason("   ") == (None, None)
        assert clean_reason(" griefing ") == ("griefing", None)
        assert clean_reason("a\nb")[1]
        assert clean_reason("x" * 257)[1]

    def test_strip_formatting(self):
        assert strip_formatting("§aHello §lWorld§r!") == "Hello World!"
        assert strip_formatting(None) == ""


class TestInfo:
    def test_info_is_cached(self, mock_settings, monkeypatch):
        list_mods = MagicMock(return_value=["a.jar", "b.jar"])
        monkeypatch.setattr(server_files, "list_mods", list_mods)
        monkeypatch.setattr(
            server_files, "read_server_icon", lambda _d: "data:image/png;base64,AAAA"
        )
        server, _, _ = make_server()

        first = server._get_info()
        second = server._get_info()

        assert first is second
        assert list_mods.call_count == 1
        assert first["mods"] == ["a.jar", "b.jar"]
        assert first["modCount"] == 2
        assert first["icon"] == "data:image/png;base64,AAAA"

    def test_cache_expires(self, mock_settings, monkeypatch):
        list_mods = MagicMock(return_value=[])
        monkeypatch.setattr(server_files, "list_mods", list_mods)
        monkeypatch.setattr(server_files, "read_server_icon", lambda _d: None)
        server, _, _ = make_server()
        server.info_cache_seconds = 0

        server._get_info()
        server._get_info()

        assert list_mods.call_count == 2

    def test_bad_icon_and_failing_helpers_degrade_gracefully(
        self, mock_settings, monkeypatch
    ):
        monkeypatch.setattr(
            server_files, "list_mods", MagicMock(side_effect=OSError("boom"))
        )
        monkeypatch.setattr(
            server_files, "read_server_icon", lambda _d: "javascript:alert(1)"
        )
        server, _, _ = make_server()

        info = server._get_info()

        assert info["mods"] == []
        assert info["icon"] is None
        assert "serverAddress" not in info

    def test_server_address_only_when_enabled(self, mock_settings, monkeypatch):
        monkeypatch.setattr(server_files, "list_mods", lambda _d: [])
        monkeypatch.setattr(server_files, "read_server_icon", lambda _d: None)
        mock_settings.showServerIPAddress = True
        mock_settings.listenerBindHost = "play.example.net"
        mock_settings.serverPort = 25599
        server, _, _ = make_server()

        assert server._get_info()["serverAddress"] == "play.example.net:25599"


class TestRunAndShutdown:
    def test_shutdown_from_the_serving_thread_does_not_deadlock(
        self, mock_settings, monkeypatch
    ):
        """main may call shutdown() from a signal handler, which runs on the
        thread blocked in run(); emulate that via service_actions()."""
        import src.web_server as web_module
        from test.support import free_tcp_port

        mock_settings.webServerPort = free_tcp_port()
        server, _, _ = make_server()
        real_make_server = web_module.make_server

        def make_server_calling_shutdown(*args, **kwargs):
            wsgi = real_make_server(*args, **kwargs)
            original = wsgi.service_actions

            def service_actions():
                original()
                server.shutdown()

            wsgi.service_actions = service_actions
            return wsgi

        monkeypatch.setattr(web_module, "make_server", make_server_calling_shutdown)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_shutdown_before_run_makes_run_return_immediately(self, mock_settings):
        from test.support import free_tcp_port

        mock_settings.webServerPort = free_tcp_port()
        server, _, _ = make_server()
        server.shutdown()
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not server.serving.is_set()


class TestControllerFailures:
    def test_action_result_coercion(self, mock_settings):
        server, controller, _ = make_server()
        controller.kick_player.return_value = None
        assert server._call_action("kick_player", "x").success is False
        controller.kick_player.return_value = ActionResult(True, "ok")
        assert server._call_action("kick_player", "x").message == "ok"
        controller.kick_player.side_effect = RuntimeError("boom")
        assert server._call_action("kick_player", "x").success is False
