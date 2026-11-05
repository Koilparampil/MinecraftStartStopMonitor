"""``python -m src.main`` as a subprocess (--no-web).

- starts, logs (stdout + monitor.log, no secrets), sleeps on the game port and
  exits 0 on SIGTERM, releasing the port;
- first run without a terminal prints help and exits 1; invalid settings exit 1;
- with the dummy Minecraft server: a join attempt launches it, SIGTERM stops
  the monitor but never the server, and the next monitor adopts it.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

import psutil
import pytest
import yaml
from mcstatus import JavaServer

from test.integration.test_auto_start_listener import can_bind, join_attempt
from test.support import free_tcp_ports, wait_until
from test.support.dummy_server import (
    DEFAULT_RCON_PASSWORD,
    DUMMY_PROCESS_NAME,
    dummy_start_command,
    write_server_files,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_PASSWORD = "web-test-password"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


class Monitor:
    """One `python -m src.main` child; always killed by the fixture."""

    def __init__(self, workdir, settings_path, extra_args=("--no-web",)):
        self.workdir = workdir
        self.args = [sys.executable, "-m", "src.main", "--settings", str(settings_path)]
        self.args += list(extra_args)
        self.process = None
        self.stdout_path = workdir / f"stdout-{id(self)}.txt"

    def start(self):
        env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT), LOG_LEVEL="INFO")
        env.pop("MINECRAFT_MONITOR_SETTINGS", None)
        with open(self.stdout_path, "wb") as stdout:
            self.process = subprocess.Popen(
                self.args,
                cwd=self.workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT,
            )
        return self

    @property
    def log(self):
        path = self.workdir / "monitor.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @property
    def stdout(self):
        return self.stdout_path.read_text(encoding="utf-8", errors="replace")

    def wait_for_log(self, text, timeout=20):
        wait_until(
            lambda: text in self.log or self.process.poll() is not None,
            timeout=timeout,
            message=f"{text!r} not logged",
        )
        assert text in self.log, f"monitor exited early:\n{self.stdout}"

    def terminate(self, timeout=30):
        self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=timeout)

    def kill(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)


@pytest.fixture
def workspace(tmp_path):
    game_port, rcon_port = free_tcp_ports(2)
    server_dir = tmp_path / "server"
    write_server_files(server_dir, game_port, rcon_port)
    settings_path = tmp_path / "settings.yaml"
    settings = {
        "minecraft": {
            "serverDir": str(server_dir),
            "startCommand": dummy_start_command(),
            "pollingRate": 0.5,
            "stopTimeout": 10,
            "processName": DUMMY_PROCESS_NAME,
        },
        "web": {"enabled": False, "password": WEB_PASSWORD},
        "monitor": {"dataDir": str(tmp_path / "data")},
    }
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    monitors = []
    spawned_servers = []

    class Workspace:
        root = tmp_path
        server = server_dir
        settings = settings_path
        game = game_port
        rcon = rcon_port
        servers = spawned_servers

        @staticmethod
        def monitor(settings=settings_path, extra_args=("--no-web",)):
            monitor = Monitor(tmp_path, settings, extra_args)
            monitors.append(monitor)
            return monitor

    yield Workspace
    for monitor in monitors:
        monitor.kill()
    for process in spawned_servers:  # only servers our monitors launched
        if process.is_running() and Path(process.cwd()) == server_dir:
            process.kill()


def test_starts_sleeps_and_exits_cleanly_on_sigterm(workspace):
    monitor = workspace.monitor().start()
    monitor.wait_for_log(f"Sleeping on 127.0.0.1:{workspace.game}")
    monitor.wait_for_log("press Ctrl+C to stop the monitor")  # blocked, waiting
    assert JavaServer("127.0.0.1", workspace.game, timeout=3).status(
        tries=1
    ).version.name == ("Sleeping")

    assert monitor.terminate() == 0
    log = monitor.log
    assert f"Loading settings from {workspace.settings}" in log
    assert f"Game port: {workspace.game} | RCON: port {workspace.rcon}" in log
    assert "Minecraft Monitor stopped" in log
    for secret in (WEB_PASSWORD, DEFAULT_RCON_PASSWORD):
        assert secret not in log
        assert secret not in monitor.stdout
    assert "|  \\/  |" in monitor.stdout  # banner
    assert "Minecraft Monitor is running" in monitor.stdout  # logs go to stdout too
    assert can_bind(workspace.game)


def test_first_run_without_a_terminal_prints_help_and_exits_1(workspace):
    missing = workspace.root / "new" / "settings.yaml"
    monitor = workspace.monitor(settings=missing).start()
    assert monitor.process.wait(timeout=30) == 1
    assert "no terminal available for interactive setup" in monitor.stdout
    assert "settings.yaml.example" in monitor.stdout
    assert not missing.exists()


def test_invalid_settings_exit_1(workspace):
    broken = workspace.root / "broken.yaml"
    broken.write_text(yaml.safe_dump({"web": {"password": "x"}}), encoding="utf-8")
    monitor = workspace.monitor(settings=broken).start()
    assert monitor.process.wait(timeout=30) == 1
    assert "Settings validation failed" in monitor.log
    assert "minecraft.serverDir is required" in monitor.log


def test_join_starts_the_dummy_which_survives_monitor_restarts(workspace):
    first = workspace.monitor().start()
    first.wait_for_log(f"Sleeping on 127.0.0.1:{workspace.game}")

    reply = join_attempt(workspace.game, "Steve")
    assert "starting" in reply["text"]
    first.wait_for_log("Steve (127.0.0.1) is trying to join")

    def dummy_motd():
        try:
            return (
                JavaServer("127.0.0.1", workspace.game, timeout=1).status(tries=1).motd
            )
        except OSError:
            return None

    wait_until(
        lambda: (
            (motd := dummy_motd()) is not None
            and motd.to_plain() == "A Minecraft Server"
        ),
        timeout=30,
        message="the dummy server did not come up",
    )
    servers = [
        p
        for p in psutil.Process(first.process.pid).children(recursive=True)
        if DUMMY_PROCESS_NAME in " ".join(p.cmdline())
    ]
    assert servers, "the monitor did not launch the dummy"
    workspace.servers.extend(servers)

    # Stopping the monitor never stops the Minecraft server.
    assert first.terminate() == 0
    assert all(p.is_running() for p in servers)
    assert dummy_motd() is not None

    # The next monitor adopts it instead of sleeping on the (busy) game port.
    second = workspace.monitor().start()
    second.wait_for_log("Adopted the running Minecraft server process")
    assert second.terminate() == 0
    second_run = second.log.split("Minecraft Monitor stopped")[1]
    assert "Sleeping on" not in second_run  # never touched the busy game port
    assert all(p.is_running() for p in servers)
