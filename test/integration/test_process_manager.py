"""Process backend contracts against real processes on this host.

A small stdlib fake server (written to tmp_path) behaves like a Minecraft
server: it stops on ``stop`` from stdin, on a stop file (stands in for RCON)
and on SIGTERM (like the JVM shutdown hook), and writes a ``saved-<how>``
marker when it shuts down gracefully. Every process a test starts is killed
in teardown; nothing else is ever signalled.
"""

import contextlib
import os
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import psutil
import pytest

from src.lifecycle.messages import ProcessIdentity
from src.process_manager import OSProcessManager
from test.support import wait_until

PROCESS_NAME = "fake_mc_server"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX signals, sh wrapper")

FAKE_SERVER = textwrap.dedent(
    """
    import os, signal, subprocess, sys, threading, time

    markers = os.environ["FAKE_MARKERS"]
    ignore_stop = os.environ.get("FAKE_IGNORE_STOP") == "1"
    ignore_term = os.environ.get("FAKE_IGNORE_TERM") == "1"
    stop_file = os.environ.get("FAKE_STOP_FILE")

    def mark(name):
        with open(os.path.join(markers, name), "w") as f:
            f.write(str(os.getpid()))

    def shutdown(how):
        print("Stopping server", flush=True)
        print("Saving worlds", flush=True)
        time.sleep(float(os.environ.get("FAKE_STOP_DELAY", "0.2")))
        mark("saved-" + how)
        os._exit(0)

    def on_term(signum, frame):
        if not ignore_term:
            shutdown("sigterm")

    def console():
        for line in sys.stdin:
            if line.strip() == "stop" and not ignore_stop:
                shutdown("stdin")
        # EOF on the console: keep running, like Minecraft.

    signal.signal(signal.SIGTERM, on_term)
    threading.Thread(target=console, daemon=True).start()
    if os.environ.get("FAKE_CHILD") == "1":
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print('Done (0.1s)! For help, type "help"', flush=True)
    mark("ready")
    while True:
        if stop_file and os.path.exists(stop_file) and not ignore_stop:
            shutdown("rcon")
        time.sleep(0.05)
    """
)


@pytest.fixture
def env(mock_settings, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    markers = tmp_path / "markers"
    bin_dir.mkdir()
    markers.mkdir()
    script = bin_dir / f"{PROCESS_NAME}.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    server_dir = mock_settings.serverDir
    stop_file = tmp_path / "rcon-stop.flag"

    mock_settings.processName = PROCESS_NAME
    mock_settings.startCommand = [sys.executable, str(script)]
    mock_settings.stopTimeout = 5
    monkeypatch.setenv("FAKE_MARKERS", str(markers))
    monkeypatch.setenv("FAKE_STOP_FILE", str(stop_file))
    for name in (
        "FAKE_IGNORE_STOP",
        "FAKE_IGNORE_TERM",
        "FAKE_CHILD",
        "FAKE_STOP_DELAY",
    ):
        monkeypatch.delenv(name, raising=False)

    owned = []  # psutil.Process objects this test started (plus descendants)
    managers = []

    def manager(graceful_stopper=None):
        pm = OSProcessManager(graceful_stopper=graceful_stopper)
        pm.TERMINATE_TIMEOUT = 3.0
        pm.KILL_TIMEOUT = 5.0
        pm.WAIT_INTERVAL = 0.05
        managers.append(pm)
        return pm

    def own(pid):
        with contextlib.suppress(psutil.Error):
            owned.append(psutil.Process(pid))

    def launch(pm):
        identity = pm.launch_process()
        assert identity is not None
        own(identity.pid)
        return identity

    def spawn(cwd=server_dir, extra_env=None):
        """Start a fake server ourselves (an 'adopted' server)."""
        process = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            env={**os.environ, **(extra_env or {})},
            **({} if os.name == "nt" else {"start_new_session": True}),
        )
        own(process.pid)
        return process

    def marker(name):
        return (markers / name).exists()

    yield SimpleNamespace(
        settings=mock_settings,
        server_dir=server_dir,
        script=script,
        stop_file=stop_file,
        manager=manager,
        launch=launch,
        spawn=spawn,
        own=own,
        marker=marker,
        markers=markers,
    )

    for pm in managers:
        pm.close()
    for process in list(owned):
        with contextlib.suppress(psutil.Error):
            owned.extend(process.children(recursive=True))
    for process in owned:
        with contextlib.suppress(psutil.Error):
            process.kill()
    psutil.wait_procs(owned, timeout=5)


def wait_ready(env):
    wait_until(lambda: env.marker("ready"), message="fake server never became ready")


def gone(identity):
    try:
        process = psutil.Process(identity.pid)
        return process.status() == psutil.STATUS_ZOMBIE or not identity.matches(
            ProcessIdentity(process.pid, process.create_time())
        )
    except psutil.NoSuchProcess:
        return True


