"""Request-level tests for the web dashboard.

Flask's test client drives the full pipeline (routing, Flask-Login, CSRF,
rate limits, templates, JSON); the last class runs the real threaded
werkzeug server on a free port. The controller is a MagicMock returning
documented snapshots and ActionResults.
"""

import json
import re
import threading
import time
import urllib.request
from unittest.mock import MagicMock

import pytest
import requests

from src.events import Event, EventBus
from src.lifecycle.messages import ActionResult
from src.minecraft import server_files
from src.web_server import WebServer
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

    def test_logout_requires_token(self, build):
        client = build(csrf=True).app.test_client()
        login(client, csrf=True)
        assert client.post("/logout").status_code == 400
        token = csrf_from(client.get("/").get_data(as_text=True))
        assert client.post("/logout", data={"csrf_token": token}).status_code == 302


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
