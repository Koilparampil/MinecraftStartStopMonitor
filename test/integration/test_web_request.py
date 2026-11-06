"""Request-level tests for the web dashboard.

Flask's test client drives the full pipeline (routing, Flask-Login, CSRF,
rate limits, templates, JSON); the last class runs the real threaded
werkzeug server on a free port. The controller is a MagicMock returning
documented snapshots and ActionResults.
"""

import json
import logging
import re
import socket
import threading
import time
import urllib.request
from unittest.mock import MagicMock

import pytest
import requests

from src.events import Event, EventBus
from src.lifecycle.messages import ActionResult
from src.minecraft import server_files
from src.web_server import ServerLimits, WebServer
from test.support import free_tcp_port, wait_until

REAL_LIST_MODS = server_files.list_mods
REAL_READ_SERVER_ICON = server_files.read_server_icon

USERNAME = "test_admin"  # conftest TEST_SETTINGS
PASSWORD = "test_web_password"


def make_snapshot(**overrides):
    snapshot = {
        "revision": 1,
        "state": "STOPPED",
        "ready": False,
        "playerData": "unknown",
        "playerCount": 0,
        "maxPlayers": 20,
        "players": [],
        "banned_players": [],
        "server": {"version": "1.20.1", "motd": "A Modded Server", "source": None},
        "startedAt": None,
        "autoStopAt": None,
        "restartPending": False,
    }
    snapshot.update(overrides)
    return snapshot


def make_controller(snapshot=None):
    controller = MagicMock()
    controller.get_snapshot.return_value = snapshot or make_snapshot()
    controller.start_server.return_value = True
    controller.stop_server.return_value = True
    controller.restart_server.return_value = True
    controller.kick_player.return_value = ActionResult(True, "Kicked Steve")
    controller.ban_player.return_value = ActionResult(True, "Banned Steve")
    controller.unban_player.return_value = ActionResult(True, "Unbanned Steve")
    controller.run_console_command.return_value = ActionResult(
        True, "", output="There are 0 of a max of 20 players online:"
    )
    return controller


@pytest.fixture
def no_server_files(monkeypatch):
    monkeypatch.setattr(server_files, "list_mods", lambda _d: ["jei-1.20.1.jar"])
    monkeypatch.setattr(server_files, "read_server_icon", lambda _d: None)


@pytest.fixture
def build(mock_settings, no_server_files):
    """build(csrf=False, snapshot=None) -> WebServer (settings read at build time)."""
    servers = []

    def _build(csrf=False, snapshot=None):
        server = WebServer(make_controller(snapshot), bus=EventBus())
        server.app.config["WTF_CSRF_ENABLED"] = csrf
        servers.append(server)
        return server

    yield _build
    for server in servers:
        server.close()


@pytest.fixture
def web(build):
    return build()


@pytest.fixture
def client(web):
    return web.app.test_client()