def touch_stop_file(env, calls):
    def stopper():
        calls.append(True)
        env.stop_file.write_text("stop", encoding="utf-8")
        return True

    return stopper


class TestLaunchAndDiscovery:
    def test_launch_runs_in_the_server_dir_with_a_console_log(self, env):
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)

        process = psutil.Process(identity.pid)
        assert os.path.samefile(process.cwd(), env.server_dir)
        pid_file = env.settings.data_path("server.pid")
        pid_text, created_text = open(pid_file, encoding="utf-8").read().split()
        assert int(pid_text) == identity.pid
        assert float(created_text) == pytest.approx(identity.create_time)

        log = open(
            env.settings.data_path("server-console.log"), encoding="utf-8"
        ).read()
        assert log.startswith("[minecraft-monitor ")
        wait_until(
            lambda: (
                "Done (" in open(env.settings.data_path("server-console.log")).read()
            )
        )

        assert pm.find_process_identity() == identity
        snapshot = pm.observe()
        assert snapshot.tracked == identity
        assert snapshot.tracked_running is True
        assert snapshot.found == identity

    def test_console_log_is_truncated_per_launch(self, env):
        log_path = env.settings.data_path("server-console.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("old run\n" * 100)
        pm = env.manager()

        env.launch(pm)
        wait_ready(env)

        assert "old run" not in open(log_path, encoding="utf-8").read()

    def test_discovery_requires_the_server_directory(self, env, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        stranger = env.spawn(cwd=str(elsewhere))
        pm = env.manager()
        wait_ready(env)

        assert pm.find_process_identity() is None

        ours = env.spawn()
        wait_until(lambda: pm.find_process_identity() is not None)
        found = pm.find_process_identity()
        assert found.pid == ours.pid
        assert found.pid != stranger.pid

    def test_discovery_never_returns_the_monitor_process(self, env):
        env.settings.serverDir = os.getcwd()
        env.settings.processName = "python"

        found = env.manager().find_process_identity()

        assert found is None or found.pid != os.getpid()

    def test_close_never_stops_the_server(self, env):
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)

        pm.close()
        time.sleep(0.3)

        assert not gone(identity)
        assert env.manager().tracked_identity == identity  # adopted next time


class TestGracefulStop:
    def test_stop_command_on_stdin(self, env):
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)

        assert pm.terminate_process(identity) is True

        assert env.marker("saved-stdin")
        assert gone(identity)
        assert pm.tracked_identity is None
        assert not os.path.exists(env.settings.data_path("server.pid"))

    def test_adopted_server_is_stopped_through_the_graceful_stopper(self, env):
        process = env.spawn()
        wait_ready(env)
        calls = []
        pm = env.manager(graceful_stopper=touch_stop_file(env, calls))
        found = pm.find_process_identity()
        assert found is not None and found.pid == process.pid

        pm.set_known_identity(found)
        assert pm.terminate_process(found) is True

        assert calls == [True]
        assert env.marker("saved-rcon")
        process.wait(5)
        assert pm.tracked_identity is None

    @POSIX_ONLY
    def test_sigterm_when_the_server_ignores_stop(self, env, monkeypatch):
        monkeypatch.setenv("FAKE_IGNORE_STOP", "1")
        env.settings.stopTimeout = 0.5
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)

        assert pm.terminate_process(identity) is True

        assert env.marker("saved-sigterm")  # the JVM-style shutdown hook ran
        assert gone(identity)

    @POSIX_ONLY
    def test_sigterm_right_away_without_a_graceful_path(self, env):
        process = env.spawn()
        wait_ready(env)
        env.settings.stopTimeout = 30
        pm = env.manager(graceful_stopper=lambda: False)  # RCON unreachable
        found = pm.find_process_identity()
        pm.set_known_identity(found)

        started = time.monotonic()
        assert pm.terminate_process(found) is True

        assert time.monotonic() - started < 10  # no pointless stopTimeout wait
        assert env.marker("saved-sigterm")
        process.wait(5)

    @POSIX_ONLY
    def test_kill_when_sigterm_is_ignored(self, env, monkeypatch):
        monkeypatch.setenv("FAKE_IGNORE_STOP", "1")
        monkeypatch.setenv("FAKE_IGNORE_TERM", "1")
        env.settings.stopTimeout = 0.3
        pm = env.manager()
        pm.TERMINATE_TIMEOUT = 0.3
        identity = env.launch(pm)
        wait_ready(env)

        assert pm.terminate_process(identity) is True

        assert gone(identity)
        assert not any(name.startswith("saved-") for name in os.listdir(env.markers))
        assert pm.tracked_identity is None

    def test_children_of_the_server_are_stopped_too(self, env, monkeypatch):
        monkeypatch.setenv("FAKE_CHILD", "1")
        env.settings.stopTimeout = 1
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)
        parent = psutil.Process(identity.pid)
        wait_until(lambda: parent.children(recursive=True), message="no child")
        children = parent.children(recursive=True)

        # The server saves and exits; its child outlives it and still holds
        # resources, so the stop is complete only once the child is gone too.
        assert pm.terminate_process(identity) is True

        assert env.marker("saved-stdin")
        assert all(gone(ProcessIdentity(c.pid, c.create_time())) for c in children)


