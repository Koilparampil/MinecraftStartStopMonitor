"""Pytest configuration and shared fixtures for the whole suite.

Rules (see docs/ARCHITECTURE.md, "Testing rules"):
- Never use fixed ports: use test.support.free_tcp_port().
- Never write outside tmp_path (cwd-relative files like data/ are redirected).
- Settings are changed only through the `mock_settings` fixture, which
  monkeypatches the real singleton and restores it after each test.
"""

import os
import sys

import pytest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

if sys.platform == "win32":  # emoji-safe console output on Windows CI
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


TEST_SETTINGS = {
    "serverDir": None,  # set per test to a tmp dir when needed
    "startCommand": "java -jar server.jar nogui",
    "serverHost": "127.0.0.1",
    "serverPort": 25565,
    "rconPort": 25575,
    "rconPassword": "test_rcon_password",
    "pollingRate": 5,
    "stopTimeout": 10,
    "processName": "java",
    "useWebServer": True,
    "webBindHost": "127.0.0.1",
    "webServerPort": 25580,
    "webUsername": "test_admin",
    "webPassword": "test_web_password",
    "controlServerThroughWeb": True,
    "showServerIPAddress": False,
    "webConsole": True,
    "playerAvatars": False,
    "enablePlayerTracking": True,
    "autoStart": True,
    "autoStop": True,
    "listenerBindHost": "127.0.0.1",
    "sleepingVersionName": "Sleeping",
    "respectWhitelist": True,
    "autoStopDelay": 120,
    "sessionSecretKey": "test_secret_key_123456789012345678901234567890",
    "sessionTimeout": 3600,
    "maxLoginAttempts": 5,
    "lockoutDuration": 300,
    "rateLimitEnabled": True,
    "rateLimitRequests": 300,
    "rateLimitWindow": 60,
}


@pytest.fixture
def mock_settings(monkeypatch, tmp_path):
    """The real settings singleton with deterministic test values.

    Tests tweak values with plain attribute assignment
    (``mock_settings.autoStop = False``); monkeypatch restores everything.
    serverDir defaults to an empty tmp dir.
    """
    from src.settings import settings

    server_dir = tmp_path / "server"
    server_dir.mkdir(exist_ok=True)
    for key, value in TEST_SETTINGS.items():
        monkeypatch.setattr(settings, key, value, raising=False)
    monkeypatch.setattr(settings, "serverDir", str(server_dir), raising=False)
    monkeypatch.setattr(settings, "dataDir", str(tmp_path / "data"), raising=False)

    class _SettingsProxy:
        """Reads go to the singleton; writes go through monkeypatch so they
        are undone after the test."""

        def __getattr__(self, name):
            return getattr(settings, name)

        def __setattr__(self, name, value):
            monkeypatch.setattr(settings, name, value, raising=False)

        def __getitem__(self, key):
            return getattr(settings, key)

        def __setitem__(self, key, value):
            monkeypatch.setattr(settings, key, value, raising=False)

    return _SettingsProxy()


@pytest.fixture
def server_dir(mock_settings):
    """Path of the tmp server directory configured in mock_settings."""
    from pathlib import Path

    return Path(mock_settings.serverDir)


@pytest.fixture
def in_tmp_cwd(tmp_path, monkeypatch):
    """Run the test with cwd=tmp_path so cwd-relative files stay sandboxed."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