@pytest.fixture
def auth_client(web):
    client = web.app.test_client()
    response = client.post("/login", data={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 302
    return client


def csrf_from(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(
        r'name="csrf-token" content="([^"]+)"', html
    )
    assert match, "no CSRF token in page"
    return match.group(1)


def login(client, csrf=False, **extra):
    data = {"username": USERNAME, "password": PASSWORD, **extra}
    if csrf:
        data["csrf_token"] = csrf_from(client.get("/login").get_data(as_text=True))
    return client.post("/login", data=data)


def sse_payload(chunk):
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    line = next(line for line in text.splitlines() if line.startswith("data: "))
    return json.loads(line.removeprefix("data: "))


# ----------------------------------------------------------------------
# Login / logout / session
# ----------------------------------------------------------------------


class TestLogin:
    def test_login_page_renders(self, client):
        response = client.get("/login")
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Admin login" in html
        assert 'name="csrf_token"' in html
        assert 'name="next" value="/"' in html

    def test_successful_login_redirects_and_creates_a_session(
        self, client, mock_settings
    ):
        response = login(client)
        assert response.status_code == 302
        assert response.headers["Location"] == "/"
        cookie = client.get_cookie(f"mcmonitor_session_{mock_settings.webServerPort}")
        assert cookie is not None and cookie.expires is not None  # permanent session
        assert client.get("/").status_code == 200

    def test_wrong_password_shows_error(self, client):
        response = client.post(
            "/login", data={"username": USERNAME, "password": "nope"}
        )
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Invalid username or password." in html
        assert f'value="{USERNAME}"' in html  # username kept, password not
        assert client.get("/").status_code == 302

    def test_warning_and_lockout(self, client):
        responses = [
            client.post("/login", data={"username": USERNAME, "password": "x"})
            for _ in range(5)
        ]
        assert (
            "attempt"
            not in responses[1]
            .get_data(as_text=True)
            .split("Invalid")[1]
            .split("</div>")[0]
        )
        assert "2 attempts left" in responses[2].get_data(as_text=True)
        assert "1 attempt left" in responses[3].get_data(as_text=True)
        locked = responses[4]
        assert locked.status_code == 429
        assert "Too many failed attempts" in locked.get_data(as_text=True)
        assert "disabled" in locked.get_data(as_text=True)
        # login.js counts down and re-enables the form when the lockout ends.
        assert re.search(r'data-lockout-seconds="\d+"', locked.get_data(as_text=True))
        assert "login.js" in locked.get_data(as_text=True)
        # Even the right password is refused while locked out.
        assert login(client).status_code == 429

    def test_remember_me_sets_cookie(self, client, mock_settings):
        response = login(client, remember="on")
        assert response.status_code == 302
        cookies = " ".join(response.headers.getlist("Set-Cookie"))
        assert f"mcmonitor_remember_{mock_settings.webServerPort}=" in cookies

    def test_logged_in_user_visiting_login_is_redirected(self, auth_client):
        response = auth_client.get("/login?next=/banned")
        assert response.status_code == 302
        assert response.headers["Location"] == "/banned"

    def test_logout(self, auth_client):
        response = auth_client.post("/logout")
        assert response.status_code == 302
        assert response.headers["Location"] == "/login"
        assert "You have been signed out." in auth_client.get("/login").get_data(
            as_text=True
        )
        assert auth_client.get("/").status_code == 302


class TestOpenRedirect:
    @pytest.mark.parametrize("target", ["/banned", "/info?x=1"])
    def test_relative_next_is_honoured(self, client, target):
        response = client.post(
            f"/login?next={target}", data={"username": USERNAME, "password": PASSWORD}
        )
        assert response.headers["Location"] == target

    @pytest.mark.parametrize(
        "target",
        [
            "https://evil.example",
            "//evil.example",
            "///evil.example",
            "/\\evil.example",
            "%2F%2Fevil.example",
            "/%09/evil.example",
            "javascript:alert(1)",
            "/login",
            "/logout",
        ],
    )
    def test_unsafe_next_falls_back_to_dashboard(self, client, target):
        response = client.post(
            f"/login?next={target}", data={"username": USERNAME, "password": PASSWORD}
        )
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_next_from_hidden_form_field(self, client):
        response = login(client, next="//evil.example")
        assert response.headers["Location"] == "/"
        response = client.post("/logout")
        response = login(client, next="/info")
        assert response.headers["Location"] == "/info"

    def test_login_page_never_echoes_an_unsafe_next(self, client):
        html = client.get("/login?next=https://evil.example").get_data(as_text=True)
        assert "evil.example" not in html


class TestCsrf:
    def test_login_without_token_is_rejected_and_reprompted(self, build):
        client = build(csrf=True).app.test_client()
        response = client.post(
            "/login", data={"username": USERNAME, "password": PASSWORD}
        )
        assert response.status_code == 400
        assert "Your login form expired" in response.get_data(as_text=True)
        assert client.get("/").status_code == 302

    def test_login_with_token_succeeds(self, build):
        client = build(csrf=True).app.test_client()
        assert login(client, csrf=True).status_code == 302
        assert client.get("/").status_code == 200

    def test_api_post_requires_token(self, build):
        web = build(csrf=True)
        client = web.app.test_client()
        login(client, csrf=True)
        token = csrf_from(client.get("/").get_data(as_text=True))

        missing = client.post("/action", data={"action": "getStatus"})
        assert missing.status_code == 400
        assert missing.get_json()["success"] is False

        by_header = client.post(
            "/action", data={"action": "getStatus"}, headers={"X-CSRFToken": token}
        )
        by_field = client.post(
            "/action", data={"action": "getStatus", "csrf_token": token}
        )
        assert by_header.status_code == 200
        assert by_field.status_code == 200

    def test_unauthenticated_post_gets_401_not_csrf_error(self, build):
        client = build(csrf=True).app.test_client()
        response = client.post("/action", data={"action": "startServer"})
        assert response.status_code == 401

    def test_logout_works_even_without_a_valid_token(self, build):
        """Logging out must always work (the Origin check still refuses
        cross-origin posts)."""
        client = build(csrf=True).app.test_client()
        login(client, csrf=True)
        assert client.post("/logout").status_code == 302
        assert client.get("/").status_code == 302


class TestUnauthenticated:
    def test_dashboard_redirects_to_login(self, client):
        response = client.get("/")
        assert response.status_code == 302
        assert response.headers["Location"] == "/login"

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/banned"),
            ("get", "/stream"),
            ("get", "/console"),
            ("get", "/info"),
            ("post", "/action"),
            ("post", "/kick"),
            ("post", "/ban"),
            ("post", "/unban"),
            ("post", "/command"),
        ],
    )
    def test_api_returns_401_json(self, client, method, path):
        response = getattr(client, method)(path)
        assert response.status_code == 401
        assert response.get_json()["success"] is False

    def test_healthz_is_public_and_minimal(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.get_json() == {"ok": True}


# ----------------------------------------------------------------------
# Dashboard page
# ----------------------------------------------------------------------


class TestDashboardPage:
    @pytest.mark.parametrize(
        "state,ready,label",
        [
            ("STOPPED", False, "OFFLINE"),
            ("STARTING", False, "STARTING"),
            ("RUNNING", False, "LOADING"),
            ("RUNNING", True, "ONLINE"),
            ("STOPPING", False, "STOPPING"),
        ],
    )
    def test_status_pill_is_rendered_server_side(
        self, auth_client, web, state, ready, label
    ):
        web.controller.get_snapshot.return_value = make_snapshot(
            state=state, ready=ready
        )
        html = auth_client.get("/").get_data(as_text=True)
        assert f'<span id="statusText">{label}</span>' in html
        assert f"pill--{label.lower()}" in html

    def test_server_strings_are_escaped(self, auth_client, web):
        evil = "<script>alert(1)</script>"
        web.controller.get_snapshot.return_value = make_snapshot(
            server={"version": evil, "motd": evil, "source": "rcon"},
            players=[
                {
                    "name": evil,
                    "uuid": None,
                    "currently_online": False,
                    "last_online": None,
                    "first_seen": None,
                }
            ],
        )
        html = auth_client.get("/").get_data(as_text=True)
        assert evil not in html
        assert "&lt;script&gt;" in html

    def test_bootstrap_carries_the_snapshot(self, auth_client, web):
        web.controller.get_snapshot.return_value = make_snapshot(revision=7)
        html = auth_client.get("/").get_data(as_text=True)
        raw = re.search(
            r'<script type="application/json" id="bootstrap">(.*?)</script>',
            html,
            re.DOTALL,
        ).group(1)
        boot = json.loads(raw)
        assert boot["payload"]["data"]["revision"] == 7
        assert boot["config"]["controlServer"] is True
        assert "test_web_password" not in raw

    def test_controls_follow_settings(self, build, mock_settings):
        mock_settings.controlServerThroughWeb = False
        web = build()
        client = web.app.test_client()
        login(client)
        html = client.get("/").get_data(as_text=True)
        assert 'id="startBtn"' not in html
        assert 'id="refreshBtn"' in html
        assert 'id="consolePanel"' in html
        assert 'id="commandForm"' not in html

    def test_console_panel_absent_when_disabled(self, build, mock_settings):
        mock_settings.webConsole = False
        client = build().app.test_client()
        login(client)
        assert 'id="consolePanel"' not in client.get("/").get_data(as_text=True)

    def test_start_button_state_matches_snapshot(self, auth_client, web):
        html = auth_client.get("/").get_data(as_text=True)
        start = re.search(r'<button[^>]*id="startBtn"[^>]*>', html).group(0)
        stop = re.search(r'<button[^>]*id="stopBtn"[^>]*>', html).group(0)
        assert "disabled" not in start
        assert "disabled" in stop

    @pytest.mark.parametrize(
        "cookie,attr", [("dark", 'data-theme="dark"'), ("light", 'data-theme="light"')]
    )
    def test_theme_cookie_is_applied_server_side(self, auth_client, cookie, attr):
        auth_client.set_cookie("theme", cookie)
        assert attr in auth_client.get("/").get_data(as_text=True)

    def test_bogus_theme_cookie_follows_the_system(self, auth_client):
        auth_client.set_cookie("theme", '"><script>')
        html = auth_client.get("/").get_data(as_text=True)
        assert "data-theme" not in html.split(">", 2)[1]

    def test_security_headers(self, auth_client, web):
        response = auth_client.get("/")
        csp = response.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "mc-heads.net" not in csp  # playerAvatars is off in TEST_SETTINGS
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"

    def test_avatar_origin_allowed_when_enabled(self, build, mock_settings):
        mock_settings.playerAvatars = True
        client = build().app.test_client()
        login(client)
        assert (
            "https://mc-heads.net" in client.get("/").headers["Content-Security-Policy"]
        )

    def test_static_assets_are_served(self, client):
        for name in ("styles.css", "dashboard.js", "theme.js", "favicon.svg"):
            response = client.get(f"/static/{name}")
            assert response.status_code == 200, name
            response.close()


# ----------------------------------------------------------------------
# Server control
# ----------------------------------------------------------------------


class TestActions:
    def test_get_status(self, auth_client, web):
        web.controller.get_snapshot.return_value = make_snapshot(
            state="RUNNING", ready=True, revision=4
        )
        body = auth_client.post("/action", data={"action": "getStatus"}).get_json()
        assert body["success"] is True
        assert body["data"]["revision"] == 4
        assert body["autoStopDelay"] == 120
        web.controller.start_server.assert_not_called()

    @pytest.mark.parametrize(
        "action,method",
        [
            ("startServer", "start_server"),
            ("stopServer", "stop_server"),
            ("restartServer", "restart_server"),
        ],
    )
    def test_control_actions_call_the_controller(
        self, auth_client, web, action, method
    ):
        response = auth_client.post("/action", data={"action": action})
        body = response.get_json()
        assert response.status_code == 200
        assert body["success"] is True
        assert body["message"]
        assert "data" in body
        getattr(web.controller, method).assert_called_once_with()

    def test_rejected_action_explains_the_state(self, auth_client, web):
        web.controller.start_server.return_value = False
        web.controller.get_snapshot.return_value = make_snapshot(
            state="RUNNING", ready=True
        )
        body = auth_client.post("/action", data={"action": "startServer"}).get_json()
        assert body["success"] is False
        assert "running" in body["message"]

    def test_controller_exception_is_a_failure_not_a_500(self, auth_client, web):
        web.controller.stop_server.side_effect = RuntimeError("boom")
        response = auth_client.post("/action", data={"action": "stopServer"})
        assert response.status_code == 200
        assert response.get_json()["success"] is False

    @pytest.mark.parametrize("action", ["", "deleteWorld", "startserver"])
    def test_unknown_action_is_400(self, auth_client, web, action):
        response = auth_client.post("/action", data={"action": action})
        assert response.status_code == 400
        web.controller.start_server.assert_not_called()

    def test_control_disabled_is_403_but_status_still_works(self, build, mock_settings):
        mock_settings.controlServerThroughWeb = False
        web = build()
        client = web.app.test_client()
        login(client)
        for action in ("startServer", "stopServer", "restartServer"):
            assert client.post("/action", data={"action": action}).status_code == 403
        assert client.post("/action", data={"action": "getStatus"}).status_code == 200
        web.controller.start_server.assert_not_called()
        web.controller.stop_server.assert_not_called()
        web.controller.restart_server.assert_not_called()


# ----------------------------------------------------------------------
# Moderation
# ----------------------------------------------------------------------


class TestModeration:
    @pytest.mark.parametrize(
        "name",
        ["Steve", "a", "x" * 32, "Bedrock.Player", "*geyser_user", "Mr-Steve_99"],
    )
    def test_valid_names_reach_the_controller(self, auth_client, web, name):
        assert auth_client.post("/kick", data={"player": name}).status_code == 200
        web.controller.kick_player.assert_called_once_with(name, None)

    @pytest.mark.parametrize("endpoint", ["/kick", "/ban", "/unban"])
    @pytest.mark.parametrize(
        "name",
        ["", "a b", "x" * 33, "steve;op me", "steve\n", "Stéve", "<b>", "../x", "@a"],
    )
    def test_invalid_names_are_rejected_before_the_controller(
        self, auth_client, web, endpoint, name
    ):
        response = auth_client.post(endpoint, data={"player": name})
        assert response.status_code == 400
        assert response.get_json()["success"] is False
        web.controller.kick_player.assert_not_called()
        web.controller.ban_player.assert_not_called()
        web.controller.unban_player.assert_not_called()

    def test_missing_player_field(self, auth_client):
        assert auth_client.post("/ban", data={}).status_code == 400

    def test_reason_is_passed_through_and_trimmed(self, auth_client, web):
        auth_client.post("/ban", data={"player": "Steve", "reason": "  griefing  "})
        web.controller.ban_player.assert_called_once_with("Steve", "griefing")

    @pytest.mark.parametrize("reason", ["line1\nline2", "x" * 257])
    def test_bad_reason_is_400(self, auth_client, web, reason):
        assert (
            auth_client.post(
                "/kick", data={"player": "Steve", "reason": reason}
            ).status_code
            == 400
        )
        web.controller.kick_player.assert_not_called()

    def test_unban_takes_no_reason(self, auth_client, web):
        auth_client.post("/unban", data={"player": "Steve", "reason": "sorry"})
        web.controller.unban_player.assert_called_once_with("Steve")

    def test_result_and_snapshot_are_returned(self, auth_client, web):
        ban = {
            "name": "Steve",
            "uuid": "u",
            "reason": "r",
            "source": "Server",
            "created": "c",
            "expires": "forever",
        }
        web.controller.get_snapshot.return_value = make_snapshot(
            revision=9, banned_players=[ban]
        )
        body = auth_client.post("/ban", data={"player": "Steve"}).get_json()
        assert body["success"] is True
        assert body["message"] == "Banned Steve"
        assert body["data"]["banned_players"] == [ban]
        assert auth_client.get("/banned").get_json() == {
            "success": True,
            "banned_players": [ban],
        }

    def test_failure_message_is_propagated(self, auth_client, web):
        web.controller.kick_player.return_value = ActionResult(
            False, "RCON is not available."
        )
        body = auth_client.post("/kick", data={"player": "Steve"}).get_json()
        assert body == {**body, "success": False, "message": "RCON is not available."}

    def test_empty_message_gets_a_default(self, auth_client, web):
        web.controller.unban_player.return_value = ActionResult(False, "")
        assert (
            auth_client.post("/unban", data={"player": "Steve"}).get_json()["message"]
            == "Could not unban Steve"
        )

    def test_moderation_is_logged_with_user(self, auth_client, caplog):
        with caplog.at_level("INFO"):
            auth_client.post("/kick", data={"player": "Steve"})
        assert any(
            "Kick Steve by test_admin" in record.getMessage()
            for record in caplog.records
        )


# ----------------------------------------------------------------------
# Console and commands
# ----------------------------------------------------------------------


class TestConsole:
    def write_log(self, mock_settings, lines):
        logs = mock_settings.serverDir + "/logs"
        import os

        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "latest.log"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_server_log_tail(self, auth_client, mock_settings):
        self.write_log(mock_settings, [f"[12:00:{i:02d}] line {i}" for i in range(10)])
        body = auth_client.get("/console?lines=3").get_json()
        assert body["success"] is True
        assert body["source"] == "log"
        assert body["exists"] is True
        assert body["lines"] == [
            "[12:00:07] line 7",
            "[12:00:08] line 8",
            "[12:00:09] line 9",
        ]

    def test_launch_output(self, auth_client, mock_settings):
        with open(
            mock_settings.data_path("server-console.log"), "w", encoding="utf-8"
        ) as handle:
            handle.write("\x1b[32mLaunching\x1b[0m\nok\n")
        body = auth_client.get("/console?source=launch").get_json()
        assert body["source"] == "launch"
        assert body["lines"] == ["Launching", "ok"]  # ANSI colours stripped

    def test_unreadable_log_is_a_json_error_logged_once(
        self, auth_client, mock_settings, monkeypatch, caplog
    ):
        self.write_log(mock_settings, ["x"])

        def denied(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", path)

        monkeypatch.setattr(server_files, "tail_file", denied)
        with caplog.at_level(logging.WARNING, logger="src.web_server"):
            responses = [auth_client.get("/console") for _ in range(3)]
        assert [r.status_code for r in responses] == [200, 200, 200]
        body = responses[0].get_json()
        assert body["success"] is False
        assert body["message"] == "Cannot read logs/latest.log: permission denied."
        assert sum("cannot read" in r.getMessage() for r in caplog.records) == 1

    def test_missing_log(self, auth_client):
        body = auth_client.get("/console").get_json()
        assert body == {"success": True, "source": "log", "lines": [], "exists": False}

    @pytest.mark.parametrize(
        "query", ["source=world", "source=../../etc/passwd", "lines=abc"]
    )
    def test_bad_parameters(self, auth_client, query):
        assert auth_client.get(f"/console?{query}").status_code == 400

    @pytest.mark.parametrize("lines,expected", [("0", 1), ("-5", 1), ("5000", 1000)])
    def test_line_count_is_clamped(
        self, auth_client, mock_settings, monkeypatch, lines, expected
    ):
        self.write_log(mock_settings, ["x"])
        seen = []
        monkeypatch.setattr(
            server_files,
            "tail_file",
            lambda path, count, *a, **k: seen.append(count) or [],
        )
        auth_client.get(f"/console?lines={lines}")
        assert seen == [expected]

    def test_disabled_console_is_403(self, build, mock_settings):
        mock_settings.webConsole = False
        client = build().app.test_client()
        login(client)
        assert client.get("/console").status_code == 403
        assert client.post("/command", data={"command": "list"}).status_code == 403


class TestCommands:
    def test_command_runs_with_leading_slash_stripped(self, auth_client, web):
        body = auth_client.post("/command", data={"command": "/list"}).get_json()
        web.controller.run_console_command.assert_called_once_with("list")
        assert body["success"] is True
        assert body["output"].startswith("There are 0")

    @pytest.mark.parametrize(
        "command", ["", "   ", "/", "say hi\nop me", "say\x00", "x" * 257]
    )
    def test_invalid_commands_are_400(self, auth_client, web, command):
        response = auth_client.post("/command", data={"command": command})
        assert response.status_code == 400
        assert response.get_json()["output"] is None
        web.controller.run_console_command.assert_not_called()

    def test_failure_is_reported(self, auth_client, web):
        web.controller.run_console_command.return_value = ActionResult(
            False, "The server is not running."
        )
        body = auth_client.post("/command", data={"command": "list"}).get_json()
        assert body == {
            "success": False,
            "message": "The server is not running.",
            "output": None,
        }

    def test_commands_need_server_control(self, build, mock_settings):
        mock_settings.controlServerThroughWeb = False
        web = build()
        client = web.app.test_client()
        login(client)
        assert client.post("/command", data={"command": "list"}).status_code == 403
        assert client.get("/console").status_code == 200  # log tail still allowed
        web.controller.run_console_command.assert_not_called()


# ----------------------------------------------------------------------
# Info
# ----------------------------------------------------------------------


class TestInfo:
    def test_info_shape(self, auth_client):
        body = auth_client.get("/info").get_json()
        assert body["mods"] == ["jei-1.20.1.jar"]
        assert body["modCount"] == 1
        assert body["icon"] is None
        assert body["autoStart"] is True
        assert body["autoStop"] is True
        assert body["autoStopDelay"] == 120
        assert "serverAddress" not in body

    def test_real_server_files(self, build, mock_settings, monkeypatch):
        """Uses the protocol agent's helpers on a real directory."""
        import os

        monkeypatch.setattr(server_files, "list_mods", REAL_LIST_MODS)
        monkeypatch.setattr(server_files, "read_server_icon", REAL_READ_SERVER_ICON)
        mods = os.path.join(mock_settings.serverDir, "mods")
        os.makedirs(mods, exist_ok=True)
        for name in ("b-mod.jar", "a-mod.jar", "old.jar.disabled"):
            open(os.path.join(mods, name), "w").close()
        client = build().app.test_client()
        login(client)
        body = client.get("/info").get_json()
        assert body["mods"] == ["a-mod.jar", "b-mod.jar"]
        assert body["icon"] is None


# ----------------------------------------------------------------------
# Rate limiting
# ----------------------------------------------------------------------


class TestRateLimits:
    @pytest.fixture
    def limited(self, build, mock_settings):
        mock_settings.rateLimitRequests = 3
        mock_settings.rateLimitWindow = 60
        web = build()
        client = web.app.test_client()
        assert login(client).status_code == 302  # login is exempt
        return client

    def test_default_limit_applies_to_api(self, limited):
        statuses = [limited.get("/banned").status_code for _ in range(4)]
        assert statuses == [200, 200, 200, 429]
        assert limited.get("/banned").get_json()["success"] is False

    @pytest.mark.parametrize(
        "path", ["/healthz", "/console", "/static/styles.css", "/login"]
    )
    def test_exempt_routes(self, limited, path):
        for _ in range(8):
            response = limited.get(path)
            assert response.status_code in (200, 302), path
            response.close()

    def test_stream_is_exempt(self, limited):
        for _ in range(6):
            response = limited.get("/stream", buffered=False)
            assert response.status_code == 200
            response.close()

    def test_rate_limit_can_be_disabled(self, build, mock_settings):
        mock_settings.rateLimitEnabled = False
        mock_settings.rateLimitRequests = 1
        client = build().app.test_client()
        login(client)
        assert {client.get("/banned").status_code for _ in range(5)} == {200}


# ----------------------------------------------------------------------
# Live stream through the test client
# ----------------------------------------------------------------------


class TestStream:
    def test_stream_sends_snapshot_then_updates(self, auth_client, web):
        web.controller.get_snapshot.return_value = make_snapshot(
            revision=1, banned_players=[]
        )
        response = auth_client.get("/stream", buffered=False)
        try:
            assert response.mimetype == "text/event-stream"
            chunks = iter(response.response)
            assert sse_payload(next(chunks))["data"]["revision"] == 1
            web.controller.get_snapshot.return_value = make_snapshot(
                revision=2, state="STARTING"
            )
            web.bus.publish(Event.LIFECYCLE_CHANGED, {"state": "STARTING"})
            update = sse_payload(next(chunks))
            assert update["data"]["state"] == "STARTING"
            assert update["autoStopDelay"] == 120
        finally:
            response.close()


# ----------------------------------------------------------------------
# Real HTTP server
# ----------------------------------------------------------------------


class TestRealServer:
    @pytest.fixture
    def running(self, build, mock_settings):
        mock_settings.webServerPort = free_tcp_port()
        web = build()
        web.sse_keepalive_interval = 0.2
        thread = threading.Thread(target=web.run, daemon=True)
        thread.start()
        assert web.serving.wait(5)
        yield web, f"http://127.0.0.1:{web.bound_port}"
        web.shutdown()
        thread.join(5)
        assert not thread.is_alive()

    def test_serves_on_configured_port_and_shuts_down(self, running, mock_settings):
        web, base = running
        assert web.bound_port == mock_settings.webServerPort
        with urllib.request.urlopen(base + "/healthz", timeout=5) as response:
            assert json.load(response) == {"ok": True}

    def test_sse_over_http_and_shutdown_ends_the_stream(self, running):
        web, base = running
        session = requests.Session()
        assert (
            session.post(
                base + "/login",
                data={"username": USERNAME, "password": PASSWORD},
                allow_redirects=False,
            ).status_code
            == 302
        )
        with session.get(base + "/stream", stream=True, timeout=10) as response:
            lines = response.iter_lines(decode_unicode=True)
            first = next(line for line in lines if line.startswith("data: "))
            assert json.loads(first[6:])["data"]["revision"] == 1

            web.controller.get_snapshot.return_value = make_snapshot(
                revision=5, state="RUNNING", ready=True
            )
            web.bus.publish(Event.SERVER_STARTED, {"pid": 1})
            update = next(line for line in lines if line.startswith("data: "))
            assert json.loads(update[6:])["data"]["revision"] == 5

            assert any(line == ": keepalive" for line in lines)
            started = time.monotonic()
            web.shutdown()
            remaining = list(lines)  # the server ends the stream
            assert time.monotonic() - started < 5
            assert all(not line.startswith("data: ") for line in remaining)
        wait_until(lambda: len(web._streams) == 0, timeout=5)

    def test_parallel_requests_while_a_stream_is_open(self, running):
        _, base = running
        session = requests.Session()
        session.post(base + "/login", data={"username": USERNAME, "password": PASSWORD})
        with session.get(base + "/stream", stream=True, timeout=10):
            # A held-open SSE connection must not block other requests.
            assert session.get(base + "/banned", timeout=5).status_code == 200


# ----------------------------------------------------------------------
# Session-bound streams and credential revocation
# ----------------------------------------------------------------------


class TestSessionSecurity:
    def test_logout_ends_that_sessions_stream(self, auth_client):
        response = auth_client.get("/stream", buffered=False)
        chunks = iter(response.response)
        assert sse_payload(next(chunks))["data"]["revision"] == 1
        assert auth_client.post("/logout").status_code == 302
        with pytest.raises(StopIteration):
            next(chunks)
        response.close()

    def test_logout_leaves_other_sessions_streams_alone(self, web, auth_client):
        other = web.app.test_client()
        login(other)
        mine = auth_client.get("/stream", buffered=False)
        theirs = other.get("/stream", buffered=False)
        next(iter(mine.response))
        their_chunks = iter(theirs.response)
        next(their_chunks)
        auth_client.post("/logout")
        web.controller.get_snapshot.return_value = make_snapshot(revision=2)
        web.bus.publish(Event.SERVER_STATUS, {})
        assert sse_payload(next(their_chunks))["data"]["revision"] == 2
        mine.close()
        theirs.close()

    def test_stream_ends_when_the_session_would_expire(
        self, client, web, mock_settings
    ):
        web.sse_keepalive_interval = 0.02
        mock_settings.sessionTimeout = 0.2  # sessions take it when they open
        login(client)
        response = client.get("/stream", buffered=False)
        started = time.monotonic()
        rest = list(response.response)  # ends by itself
        assert time.monotonic() - started < 3
        assert all(not chunk.startswith(b"data: ") for chunk in rest[1:])
        response.close()

    def test_password_change_revokes_sessions_and_remember_cookies(
        self, client, mock_settings
    ):
        assert login(client, remember="on").status_code == 302
        client.delete_cookie(f"mcmonitor_session_{mock_settings.webServerPort}")
        assert client.get("/").status_code == 200  # restored from the remember cookie
        mock_settings.webPassword = "a-brand-new-password"
        assert client.get("/").status_code == 302
        assert client.get("/banned").status_code == 401

    def test_pre_epoch_session_ids_are_rejected(self, client):
        with client.session_transaction() as session:
            session["_user_id"] = "test_admin"
            session["_fresh"] = True
        assert client.get("/").status_code == 302


class TestNoExternalRequests:
    def test_pages_reference_no_third_party_hosts(self, client, auth_client):
        for html in (
            client.get("/login").get_data(as_text=True),
            auth_client.get("/").get_data(as_text=True),
        ):
            assert "googleapis" not in html
            assert "gstatic" not in html

    def test_csp_is_self_only_for_styles_and_fonts(self, auth_client):
        csp = auth_client.get("/").headers["Content-Security-Policy"]
        assert "style-src 'self';" in csp
        assert "font-src 'self';" in csp
        assert "googleapis" not in csp

    def test_bundled_font_and_its_license_are_served(self, client):
        font = client.get("/static/fonts/PressStart2P-Regular.ttf")
        license_text = client.get("/static/fonts/OFL.txt")
        assert font.status_code == 200 and len(font.data) > 10000
        assert b"SIL OPEN FONT LICENSE" in license_text.data.upper()
        font.close()
        license_text.close()


class TestRestartFrames:
    def test_stopped_with_pending_restart_renders_as_starting(self, auth_client, web):
        web.controller.get_snapshot.return_value = make_snapshot(
            state="STOPPED", restartPending=True
        )
        html = auth_client.get("/").get_data(as_text=True)
        assert '<span id="statusText">STARTING</span>' in html
        assert "Restarting" in html
        start = re.search(r'<button[^>]*id="startBtn"[^>]*>', html).group(0)
        assert "disabled" in start

    def test_stopping_with_pending_restart_offers_cancel(self, auth_client, web):
        web.controller.get_snapshot.return_value = make_snapshot(
            state="STOPPING", restartPending=True
        )
        html = auth_client.get("/").get_data(as_text=True)
        stop = re.search(r'<button[^>]*id="stopBtn"[^>]*>', html).group(0)
        assert "disabled" not in stop
        assert '<span id="stopLabel">Cancel restart</span>' in html

    def test_plain_stopping_keeps_stop_disabled(self, auth_client, web):
        web.controller.get_snapshot.return_value = make_snapshot(state="STOPPING")
        html = auth_client.get("/").get_data(as_text=True)
        assert "disabled" in re.search(r'<button[^>]*id="stopBtn"[^>]*>', html).group(0)


class TestControllerMessages:
    @pytest.mark.parametrize("success", [True, False])
    def test_action_result_message_is_shown_verbatim(self, auth_client, web, success):
        web.controller.stop_server.return_value = ActionResult(
            success, "Restart cancelled; the server stays offline."
        )
        body = auth_client.post("/action", data={"action": "stopServer"}).get_json()
        assert body["success"] is success
        assert body["message"] == "Restart cancelled; the server stays offline."

    def test_action_result_without_message_gets_a_default(self, auth_client, web):
        web.controller.start_server.return_value = ActionResult(True, "")
        assert (
            auth_client.post("/action", data={"action": "startServer"}).get_json()[
                "message"
            ]
            == "Starting the server…"
        )


# ----------------------------------------------------------------------
# Phase 3 hardening: sessions, Origin, proxies, cookies, monitor state
# ----------------------------------------------------------------------


def cookie_name(kind, mock_settings, prefix=""):
    return f"{prefix}mcmonitor_{kind}_{mock_settings.webServerPort}"


class TestSessionFixation:
    def test_login_discards_the_pre_login_csrf_secret(self, build):
        client = build(csrf=True).app.test_client()
        planted = csrf_from(client.get("/login").get_data(as_text=True))
        assert login(client, csrf_token=planted).status_code == 302
        stale = client.post(
            "/action", data={"action": "getStatus", "csrf_token": planted}
        )
        assert stale.status_code == 400
        fresh = csrf_from(client.get("/").get_data(as_text=True))
        ok = client.post("/action", data={"action": "getStatus", "csrf_token": fresh})
        assert ok.status_code == 200

    def test_login_starts_a_new_session(self, web, client):
        with client.session_transaction() as sess:
            sess["planted"] = "by an attacker"
        login(client)
        with client.session_transaction() as sess:
            assert "planted" not in sess
            assert web.sessions.check_session(sess["sid"], web._credential_epoch())


class TestServerSideSessions:
    def test_copied_session_cookie_dies_with_logout(self, web, mock_settings):
        client = web.app.test_client()
        login(client)
        name = cookie_name("session", mock_settings)
        copied = client.get_cookie(name).value
        assert client.post("/logout").status_code == 302
        thief = web.app.test_client()
        thief.set_cookie(name, copied)
        assert thief.get("/info").status_code == 401
        assert thief.post("/command", data={"command": "op Mallory"}).status_code == 401
        web.controller.run_console_command.assert_not_called()

    def test_remember_tokens_are_random_and_revoked_by_logout(self, web, mock_settings):
        name = cookie_name("remember", mock_settings)
        first, second = web.app.test_client(), web.app.test_client()
        login(first, remember="on")
        login(second, remember="on")
        token = first.get_cookie(name).value
        assert token != second.get_cookie(name).value
        assert "|" not in token  # not the old deterministic id|hmac format
        thief = web.app.test_client()
        thief.set_cookie(name, token)
        assert thief.get("/info").status_code == 200  # valid until logout
        first.post("/logout")
        assert thief.get("/info").status_code == 401  # restored session ended too
        late = web.app.test_client()
        late.set_cookie(name, token)
        assert late.get("/info").status_code == 401
        assert second.get("/info").status_code == 200  # other logins unaffected

    def test_remember_restores_on_safe_requests_only(self, web, mock_settings):
        name = cookie_name("remember", mock_settings)
        owner = web.app.test_client()
        login(owner, remember="on")
        browser = web.app.test_client()
        browser.set_cookie(name, owner.get_cookie(name).value)
        refused = browser.post("/action", data={"action": "getStatus"})
        assert refused.status_code == 401
        restored = browser.get("/info")
        assert restored.status_code == 200
        assert restored.headers["X-CSRF-Token"]  # the page adopts the new token
        assert browser.post("/action", data={"action": "getStatus"}).status_code == 200

    def test_bad_remember_cookie_is_deleted(self, client, mock_settings):
        name = cookie_name("remember", mock_settings)
        client.set_cookie(name, "admin|0123|forged")
        response = client.get("/info")
        assert response.status_code == 401
        assert any(
            c.startswith(f"{name}=;") for c in response.headers.getlist("Set-Cookie")
        )

    def test_expired_session_needs_a_new_login(self, web, client):
        login(client)
        web.sessions._clock = lambda: time.time() + 13 * 3600
        assert client.get("/info").status_code == 401

    def test_log_out_everywhere(self, web, mock_settings):
        first, second = web.app.test_client(), web.app.test_client()
        login(first)
        login(second, remember="on")
        assert first.post("/logout", data={"scope": "all"}).status_code == 302
        assert second.get("/info").status_code == 401
        assert web.sessions.token_count() == 0
        assert "Signed out everywhere" in first.get("/login").get_data(as_text=True)

    def test_logout_with_a_lapsed_session_still_forgets_the_browser(
        self, web, mock_settings
    ):
        name = cookie_name("remember", mock_settings)
        client = web.app.test_client()
        login(client, remember="on")
        token = client.get_cookie(name).value
        web.sessions._sessions.clear()  # the session lapsed server-side
        assert client.post("/logout").status_code == 302
        stranger = web.app.test_client()
        stranger.set_cookie(name, token)
        assert stranger.get("/info").status_code == 401


class TestOriginCheck:
    def test_cross_origin_post_is_refused(self, auth_client, web):
        response = auth_client.post(
            "/action",
            data={"action": "stopServer"},
            headers={"Origin": "http://localhost:8123"},
        )
        assert response.status_code == 403
        assert "trustedProxies" in response.get_json()["message"]
        web.controller.stop_server.assert_not_called()

    @pytest.mark.parametrize(
        "headers,status",
        [
            ({"Origin": "http://localhost"}, 200),
            ({"Origin": "http://LOCALHOST:80"}, 200),
            ({"Origin": "null"}, 403),
            ({"Origin": "https://localhost"}, 403),
            ({"Referer": "http://localhost/"}, 200),
            ({"Referer": "http://evil.example/page"}, 403),
            ({}, 200),  # non-browser clients still need the CSRF token
        ],
    )
    def test_origin_and_referer(self, auth_client, headers, status):
        response = auth_client.post(
            "/action", data={"action": "getStatus"}, headers=headers
        )
        assert response.status_code == status

    def test_cross_origin_login_is_refused(self, client):
        response = client.post(
            "/login",
            data={"username": USERNAME, "password": PASSWORD},
            headers={"Origin": "http://localhost:9000"},
        )
        assert response.status_code == 403
        assert "Request blocked" in response.get_data(as_text=True)
        assert client.get("/").status_code == 302


class TestTrustedProxies:
    def fail_logins(self, client, address, count=5):
        for _ in range(count):
            client.post(
                "/login",
                data={"username": USERNAME, "password": "wrong"},
                headers={"X-Forwarded-For": address},
            )

    def test_lockout_follows_the_forwarded_client(self, build, mock_settings, caplog):
        mock_settings.trustedProxies = ["127.0.0.1"]
        web = build()
        attacker, admin = web.app.test_client(), web.app.test_client()
        with caplog.at_level(logging.WARNING):
            self.fail_logins(attacker, "203.0.113.66")
        assert "203.0.113.66" in caplog.text
        response = admin.post(
            "/login",
            data={"username": USERNAME, "password": PASSWORD},
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
        assert response.status_code == 302

    def test_untrusted_peers_cannot_spoof_their_address(self, build, mock_settings):
        client = build().app.test_client()  # trustedProxies defaults to none
        self.fail_logins(client, "203.0.113.66")
        response = client.post(
            "/login",
            data={"username": USERNAME, "password": PASSWORD},
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
        assert response.status_code == 429

    def test_https_proxy_origin_is_accepted(self, build, mock_settings):
        mock_settings.trustedProxies = ["127.0.0.0/8"]
        client = build().app.test_client()
        login(client)
        proxied = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "mc.example.net"}
        ok = client.post(
            "/action",
            data={"action": "getStatus"},
            headers={**proxied, "Origin": "https://mc.example.net"},
        )
        assert ok.status_code == 200
        bad = client.post(
            "/action",
            data={"action": "getStatus"},
            headers={**proxied, "Origin": "http://mc.example.net:8123"},
        )
        assert bad.status_code == 403


class TestCookies:
    def test_default_cookies_are_strict_and_not_secure(self, client, mock_settings):
        response = login(client, remember="on")
        cookies = response.headers.getlist("Set-Cookie")
        for kind in ("session", "remember"):
            cookie = next(
                c for c in cookies if c.startswith(cookie_name(kind, mock_settings))
            )
            assert "SameSite=Strict" in cookie and "HttpOnly" in cookie
            assert "Secure" not in cookie
        assert "Strict-Transport-Security" not in response.headers

    def test_secure_cookies(self, build, mock_settings):
        mock_settings.secureCookies = True
        client = build().app.test_client()
        response = login(client, remember="on")
        cookies = response.headers.getlist("Set-Cookie")
        for kind in ("session", "remember"):
            cookie = next(
                c
                for c in cookies
                if c.startswith(cookie_name(kind, mock_settings, prefix="__Host-"))
            )
            assert "Secure" in cookie and "Path=/" in cookie and "Domain" not in cookie
        assert response.headers["Strict-Transport-Security"].startswith("max-age=")


class TestMonitorWarnings:
    BLOCKED = {"armed": True, "listening": False, "error": "Address already in use"}

    def test_info_and_page_report_listener_and_rcon_state(self, build):
        web = build()
        web.controller.client.rcon_status = "auth_failed"
        web.auto_start_status = lambda: dict(self.BLOCKED)
        client = web.app.test_client()
        login(client)
        body = client.get("/info").get_json()
        assert body["rconStatus"] == "auth_failed"
        assert body["autoStartStatus"] == self.BLOCKED
        html = client.get("/").get_data(as_text=True)
        assert "auto-start can't use the game port" in html
        assert "Sleeping — a join attempt wakes it" not in html
        assert '"rconStatus": "auth_failed"' in html  # bootstrap for the script

    def test_json_responses_carry_monitor_state(self, auth_client):
        body = auth_client.post("/action", data={"action": "getStatus"}).get_json()
        assert body["monitor"] == {"rconStatus": None, "autoStartStatus": None}

    def test_automation_text_when_nothing_can_start_the_server(
        self, build, mock_settings
    ):
        mock_settings.autoStart = False
        mock_settings.controlServerThroughWeb = False
        client = build().app.test_client()
        login(client)
        html = client.get("/").get_data(as_text=True)
        assert "start the server from this dashboard" not in html
        assert "started outside the monitor" in html


# ----------------------------------------------------------------------
# Bounded server: slow and idle connections (security-1)
# ----------------------------------------------------------------------


class TestBoundedServer:
    @pytest.fixture
    def serve(self, build, mock_settings):
        started = []

        def _serve(**limits):
            mock_settings.webServerPort = free_tcp_port()
            web = build()
            web.server_limits = ServerLimits(**limits)
            thread = threading.Thread(target=web.run, daemon=True)
            thread.start()
            assert web.serving.wait(5)
            started.append((web, thread))
            return web

        yield _serve
        for web, thread in started:
            web.shutdown()
            thread.join(5)

    @staticmethod
    def idle_connections(port, count, source):
        sockets = []
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind((source, 0))
            except OSError:
                sock.close()
                pytest.skip(f"cannot use {source} as a source address here")
            sock.settimeout(5)
            try:
                sock.connect(("127.0.0.1", port))
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")  # headers never end
            except OSError:
                pass  # already refused
            sockets.append(sock)
        return sockets

    @staticmethod
    def closed_by_server(sock):
        sock.settimeout(2)
        try:
            return sock.recv(1) == b""
        except (ConnectionError, OSError):
            return True

    @staticmethod
    def admin_can_use_the_dashboard(web):
        session = requests.Session()
        base = f"http://127.0.0.1:{web.bound_port}"
        login = session.post(
            base + "/login",
            data={"username": USERNAME, "password": PASSWORD},
            allow_redirects=False,
            timeout=5,
        )
        assert login.status_code == 302
        assert session.get(base + "/info", timeout=5).status_code == 200

    def test_idle_flood_from_one_address_is_capped(self, serve):
        web = serve(max_connections_per_ip=8, request_read_timeout=30)
        flood = self.idle_connections(web.bound_port, 40, "127.0.0.2")
        try:
            refused = sum(self.closed_by_server(sock) for sock in flood[8:])
            assert refused == 32
            assert web._server.open_connections() <= 8
            self.admin_can_use_the_dashboard(web)
        finally:
            for sock in flood:
                sock.close()

    def test_flood_from_many_addresses_cannot_keep_requests_out(self, serve):
        web = serve(
            max_connections=16, max_connections_per_ip=6, request_read_timeout=30
        )
        flood = []
        try:
            for host in range(2, 10):
                flood += self.idle_connections(web.bound_port, 6, f"127.0.0.{host}")
            assert web._server.open_connections() <= 16
            for _ in range(3):  # each request evicts the oldest idle connection
                self.admin_can_use_the_dashboard(web)
        finally:
            for sock in flood:
                sock.close()

    def test_trickled_request_is_dropped_at_the_deadline(self, serve):
        web = serve(request_read_timeout=0.5)
        sock = socket.create_connection(("127.0.0.1", web.bound_port), timeout=5)
        try:
            started = time.monotonic()
            sock.sendall(b"GET / HTTP/1.1\r\n")
            sock.settimeout(0.1)
            closed = False
            while not closed and time.monotonic() - started < 5:
                try:
                    sock.sendall(b"X")  # one byte at a time, never a full line
                    closed = sock.recv(1) == b""
                except TimeoutError:
                    continue
                except OSError:
                    closed = True
            assert closed
            assert 0.4 < time.monotonic() - started < 3
            wait_until(lambda: web._server.open_connections() == 0, timeout=5)
        finally:
            sock.close()

    def test_streams_outlive_the_read_deadline(self, serve):
        web = serve(request_read_timeout=0.3)
        session = requests.Session()
        base = f"http://127.0.0.1:{web.bound_port}"
        session.post(base + "/login", data={"username": USERNAME, "password": PASSWORD})
        with session.get(base + "/stream", stream=True, timeout=10) as response:
            lines = response.iter_lines(decode_unicode=True)
            next(line for line in lines if line.startswith("data: "))
            time.sleep(0.6)
            web.controller.get_snapshot.return_value = make_snapshot(revision=7)
            web.bus.publish(Event.SERVER_STATUS, {})
            update = next(line for line in lines if line.startswith("data: "))
            assert json.loads(update[6:])["data"]["revision"] == 7


# ----------------------------------------------------------------------
# Fix round 2
# ----------------------------------------------------------------------


class TestSessionRenewal:
    def restarted(self, build, client):
        """A second WebServer on the same settings and data dir (a monitor
        restart), with the browser's cookies carried over."""
        web = build()
        browser = web.app.test_client()
        for cookie in client._cookies.values():
            browser.set_cookie(cookie.key, cookie.value)
        return web, browser

    def test_page_left_open_across_a_restart_can_still_act(self, build, mock_settings):
        first = build(csrf=True)
        client = first.app.test_client()
        assert login(client, csrf=True, remember="on").status_code == 302
        page_token = csrf_from(client.get("/").get_data(as_text=True))
        web, browser = self.restarted(build, client)
        web.app.config["WTF_CSRF_ENABLED"] = True

        stream = browser.get("/stream", buffered=False)  # the page reconnects first
        first_message = sse_payload(next(iter(stream.response)))
        stream.close()
        fresh = first_message["csrfToken"]
        assert fresh and fresh != page_token
        ok = browser.post(
            "/action", data={"action": "startServer", "csrf_token": fresh}
        )
        assert ok.status_code == 200
        web.controller.start_server.assert_called_once()

    def test_stale_token_is_flagged_and_csrf_endpoint_recovers(self, build):
        web = build(csrf=True)
        client = web.app.test_client()
        login(client, csrf=True)
        stale = client.post("/action", data={"action": "getStatus", "csrf_token": "x"})
        assert stale.status_code == 400 and stale.get_json()["csrfError"] is True
        token = client.get("/csrf").get_json()["csrfToken"]
        ok = client.post("/action", data={"action": "getStatus", "csrf_token": token})
        assert ok.status_code == 200
        assert web.app.test_client().get("/csrf").status_code == 401

    def test_logout_with_a_stale_token_still_logs_out(self, build, mock_settings):
        web = build(csrf=True)
        client = web.app.test_client()
        login(client, csrf=True, remember="on")
        response = client.post("/logout", data={"csrf_token": "stale"})
        assert response.status_code == 302
        assert response.headers["Location"] == "/login"
        assert client.get("/").status_code == 302
        assert web.sessions.token_count() == 0

    def test_payloads_carry_the_rcon_status(self, auth_client, web):
        web.controller.client.rcon_status = "ok"
        body = auth_client.post("/action", data={"action": "getStatus"}).get_json()
        assert body["rconStatus"] == "ok"


class TestLoginBackstops:
    def test_ipv6_rotation_within_a_64_is_one_client(self, build, mock_settings):
        mock_settings.trustedProxies = ["127.0.0.1"]
        client = build().app.test_client()
        codes = [
            client.post(
                "/login",
                data={"username": USERNAME, "password": "wrong"},
                headers={"X-Forwarded-For": f"2001:db8:1:2::{n:x}"},
            ).status_code
            for n in range(1, 8)
        ]
        assert codes == [200, 200, 200, 200, 429, 429, 429]
        other_64 = client.post(
            "/login",
            data={"username": USERNAME, "password": PASSWORD},
            headers={"X-Forwarded-For": "2001:db8:1:3::1"},
        )
        assert other_64.status_code == 302

    def test_global_backstop_across_addresses(self, build, mock_settings):
        mock_settings.trustedProxies = ["127.0.0.1"]
        web = build()
        web.login_tracker.global_threshold = 3
        client = web.app.test_client()
        for n in range(3):
            client.post(
                "/login",
                data={"username": USERNAME, "password": "wrong"},
                headers={"X-Forwarded-For": f"198.51.100.{n}"},
            )
        refused = client.post(
            "/login",
            data={"username": USERNAME, "password": PASSWORD},
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert refused.status_code == 429
        assert "Too many failed logins on this dashboard" in refused.get_data(
            as_text=True
        )


class TestProxyOrigins:
    @pytest.fixture
    def proxied(self, build, mock_settings):
        mock_settings.trustedProxies = ["127.0.0.1"]
        web = build(csrf=True)
        client = web.app.test_client()
        return web, client

    def post_login(self, client, headers):
        token = csrf_from(client.get("/login", headers=headers).get_data(as_text=True))
        return client.post(
            "/login",
            data={"username": USERNAME, "password": PASSWORD, "csrf_token": token},
            headers=headers,
        )

    def test_forwarded_port_completes_a_host_without_port(self, proxied):
        _, client = proxied
        headers = {
            "Host": "mc.example.com",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Port": "8443",
            "Origin": "https://mc.example.com:8443",
        }
        assert self.post_login(client, headers).status_code == 302

    def test_missing_port_gets_an_accurate_hint(self, proxied):
        _, client = proxied
        headers = {
            "Host": "mc.example.com",
            "X-Forwarded-Proto": "https",
            "Origin": "https://mc.example.com:8443",
        }
        response = self.post_login(client, headers)
        assert response.status_code == 403
        assert "$http_host" in response.get_data(as_text=True)

    def test_https_post_without_referer_is_accepted(self, proxied):
        _, client = proxied
        headers = {
            "Host": "mc.example.com",
            "X-Forwarded-Proto": "https",
            "Origin": "https://mc.example.com",
        }
        assert self.post_login(client, headers).status_code == 302
        token = csrf_from(client.get("/", headers=headers).get_data(as_text=True))
        ok = client.post(
            "/action",
            data={"action": "getStatus", "csrf_token": token},
            headers=headers,
        )
        assert ok.status_code == 200


class TestServerCleanup:
    @pytest.fixture
    def serve(self, build, mock_settings):
        started = []

        def _serve(**limits):
            mock_settings.webServerPort = free_tcp_port()
            web = build()
            web.server_limits = ServerLimits(**limits)
            web.sse_keepalive_interval = 0.1
            thread = threading.Thread(target=web.run, daemon=True)
            thread.start()
            assert web.serving.wait(5)
            started.append((web, thread))
            return web

        yield _serve
        for web, thread in started:
            web.shutdown()
            thread.join(5)

    @staticmethod
    def epoll_fds():
        import os

        try:
            names = os.listdir("/proc/self/fd")
        except OSError:
            return None
        count = 0
        for name in names:
            try:
                count += "eventpoll" in os.readlink(f"/proc/self/fd/{name}")
            except OSError:
                pass
        return count

    def test_long_streams_are_freed_without_garbage_collection(self, serve):
        import gc

        web = serve(request_read_timeout=0.3)
        base = f"http://127.0.0.1:{web.bound_port}"
        session = requests.Session()
        session.post(base + "/login", data={"username": USERNAME, "password": PASSWORD})
        before = self.epoll_fds()
        gc.disable()
        try:
            for _ in range(5):
                response = session.get(base + "/stream", stream=True, timeout=10)
                next(response.iter_lines())
                time.sleep(0.5)  # outlive the request read deadline
                response.close()
            wait_until(lambda: len(web._streams) == 0, timeout=5)
            wait_until(lambda: web._server.open_connections() == 0, timeout=5)
            if before is not None:
                assert self.epoll_fds() <= before
        finally:
            gc.enable()

    def test_slow_bodies_cannot_keep_requests_out(self, serve):
        web = serve(
            max_connections=16, max_connections_per_ip=6, request_read_timeout=30
        )
        port = web.bound_port
        flood = []
        try:
            for host in range(2, 6):
                for _ in range(6):
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        sock.bind((f"127.0.0.{host}", 0))
                    except OSError:
                        sock.close()
                        pytest.skip("cannot use 127.0.0.x as a source address here")
                    sock.settimeout(5)
                    try:
                        sock.connect(("127.0.0.1", port))
                        sock.sendall(
                            b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: 8000\r\n"
                            b"Content-Type: application/x-www-form-urlencoded\r\n\r\nu"
                        )
                    except OSError:
                        pass
                    flood.append(sock)
            time.sleep(0.3)
            assert web._server.open_connections() <= 16
            for _ in range(3):
                TestBoundedServer.admin_can_use_the_dashboard(web)
        finally:
            for sock in flood:
                sock.close()
