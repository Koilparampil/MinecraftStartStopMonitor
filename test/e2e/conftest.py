"""Fixtures for the e2e suite (run with: uv run poe smoke).

Each test module gets its own sandboxed monitor (free ports, tmp server dir,
tmp data dir, tmp cwd). A module customises it with a module-level
``MONITOR_OPTIONS`` dict (``dummy_env``, ``auto_stop_delay``, ``polling_rate``).
"""

import pytest

from .helpers import MonitorSession, run_monitor


@pytest.fixture(scope="module")
def monitor_app(request, tmp_path_factory):
    options = dict(getattr(request.module, "MONITOR_OPTIONS", {}))
    with run_monitor(tmp_path_factory.mktemp("monitor"), **options) as app:
        yield app


@pytest.fixture
def api(monitor_app):
    return MonitorSession(monitor_app).login()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    """A fresh page that fails the test on native dialogs or script errors."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(15000)
    native_dialogs, script_errors = [], []

    def on_dialog(dialog):
        native_dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", on_dialog)
    page.on("pageerror", lambda error: script_errors.append(str(error)))
    yield page
    try:
        page.goto("about:blank", timeout=3000)  # drop the SSE connection first
    except Exception:
        pass
    context.close()
    assert not native_dialogs, f"native dialogs were used: {native_dialogs}"
    assert not script_errors, f"page script errors: {script_errors}"
