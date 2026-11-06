"""A dashboard left open across a monitor restart keeps working.

With "Keep me signed in", the restarted monitor restores the login into a new
session when the page's live stream reconnects. The page must pick up that
session's CSRF token (it arrives in the stream's first message), so actions
and Log out work without a reload. Runs the real WebServer in-process with a
fake controller; Playwright drives the page.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import expect, sync_playwright

from src.events import EventBus
from src.lifecycle.messages import ActionResult
from src.minecraft import server_files
from src.web_server import WebServer
from test.support import free_tcp_port

pytestmark = pytest.mark.e2e

SNAPSHOT = {
    "revision": 1,
    "state": "STOPPED",
    "ready": False,
    "playerData": "unknown",
    "playerCount": 0,
    "maxPlayers": 20,
    "players": [],
    "banned_players": [],
    "server": {"version": None, "motd": None, "source": None},
    "startedAt": None,
    "autoStopAt": None,
    "restartPending": False,
}


def start_monitor():
    controller = MagicMock()
    controller.get_snapshot.return_value = dict(SNAPSHOT)
    controller.start_server.return_value = ActionResult(True, "Starting the server…")
    web = WebServer(controller, EventBus())
    thread = threading.Thread(target=web.run, daemon=True)
    thread.start()
    assert web.serving.wait(5)
    return web, thread


def stop_monitor(web, thread):
    web.shutdown()
    thread.join(5)
    web.close()


def test_open_page_survives_a_monitor_restart(mock_settings, monkeypatch):
    mock_settings.webServerPort = free_tcp_port()
    mock_settings.webBindHost = "127.0.0.1"
    mock_settings.playerAvatars = False
    monkeypatch.setattr(server_files, "list_mods", lambda _d: [])
    monkeypatch.setattr(server_files, "read_server_icon", lambda _d: None)
    base = f"http://127.0.0.1:{mock_settings.webServerPort}"

    first = start_monitor()
    second = None
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(base + "/login")
            page.fill("#username", mock_settings.webUsername)
            page.fill("#password", mock_settings.webPassword)
            page.check("input[name=remember]")
            page.click("button[type=submit]")
            page.wait_for_url(base + "/")
            expect(page.locator("#connText")).to_have_text("Live", timeout=10000)
            page.click("#consoleDetails > summary")  # only the stream polls now
            old_token = page.get_attribute('meta[name="csrf-token"]', "content")

            stop_monitor(*first)
            first = None
            second = start_monitor()  # same settings, secret key and data dir

            # The stream reconnects first and restores the remembered login.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if second[0].controller.get_snapshot.called and (
                    page.get_attribute('meta[name="csrf-token"]', "content") != old_token
                ):
                    break
                page.wait_for_timeout(200)
            expect(page.locator("#connText")).to_have_text("Live", timeout=10000)

            page.click("#startBtn")
            expect(page.locator("#toasts .toast--success")).to_contain_text(
                "Starting", timeout=10000
            )
            second[0].controller.start_server.assert_called_once()
            assert page.locator("#toasts .toast--error").count() == 0

            page.click(".logout-form button")
            page.wait_for_url(base + "/login", timeout=10000)
            expect(page.locator(".banner--info")).to_contain_text("signed out")
        finally:
            browser.close()
            for running in (first, second):
                if running:
                    stop_monitor(*running)