@POSIX_ONLY
class TestWrapperTree:
    """Forge's run.sh is ``sh`` running ``java`` as a child."""

    @pytest.fixture
    def run_sh(self, env):
        script = os.path.join(env.server_dir, "run.sh")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                f'#!/bin/sh\n"{sys.executable}" "{env.script}" "$@"\necho "exited: $?"\n'
            )
        os.chmod(script, 0o755)
        env.settings.startCommand = "./run.sh nogui"
        return script

    def launch_tree(self, env, pm):
        wrapper = env.launch(pm)
        wait_ready(env)
        process = psutil.Process(wrapper.pid)
        wait_until(lambda: process.children(recursive=True), message="no java child")
        [child] = process.children(recursive=True)
        env.own(child.pid)
        return wrapper, child

    def test_wrapper_is_tracked_and_found_as_the_server(self, env, run_sh):
        pm = env.manager()
        wrapper, child = self.launch_tree(env, pm)

        assert PROCESS_NAME not in " ".join(psutil.Process(wrapper.pid).cmdline())
        assert pm.find_process_identity() == wrapper
        assert pm.observe().found == wrapper

    def test_stop_reaches_the_child_and_waits_for_the_whole_tree(self, env, run_sh):
        pm = env.manager()
        wrapper, child = self.launch_tree(env, pm)

        assert pm.terminate_process(wrapper) is True

        assert env.marker("saved-stdin")  # the child read our stdin pipe
        assert gone(wrapper)
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        log = open(
            env.settings.data_path("server-console.log"), encoding="utf-8"
        ).read()
        assert "exited: 0" in log

    def test_wrapper_is_adopted_after_a_monitor_restart(self, env, run_sh):
        first = env.manager()
        wrapper, child = self.launch_tree(env, first)
        first.close()  # the monitor exits; the server keeps running

        calls = []
        second = env.manager(graceful_stopper=touch_stop_file(env, calls))
        assert second.tracked_identity == wrapper
        snapshot = second.observe()
        assert snapshot.tracked_running is True
        assert snapshot.found == wrapper  # the supervisor adopts the tracked tree

        assert second.terminate_process(wrapper) is True

        assert calls == [True]  # no console pipe any more: RCON stop
        assert env.marker("saved-rcon")
        assert gone(wrapper)


class TestProcessIdentity:
    @pytest.mark.skipif(sys.platform != "linux", reason="Linux zombie semantics")
    def test_exited_child_is_not_running_before_it_is_reaped(self, env):
        pm = env.manager()
        with subprocess.Popen([sys.executable, "-c", "pass"]) as process:
            # Observe exit without reaping so the backend sees a real zombie.
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
            assert psutil.Process(process.pid).status() == psutil.STATUS_ZOMBIE
            identity = ProcessIdentity(
                process.pid, psutil.Process(process.pid).create_time()
            )

            assert pm.identity_is_running(identity) is False

    def test_our_exited_server_is_reaped_and_reported_gone(self, env):
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)
        env.stop_file.write_text("stop", encoding="utf-8")  # it stops by itself

        wait_until(lambda: not pm.identity_is_running(identity))

        with pytest.raises(psutil.NoSuchProcess):
            psutil.Process(identity.pid).status()  # reaped, not a zombie

    def test_a_reused_pid_counts_as_gone_and_is_never_signalled(self, env):
        process = env.spawn()
        wait_ready(env)
        pm = env.manager()
        stale = ProcessIdentity(
            process.pid, psutil.Process(process.pid).create_time() - 50
        )
        pm.set_known_identity(stale)

        assert pm.identity_is_running(stale) is False
        assert pm.terminate_process(stale) is True

        assert pm.tracked_identity is None
        assert process.poll() is None  # the unrelated process is untouched
        assert not any(name.startswith("saved-") for name in os.listdir(env.markers))


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="cmd.exe batch wrapper")
class TestWindowsBatchWrapper:
    def test_run_bat_with_pause_stops_through_stdin(self, env):
        batch = os.path.join(env.server_dir, "run.bat")
        with open(batch, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{env.script}" %*\r\npause\r\n')
        env.settings.startCommand = "run.bat nogui"
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)
        assert pm.find_process_identity() == identity

        # "stop" reaches python; once only cmd.exe (waiting at pause) is left,
        # the pipe is closed so pause returns and the tree exits in time.
        assert pm.terminate_process(identity) is True
        assert env.marker("saved-stdin")
        assert gone(identity)
