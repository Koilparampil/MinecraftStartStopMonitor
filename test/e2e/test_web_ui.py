"""End-to-end: drive the real dashboard in Chromium against the dummy server.

Covers login, start (LOADING while mods "load", then ONLINE), the live
player list, kick / ban / unban through in-page dialogs, the console and an
RCON command, restart and stop.
"""

import re
import time

import pytest
from playwright.sync_api import Page, expect

from test.support import wait_until

from .helpers import USERNAME

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

# A short startup delay makes the RUNNING-but-not-ready ("LOADING") phase visible.
MONITOR_OPTIONS = {"dummy_env": {"DUMMY_STARTUP_DELAY": "2"}, "auto_stop_delay": 600}

STOP_COOLDOWN = 6.0  # supervisor.server_stopping_cooldown (5 s) + margin

RECORD_STATES = """() => {
  const node = document.getElementById('statusText');
  window.__states = [node.textContent];
  new MutationObserver(() => {
    const text = node.textContent;
    if (window.__states[window.__states.length - 1] !== text) window.__states.push(text);
  }).observe(node, {childList: true, characterData: true, subtree: true});
}"""


def log_in(page: Page, app) -> None:
    page.goto(app.base_url + "/")
    expect(page).to_have_url(re.compile(r"/login"))
    expect(page.get_by_role("heading", name="Admin login")).to_be_visible()
    page.fill("#username", USERNAME)
    page.fill("#password", app.password)
    page.click("button[type=submit]")
    expect(page).to_have_url(app.base_url + "/")
    expect(page.locator("#connText")).to_have_text("Live")


def states(page: Page) -> list[str]:
    return page.evaluate("() => window.__states")


def confirm(page: Page, reason: str | None = None) -> None:
    dialog = page.locator("#confirmDialog")
    expect(dialog).to_be_visible()
    if reason is not None:
        page.fill("#confirmReason", reason)
    page.click("#confirmOk")
    expect(dialog).to_be_hidden()


def row(page: Page, name: str):
    return page.locator(f'#playersBody tr[data-player="{name.lower()}"]')


def expect_toast(page: Page, text: str) -> None:
    expect(page.locator("#toasts .toast", has_text=text).first).to_be_visible(
        timeout=20000
    )


def test_login_rejects_bad_password(page, monitor_app):
    page.goto(monitor_app.base_url + "/login")
    page.fill("#username", USERNAME)
    page.fill("#password", "definitely-wrong")
    page.click("button[type=submit]")
    expect(page.locator("#loginError")).to_contain_text("Invalid username or password")


def test_full_lifecycle(page, monitor_app):
    app = monitor_app
    log_in(page, app)
    expect(page.locator("#statusText")).to_have_text("OFFLINE")
    page.evaluate(RECORD_STATES)

    # Start: LOADING while the dummy "loads mods", then ONLINE.
    page.click("#startBtn")
    expect_toast(page, "Starting the server")
    expect(page.locator("#statusText")).to_have_text("ONLINE", timeout=60000)
    seen = states(page)
    assert "LOADING" in seen, seen
    assert seen.index("LOADING") < len(seen) - 1 and seen[-1] == "ONLINE", seen

    # The dummy's roster shows up live.
    expect(page.locator("#playersBody tr.is-online")).to_have_count(3, timeout=30000)
    for name in ("TestPlayer1", "TestPlayer2", "TestPlayer3"):
        expect(row(page, name).locator(".player-status")).to_have_text("Online")
    expect(page.locator("#playerCountChip")).to_have_text("3 online")

    # Kick.
    row(page, "TestPlayer1").locator('button[data-player-action="kick"]').click()
    confirm(page, reason="e2e kick")
    expect_toast(page, "Kicked")
    expect(row(page, "TestPlayer1")).to_have_class(
        re.compile(r"is-offline"), timeout=30000
    )
    expect(page.locator("#playersBody tr.is-online")).to_have_count(2, timeout=30000)

    # Ban (written to the server's banned-players.json by the server itself).
    row(page, "TestPlayer2").locator('button[data-player-action="ban"]').click()
    confirm(page, reason="e2e ban")
    expect_toast(page, "Banned")
    expect(row(page, "TestPlayer2")).to_have_class(
        re.compile(r"is-banned"), timeout=30000
    )
    expect(row(page, "TestPlayer2")).to_contain_text("e2e ban")
    assert "TestPlayer2" in app.banned_names()

    # Unban.
    row(page, "TestPlayer2").locator('button[data-player-action="unban"]').click()
    confirm(page)
    expect_toast(page, "Unbanned")
    expect(row(page, "TestPlayer2")).not_to_have_class(
        re.compile(r"is-banned"), timeout=30000
    )
    assert "TestPlayer2" not in app.banned_names()

    # Console: the log tail and an RCON command.
    expect(page.locator("#consoleLog")).to_contain_text("Done (", timeout=20000)
    page.fill("#commandInput", "list")
    page.press("#commandInput", "Enter")
    expect(page.locator("#consoleTranscript")).to_contain_text(
        "players online", timeout=20000
    )

    # Restart: back to ONLINE with a fresh process and the default roster.
    page.evaluate(RECORD_STATES)
    page.click("#restartBtn")
    confirm(page)
    expect(page.locator("#statusText")).to_have_text("STOPPING", timeout=20000)
    stopped_at = time.monotonic()
    expect(page.locator("#statusText")).to_have_text("ONLINE", timeout=90000)
    seen = states(page)
    assert seen[0] == "ONLINE" and "STOPPING" in seen and seen[-1] == "ONLINE", seen
    expect(page.locator("#playersBody tr.is-online")).to_have_count(3, timeout=30000)

    # Stop: OFFLINE and the dummy process is gone. The supervisor refuses a
    # stop within its cooldown after the previous stop (the restart's).
    time.sleep(max(0.0, STOP_COOLDOWN - (time.monotonic() - stopped_at)))
    page.click("#stopBtn")
    confirm(page)
    expect(page.locator("#statusText")).to_have_text("OFFLINE", timeout=60000)
    wait_until(
        lambda: not app.dummy_processes(), timeout=15, message="dummy still running"
    )
    expect(page.locator("#startBtn")).to_be_enabled()


def test_expired_session_redirects_to_login(page, monitor_app):
    log_in(page, monitor_app)
    page.context.clear_cookies()
    page.click("#refreshBtn")
    expect(page).to_have_url(re.compile(r"/login"), timeout=15000)
