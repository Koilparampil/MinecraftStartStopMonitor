"""Tabs share one live stream: a visible leader relays it to the others.

A tab brought to the front during a monitor outage must not claim "Live"
while the leader shows "Reconnecting…": the heartbeats it heard while it was
hidden came from a leader whose stream is down now (FU-8). Runs the real
WebServer in-process with a fake controller; Playwright drives two tabs of
one browser profile.
"""

import threading
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import expect

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

# Runs before the dashboard's own script: the tab starts hidden (headless
# Chromium shows every tab), and window.__show() brings it to the front.
HIDDEN_TAB = """
(() => {
  let hidden = true;
  Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
  Object.defineProperty(document, 'visibilityState', {
    configurable: true, get: () => (hidden ? 'hidden' : 'visible'),
  });
  window.__show = () => {
    hidden = false;
    document.dispatchEvent(new Event('visibilitychange'));
  };
})();
"""

RECORD_CONNECTION = """() => {
  const node = document.getElementById('connText');
  window.__conn = [node.textContent];
  new MutationObserver(() => {
    const text = node.textContent;
    if (window.__conn[window.__conn.length - 1] !== text) window.__conn.push(text);
  }).observe(node, {childList: true, characterData: true, subtree: true});
}"""


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


def test_a_tab_shown_during_an_outage_never_claims_live(
    browser, mock_settings, monkeypatch
):
    mock_settings.webServerPort = free_tcp_port()
    mock_settings.webBindHost = "127.0.0.1"
    mock_settings.playerAvatars = False
    monkeypatch.setattr(server_files, "list_mods", lambda _d: [])
    monkeypatch.setattr(server_files, "read_server_icon", lambda _d: None)
    base = f"http://127.0.0.1:{mock_settings.webServerPort}"
    running = [start_monitor()]
    context = browser.new_context()
    script_errors = []
    try:
        leader = context.new_page()
        leader.on("pageerror", lambda error: script_errors.append(str(error)))
        leader.goto(base + "/login")
        leader.fill("#username", mock_settings.webUsername)
        leader.fill("#password", mock_settings.webPassword)
        leader.check("input[name=remember]")  # the restarted monitor restores it
        leader.click("button[type=submit]")
        leader.wait_for_url(base + "/")
        expect(leader.locator("#connText")).to_have_text("Live", timeout=10000)

        hidden = context.new_page()
        hidden.on("pageerror", lambda error: script_errors.append(str(error)))
        hidden.add_init_script(HIDDEN_TAB)
        hidden.goto(base + "/")
        expect(hidden.locator("#connText")).to_have_text(
            "Paused while the tab is hidden"
        )
        hidden.wait_for_timeout(2500)  # it hears the leader's heartbeats
        hidden.evaluate(RECORD_CONNECTION)

        stop_monitor(*running.pop())  # the outage
        hidden.evaluate("() => window.__show()")  # within a second of it
        expect(leader.locator("#connText")).not_to_have_text("Live", timeout=5000)
        hidden.wait_for_timeout(4000)  # past the leader timeout (5 s)
        during_outage = hidden.evaluate("() => window.__conn")
        assert "Live" not in during_outage, during_outage

        running.append(start_monitor())  # the monitor is back
        expect(hidden.locator("#connText")).to_have_text("Live", timeout=20000)
        expect(leader.locator("#connText")).to_have_text("Live", timeout=20000)
    finally:
        context.close()
        for web_and_thread in running:
            stop_monitor(*web_and_thread)
    assert not script_errors, script_errors
