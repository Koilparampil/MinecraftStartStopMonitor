"""Process backend contracts against real processes on this host.

A small stdlib fake server (written to tmp_path) behaves like a Minecraft
server: it stops on ``stop`` from stdin, on a stop file (stands in for RCON)
and on SIGTERM (like the JVM shutdown hook), and writes a ``saved-<how>``
marker when it shuts down gracefully. Every process a test starts is killed
in teardown; nothing else is ever signalled.
"""

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

from src.events import EventBus
from src.lifecycle.messages import (
    LaunchRequest,
    LaunchResult,
    LifecycleState,
    ProcessIdentity,
    ProcessObservation,
    RestartCommand,
    StartCommand,
    StopResult,
)
from src.lifecycle.runtime import LifecycleRuntime
from src.lifecycle.supervisor import Supervisor
from src.process_manager import (
    OSProcessManager,
    normalized_dir,
    process_start_key,
    scan_for_server,
)
from test.support import wait_until
from test.support.fakes import FakeBanlistManager, FakeClient, FakePlayerManager

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


def write_launcher(env, after="", redirect=""):
    """A Forge-style ./run.sh: sh runs the server as a child, then `after`."""
    script = os.path.join(env.server_dir, "run.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            f'#!/bin/sh\n"{sys.executable}" "{env.script}" "$@"{redirect}\n'
            f'echo "exited: $?"\n{after}'
        )
    os.chmod(script, 0o755)
    env.settings.startCommand = "./run.sh nogui"
    return script


def wait_ready(env):
    wait_until(lambda: env.marker("ready"), message="fake server never became ready")


def gone(identity):
    try:
        process = psutil.Process(identity.pid)
        return process.status() == psutil.STATUS_ZOMBIE or not identity.matches(
            ProcessIdentity(process.pid, process_start_key(process.pid))
        )
    except psutil.NoSuchProcess:
        return True


def touch_stop_file(env, calls):
    def stopper():
        calls.append(True)
        env.stop_file.write_text("stop", encoding="utf-8")
        return True

    return stopper


def launcher_dies(wrapper):
    """The launcher dies before its server (the OOM killer, an admin...):
    the stop chain itself never signals the launcher."""
    with contextlib.suppress(psutil.NoSuchProcess):
        psutil.Process(wrapper.pid).terminate()
    wait_until(lambda: gone(wrapper), message="the launcher survived SIGTERM")


def wait_helpers_ended(pm, processes):
    """Helpers are ended after their grace by the detection scans."""

    def ended():
        pm.observe()
        return all(gone(ProcessIdentity(p.pid)) for p in processes)

    wait_until(ended, timeout=10, message="a helper kept running")


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

    @POSIX_ONLY
    def test_sigterm_wait_is_at_least_the_stop_timeout(self, env, monkeypatch):
        monkeypatch.setenv("FAKE_IGNORE_STOP", "1")
        monkeypatch.setenv("FAKE_STOP_DELAY", "0.6")  # saving after SIGTERM
        env.settings.stopTimeout = 1
        pm = env.manager()
        pm.TERMINATE_TIMEOUT = 0.2  # a fixed short wait would kill mid-save
        identity = env.launch(pm)
        wait_ready(env)

        assert pm.terminate_process(identity) is True

        assert env.marker("saved-sigterm")

    @POSIX_ONLY
    def test_unattended_stop_never_forces_without_a_graceful_path(self, env):
        process = env.spawn()  # adopted: no console pipe
        wait_ready(env)
        env.settings.stopTimeout = 0.5
        pm = env.manager(graceful_stopper=lambda: False)  # RCON unavailable
        pm.windows = True  # Windows decisions (its terminate is a hard kill)
        pm.TERMINATE_TIMEOUT = 0.5
        found = pm.find_process_identity()
        pm.set_known_identity(found)

        assert pm.terminate_process(found, force=False) is False  # auto-stop
        assert process.poll() is None
        assert pm.tracked_identity == found

        assert pm.terminate_process(found, force=True) is True  # admin stop
        process.wait(5)

    def test_children_of_the_server_are_stopped_too(self, env, monkeypatch):
        monkeypatch.setenv("FAKE_CHILD", "1")
        env.settings.stopTimeout = 1
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)
        parent = psutil.Process(identity.pid)
        wait_until(lambda: parent.children(recursive=True), message="no child")
        children = parent.children(recursive=True)

        # The server saves and exits; its child (a helper that ran with it)
        # is ended once the launch (here the server itself) is over, without
        # holding back the stop.
        assert pm.terminate_process(identity) is True

        assert env.marker("saved-stdin")
        wait_helpers_ended(pm, children)


@POSIX_ONLY
class TestWrapperTree:
    """Forge's run.sh is ``sh`` running ``java`` as a child."""

    @pytest.fixture
    def run_sh(self, env):
        return write_launcher(env)

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
        wait_until(lambda: gone(wrapper))  # the launcher finishes by itself
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
        wait_until(lambda: gone(wrapper))

    def stop_server_by_itself(self, env, child):
        """The server exits on its own (crash, in-game /stop)."""
        child_identity = ProcessIdentity(child.pid, process_start_key(child.pid))
        env.stop_file.write_text("stop", encoding="utf-8")
        wait_until(lambda: gone(child_identity), timeout=15)

    def test_launcher_waiting_for_input_does_not_keep_the_server_running(self, env):
        write_launcher(env, after='echo "Press enter"\nread answer\n')  # run.bat: pause
        pm = env.manager()
        wrapper, child = self.launch_tree(env, pm)
        assert pm.observe().tracked_running is True

        self.stop_server_by_itself(env, child)

        # The first scan without the server closes our stdin pipe: `read`
        # returns and the launcher exits, which is detected as usual.
        wait_until(lambda: not pm.observe().tracked_running, timeout=10)
        wait_until(lambda: gone(wrapper), timeout=10)

    def test_launcher_that_outlives_the_server_counts_as_stopped(self, env):
        write_launcher(env, after="sleep 60\n")  # ignores its input
        pm = env.manager()
        wrapper, child = self.launch_tree(env, pm)
        assert pm.observe().tracked_running is True

        self.stop_server_by_itself(env, child)
        first, second = pm.observe(), pm.observe()

        assert first.tracked_running is True  # one scan could be a hiccup
        assert second.tracked_running is False
        assert second.found is None
        assert not gone(wrapper)  # it lingers, but it is not the server

    def test_rcon_follow_up_when_the_launcher_swallows_stdin(self, env):
        write_launcher(env, redirect=" < /dev/null")  # stdin never reaches it
        env.settings.stopTimeout = 30
        calls = []
        pm = env.manager(graceful_stopper=touch_stop_file(env, calls))
        pm.STDIN_STOP_GRACE = 0.5
        wrapper, _child = self.launch_tree(env, pm)

        started = time.monotonic()
        assert pm.terminate_process(wrapper) is True

        assert time.monotonic() - started < 10  # not the 30 s stopTimeout
        assert calls == [True]
        assert env.marker("saved-rcon")


class TestProcessIdentity:
    @pytest.mark.skipif(sys.platform != "linux", reason="Linux zombie semantics")
    def test_exited_child_is_not_running_before_it_is_reaped(self, env):
        pm = env.manager()
        with subprocess.Popen([sys.executable, "-c", "pass"]) as process:
            # Observe exit without reaping so the backend sees a real zombie.
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
            assert psutil.Process(process.pid).status() == psutil.STATUS_ZOMBIE
            identity = ProcessIdentity(process.pid, process_start_key(process.pid))

            assert pm.identity_is_running(identity) is False

    def test_our_exited_server_is_reaped_and_reported_gone(self, env):
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)
        env.stop_file.write_text("stop", encoding="utf-8")  # it stops by itself

        wait_until(lambda: not pm.identity_is_running(identity), timeout=20)

        # Reported gone only once reaped (it may exit between the checks).
        with pytest.raises(psutil.NoSuchProcess):
            psutil.Process(identity.pid).status()

    def test_a_reused_pid_counts_as_gone_and_is_never_signalled(self, env):
        process = env.spawn()
        wait_ready(env)
        pm = env.manager()
        stale = ProcessIdentity(process.pid, process_start_key(process.pid) - 50)
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
        wait_until(lambda: gone(identity))


@pytest.mark.skipif(sys.platform != "linux", reason="/proc/stat btime")
class TestClockStep:
    """A wall-clock step moves /proc/stat btime, so psutil's create_time();
    the identity must not notice (robustness-1)."""

    @pytest.fixture
    def step_clock(self, monkeypatch):
        boot_time = psutil._pslinux.boot_time

        def step(seconds):
            monkeypatch.setattr(
                psutil._pslinux, "boot_time", lambda: boot_time() + seconds
            )

        return step

    def test_launched_server_survives_a_clock_step(self, env, step_clock):
        pm = env.manager()
        identity = env.launch(pm)
        wait_ready(env)

        step_clock(3600)
        snapshot = pm.observe()

        assert snapshot.tracked_running is True
        assert snapshot.found == identity
        assert pm.terminate_process(identity) is True
        assert env.marker("saved-stdin")  # really asked to stop, not "gone"

    def test_adopted_server_survives_repeated_clock_steps(self, env, step_clock):
        env.spawn()
        wait_ready(env)
        pm = env.manager()
        found = pm.find_process_identity()
        pm.set_known_identity(found)

        for seconds in (5, -30, 3600):
            step_clock(seconds)
            snapshot = pm.observe()
            assert snapshot.tracked_running is True
            assert snapshot.found == found


@POSIX_ONLY
class TestLauncherDiesBeforeItsServer:
    """run.sh (sh) dies at SIGTERM while its JVM child still saves
    (lifecycle-1): the tree is one server until all of it is gone."""

    @pytest.fixture
    def slow_save(self, env, monkeypatch):
        write_launcher(env)
        monkeypatch.setenv("FAKE_IGNORE_STOP", "1")  # escalate to SIGTERM
        monkeypatch.setenv("FAKE_STOP_DELAY", "2.0")  # saving after SIGTERM
        env.settings.stopTimeout = 0.3
        return env

    def launch_tree(self, env, pm):
        wrapper = env.launch(pm)
        wait_ready(env)
        root = psutil.Process(wrapper.pid)
        wait_until(lambda: root.children(recursive=True), message="no server child")
        [child] = root.children(recursive=True)
        env.own(child.pid)
        return wrapper, child

    def test_detection_never_reports_the_saving_child_as_another_server(
        self, slow_save
    ):
        env = slow_save
        pm = env.manager()
        wrapper, child = self.launch_tree(env, pm)
        pm.observe()  # remembers the child, as the detection worker does
        result = []
        stopper = threading.Thread(target=lambda: result.append(pm.terminate_process()))
        stopper.start()

        launcher_dies(wrapper)
        seen = []
        while stopper.is_alive():
            snapshot = pm.observe()
            seen.append(snapshot)
            assert snapshot.found in (None, wrapper)
            assert snapshot.tracked_running or gone(ProcessIdentity(child.pid))
            time.sleep(0.1)
        stopper.join()

        assert any(s.tracked_running and gone(wrapper) for s in seen)
        assert result == [True]
        assert env.marker("saved-sigterm")
        assert pm.tracked_identity is None

    def test_restart_waits_for_the_child_then_launches_again(self, slow_save):
        env = slow_save
        pm = env.manager()
        bus = EventBus()
        supervisor = Supervisor(
            pm, FakePlayerManager(bus), FakeBanlistManager(bus), bus
        )
        supervisor.server_stopping_cooldown = 0
        [launch_request] = supervisor.handle(StartCommand())
        wrapper, child = self.launch_tree(env, pm)
        supervisor.handle(LaunchResult(launch_request.generation, wrapper))
        supervisor.handle(ProcessObservation(supervisor.generation, pm.observe()))
        assert supervisor.state == LifecycleState.RUNNING

        [stop_request] = supervisor.handle(RestartCommand())
        result = []
        stopper = threading.Thread(
            target=lambda: result.append(pm.terminate_process(stop_request.identity))
        )
        stopper.start()
        launcher_dies(wrapper)
        relaunch = None
        while stopper.is_alive() and relaunch is None:  # the detection worker
            requests = supervisor.handle(
                ProcessObservation(supervisor.generation, pm.observe())
            )
            if requests:
                # Detection saw the whole server gone before the stop returned:
                # only once the saving child had exited.
                [relaunch] = requests
                assert gone(ProcessIdentity(child.pid))
                assert env.marker("saved-sigterm")
            else:
                assert supervisor.state == LifecycleState.STOPPING
                assert supervisor.restart_pending is True
                time.sleep(0.1)
        stopper.join()

        late = supervisor.handle(
            StopResult(stop_request.generation, stop_request.identity, result[0])
        )
        if relaunch is None:
            [relaunch] = late
        else:
            assert late == []  # the stop result of the old generation is stale
        assert isinstance(relaunch, LaunchRequest)
        assert supervisor.state == LifecycleState.STARTING
        assert env.marker("saved-sigterm") and gone(ProcessIdentity(child.pid))


@POSIX_ONLY
class TestBackgroundHelper:
    """run.sh starts a helper in the background (a tunnel, a backup loop)
    before the server; it never counts as the server (R2, R14)."""

    @pytest.fixture
    def helper_env(self, env):
        script = os.path.join(env.server_dir, "run.sh")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nsleep 600 &\n"{sys.executable}" "{env.script}" "$@"\n')
        os.chmod(script, 0o755)
        env.settings.startCommand = "./run.sh nogui"
        return env

    def launch(self, env, pm):
        wrapper = env.launch(pm)
        wait_ready(env)
        root = psutil.Process(wrapper.pid)
        wait_until(lambda: len(root.children(recursive=True)) == 2)
        processes = root.children(recursive=True)
        for process in processes:
            env.own(process.pid)
        [server] = [p for p in processes if PROCESS_NAME in " ".join(p.cmdline())]
        [helper] = [p for p in processes if p is not server]
        pm.observe()  # remembers both, as the detection worker does
        return wrapper, server, helper

    @staticmethod
    def scans_after_exit(pm, wrapper, server):
        wait_until(lambda: gone(ProcessIdentity(server.pid)) and gone(wrapper))
        return [pm.observe().tracked_running for _ in range(3)]

    def test_a_crash_is_detected_while_the_helper_lives(self, helper_env):
        pm = helper_env.manager()
        wrapper, server, helper = self.launch(helper_env, pm)

        server.kill()

        assert self.scans_after_exit(pm, wrapper, server) == [True, False, False]
        assert helper.is_running()
        assert pm.identity_is_running(wrapper) is False  # no "already running"

    def test_an_in_game_stop_is_detected_while_the_helper_lives(self, helper_env):
        pm = helper_env.manager()
        wrapper, server, helper = self.launch(helper_env, pm)

        helper_env.stop_file.write_text("stop", encoding="utf-8")  # /stop

        assert self.scans_after_exit(pm, wrapper, server) == [True, False, False]
        assert helper_env.marker("saved-rcon")

    def test_a_dashboard_stop_does_not_wait_for_the_helper(self, helper_env):
        helper_env.settings.stopTimeout = 60
        pm = helper_env.manager()
        wrapper, server, helper = self.launch(helper_env, pm)

        started = time.monotonic()
        assert pm.terminate_process(wrapper) is True

        assert time.monotonic() - started < 10  # not the 60 s stopTimeout
        assert helper_env.marker("saved-stdin")
        assert pm.tracked_identity is None
        assert helper.is_running()  # ended by a later scan, once run.sh exited
        wait_helpers_ended(pm, [helper])

    def test_a_crash_ends_the_helper_once_the_launcher_exited(self, helper_env):
        pm = helper_env.manager()
        wrapper, server, helper = self.launch(helper_env, pm)

        server.kill()
        assert self.scans_after_exit(pm, wrapper, server)[-1] is False
        assert pm.clear_identity(wrapper)  # the supervisor commits the exit

        assert helper.is_running()
        wait_helpers_ended(pm, [helper])


@POSIX_ONLY
class TestPostStopWork:
    """run.sh does work after the server exits (a backup): a monitor stop
    lets it finish, and a restart waits for it before relaunching (S1)."""

    @pytest.fixture
    def post_stop(self, env):
        env.done = env.markers / "post-stop-done"
        write_launcher(env, after=f'sleep 3\ntouch "{env.done}"\n')
        return env

    @staticmethod
    def launch_tree(env, pm):
        wrapper = env.launch(pm)
        wait_ready(env)
        root = psutil.Process(wrapper.pid)
        wait_until(lambda: root.children(recursive=True), message="no server child")
        for child in root.children(recursive=True):
            env.own(child.pid)
        pm.observe()
        return wrapper

    @pytest.mark.parametrize("force", [True, False], ids=["stop", "auto-stop"])
    def test_a_stop_lets_the_launch_script_finish(self, post_stop, force):
        env = post_stop
        pm = env.manager()
        wrapper = self.launch_tree(env, pm)

        assert pm.terminate_process(wrapper, force=force) is True

        assert env.marker("saved-stdin")
        assert not env.done.exists()  # STOPPED is not held back by the script
        wait_until(env.done.exists, timeout=10, message="post-stop work cut off")
        wait_until(lambda: gone(wrapper))

    def test_a_restart_relaunches_only_after_the_launch_script(self, post_stop):
        env = post_stop
        pm = env.manager()
        bus = EventBus()
        supervisor = Supervisor(
            pm, FakePlayerManager(bus), FakeBanlistManager(bus), bus
        )
        supervisor.server_stopping_cooldown = 0
        [launch] = supervisor.handle(StartCommand())
        wrapper = self.launch_tree(env, pm)
        supervisor.handle(LaunchResult(launch.generation, wrapper))

        [stop] = supervisor.handle(RestartCommand())
        stopped = pm.terminate_process(stop.identity)
        [relaunch] = supervisor.handle(
            StopResult(stop.generation, stop.identity, stopped)
        )
        assert isinstance(relaunch, LaunchRequest)
        assert not env.done.exists()

        second = pm.launch_process()  # the process worker's launch
        assert second is not None
        env.own(second.pid)
        assert env.done.exists()  # the backup finished before the relaunch
        assert second != wrapper


# ----------------------------------------------------------------------
# What a launch leaves behind (FU-6) and shutting down meanwhile (FU-7)
# ----------------------------------------------------------------------

TUNNEL = textwrap.dedent(
    """
    import os, sys, time
    open(os.path.join(sys.argv[1], "tunnel-%d" % os.getpid()), "w").close()
    time.sleep(600)
    """
)


def write_script(env, body, shell="/bin/sh"):
    """./run.sh with *body*; {py}, {server}, {markers} are filled in."""
    script = os.path.join(env.server_dir, "run.sh")
    text = body.format(py=sys.executable, server=env.script, markers=env.markers)
    with open(script, "w", encoding="utf-8") as f:
        f.write(f"#!{shell}\n{text}")
    os.chmod(script, 0o755)
    env.settings.startCommand = "./run.sh nogui"
    return script


def own_tree(env, identity):
    root = psutil.Process(identity.pid)
    for child in root.children(recursive=True):
        env.own(child.pid)


def stop_with_scans(env, pm, identity, force=True):
    """terminate_process() as the process worker runs it, while the
    detection worker keeps scanning (scans run the cleanup)."""
    result = []
    stopper = threading.Thread(
        target=lambda: result.append(pm.terminate_process(identity, force=force))
    )
    stopper.start()
    while stopper.is_alive():
        pm.observe()
        time.sleep(0.05)
    stopper.join()
    return result == [True]


def scan_until(pm, predicate, timeout=15, message="condition not met"):
    def check():
        pm.observe()
        return predicate()

    wait_until(check, timeout=timeout, message=message)


@POSIX_ONLY
class TestPipelineLaunchScript:
    """run.sh pipes the server and its post-stop work through ``cat`` (or
    ``tee``): the pipeline existed while the server ran, so it used to count
    as background helpers and was ended after min(stopTimeout, 30 s), cutting
    the backup short (S1). Nothing is ended while run.sh runs."""

    @pytest.fixture
    def pipeline(self, env):
        env.done = env.markers / "post-stop-done"
        write_script(
            env,
            '( "{py}" "{server}" "$@"; sleep 3; touch "{markers}/post-stop-done" )'
            " | cat\n",
        )
        return env

    def launch(self, env, pm):
        identity = env.launch(pm)
        wait_ready(env)
        root = psutil.Process(identity.pid)
        wait_until(lambda: len(root.children(recursive=True)) >= 3)
        own_tree(env, identity)
        pm.observe()  # remembers the tree, as the detection worker does
        return identity

    @pytest.mark.parametrize("force", [True, False], ids=["stop", "auto-stop"])
    def test_a_stop_lets_the_pipeline_finish(self, pipeline, force):
        env = pipeline
        env.settings.stopTimeout = 1  # the old cleanup grace: min(stopTimeout, 30)
        pm = env.manager()
        launcher = self.launch(env, pm)

        assert stop_with_scans(env, pm, launcher, force=force)

        assert env.marker("saved-stdin")
        assert not env.done.exists()  # STOPPED is not held back by the script
        scan_until(pm, env.done.exists, message="post-stop work was cut short")
        scan_until(pm, lambda: gone(launcher))
        assert not os.path.exists(pm.leftovers_file)  # nothing left to end

    def test_a_restart_relaunches_only_after_the_pipeline(self, pipeline):
        env = pipeline
        env.settings.stopTimeout = 10  # longer than the 3 s of post-stop work
        pm = env.manager()
        bus = EventBus()
        supervisor = Supervisor(
            pm, FakePlayerManager(bus), FakeBanlistManager(bus), bus
        )
        supervisor.server_stopping_cooldown = 0
        [launch] = supervisor.handle(StartCommand())
        first = self.launch(env, pm)
        supervisor.handle(LaunchResult(launch.generation, first))

        [stop] = supervisor.handle(RestartCommand())
        stopped = stop_with_scans(env, pm, stop.identity)
        [relaunch] = supervisor.handle(
            StopResult(stop.generation, stop.identity, stopped)
        )
        assert isinstance(relaunch, LaunchRequest)
        assert not env.done.exists()

        second = pm.launch_process()  # the process worker's launch
        assert second is not None
        env.own(second.pid)
        assert env.done.exists()  # the backup finished before the relaunch
        assert gone(first)


@POSIX_ONLY
@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
class TestProcessSubstitutionLaunchScript:
    def test_post_stop_steps_through_tee_all_complete(self, env):
        """``exec > >(tee -a run.log)``: tee ran with the server; the 40
        backup steps after it print through tee and must all run (the old
        cleanup ended tee and the script died on SIGPIPE)."""
        env.settings.stopTimeout = 1  # the old cleanup grace: 1 s
        write_script(
            env,
            "exec > >(tee -a run.log) 2>&1\n"
            '"{py}" "{server}" "$@"\n'
            'for step in $(seq 1 40); do echo "backup step $step"; sleep 0.1; done\n'
            'touch "{markers}/backup-done"\n',
            shell=shutil.which("bash"),
        )
        pm = env.manager()
        launcher = env.launch(pm)
        wait_ready(env)
        wait_until(lambda: psutil.Process(launcher.pid).children(recursive=True))
        own_tree(env, launcher)
        pm.observe()

        assert stop_with_scans(env, pm, launcher)

        scan_until(pm, lambda: env.marker("backup-done"), message="steps cut short")
        log = open(os.path.join(env.server_dir, "run.log"), encoding="utf-8").read()
        assert "backup step 40" in log


@POSIX_ONLY
class TestLeftoverHelpers:
    """run.sh starts a tunnel in the background, reusing one that is still
    running ("only if it isn't running")."""

    @pytest.fixture
    def tunnel_env(self, env):
        tunnel = env.script.parent / "tunnel.py"
        tunnel.write_text(TUNNEL, encoding="utf-8")
        pid_file = env.markers / "tunnel.pid"
        env.tunnel_pid_file = pid_file
        env.post_stop = ""

        def script(post_stop=""):
            # ps, not kill -0: an orphaned helper that has exited can stay a
            # zombie where PID 1 does not reap (some containers).
            write_script(
                env,
                f'state=$(ps -o stat= -p "$(cat "{pid_file}" 2>/dev/null)" 2>/dev/null)\n'
                'case "$state" in\n'
                f'  ""|Z*) "{{py}}" "{tunnel}" "{{markers}}" &\n'
                f'         echo $! > "{pid_file}" ;;\n'
                "esac\n"
                '"{py}" "{server}" "$@"\n' + post_stop,
            )

        env.write_run_sh = script
        yield env
        for marker in env.markers.glob("tunnel-*"):  # orphans after their run.sh
            with contextlib.suppress(psutil.Error, ValueError):
                psutil.Process(int(marker.name.split("-")[1])).kill()

    @staticmethod
    def live_tunnels(env):
        alive = []
        for marker in env.markers.glob("tunnel-*"):
            identity = ProcessIdentity(int(marker.name.split("-")[1]))
            if not gone(identity):
                alive.append(identity.pid)
        return alive

    def launch(self, env, pm):
        identity = env.launch(pm)
        wait_ready(env)
        wait_until(lambda: self.live_tunnels(env), message="no tunnel")
        own_tree(env, identity)
        pm.observe()  # sees the tunnel next to the running server
        return identity

    def test_the_next_launch_keeps_its_helper(self, tunnel_env):
        """A player rejoins right after a stop: the old tunnel is ended before
        the relaunch, so run.sh starts a new one, and no cleanup of the old
        server ends the one the new server uses."""
        env = tunnel_env
        env.settings.stopTimeout = 1  # the old cleanup grace: 1 s
        env.write_run_sh()
        pm = env.manager()
        first = self.launch(env, pm)
        [old_tunnel] = self.live_tunnels(env)

        assert pm.terminate_process(first) is True
        second = pm.launch_process()  # the rejoin, within the old grace
        assert second is not None
        env.own(second.pid)
        wait_until(lambda: env.marker("ready"))
        wait_until(lambda: self.live_tunnels(env) != [old_tunnel])

        for _ in range(30):  # 1.5 s of scans: past the old 1 s grace
            pm.observe()
            time.sleep(0.05)
        [new_tunnel] = self.live_tunnels(env)
        assert new_tunnel != old_tunnel
        assert int(env.tunnel_pid_file.read_text()) == new_tunnel

    def test_close_ends_helpers_once_the_launcher_exited(self, tunnel_env):
        env = tunnel_env
        env.write_run_sh()
        pm = env.manager()
        launcher = self.launch(env, pm)

        assert pm.terminate_process(launcher) is True
        wait_until(lambda: gone(launcher))
        pm.close()  # the monitor stops right after the server

        wait_until(lambda: not self.live_tunnels(env), message="tunnel left running")
        assert not os.path.exists(pm.leftovers_file)

    def test_the_next_monitor_finishes_the_cleanup(self, tunnel_env, caplog):
        """The monitor stops while run.sh still does post-stop work: the
        tunnel is saved for later, and the next monitor ends it before it
        launches, so there is never a second copy."""
        env = tunnel_env
        env.settings.stopTimeout = 10
        env.write_run_sh("sleep 2\n")
        first_monitor = env.manager()
        launcher = self.launch(env, first_monitor)
        [old_tunnel] = self.live_tunnels(env)

        assert first_monitor.terminate_process(launcher) is True
        first_monitor.close()
        saved = json.loads(open(first_monitor.leftovers_file).read())
        [launch] = saved["launches"]
        assert launch["launcher"][0] == launcher.pid
        assert [pid for pid, _key in launch["helpers"]] == [old_tunnel]
        assert self.live_tunnels(env) == [old_tunnel]  # run.sh still runs

        next_monitor = env.manager()
        with caplog.at_level(logging.INFO, logger="src.process_manager"):
            second = next_monitor.launch_process()
        assert second is not None
        env.own(second.pid)
        assert "Waiting up to" in caplog.text  # for the old run.sh first
        assert gone(launcher)
        wait_until(
            lambda: self.live_tunnels(env) and old_tunnel not in self.live_tunnels(env)
        )
        assert len(self.live_tunnels(env)) == 1


@POSIX_ONLY
class TestShutdownWhileWaitingForTheLaunchScript:
    def test_close_skips_the_relaunch(self, env, caplog):
        """FU-7: a Restart waits for run.sh's 10 s of post-stop work; the
        monitor is stopped 1 s into the wait. close() returns at once and no
        second server is started."""
        env.done = env.markers / "post-stop-done"
        write_launcher(env, after=f'sleep 10\ntouch "{env.done}"\n')
        env.settings.stopTimeout = 30
        env.settings.pollingRate = 0.05
        env.settings.autoStop = False
        pm = env.manager()
        launches = []
        real_launch = pm.launch_process

        def launch_process():
            identity = real_launch()
            launches.append(identity)
            if identity is not None:
                env.own(identity.pid)
            return identity

        pm.launch_process = launch_process
        bus = EventBus()
        supervisor = Supervisor(
            pm, FakePlayerManager(bus), FakeBanlistManager(bus), bus
        )
        supervisor.server_starting_cooldown = 0
        supervisor.server_stopping_cooldown = 0
        runtime = LifecycleRuntime(supervisor, FakeClient(), pm)
        runtime.start()
        try:
            assert runtime.submit_command(StartCommand()).success
            wait_until(lambda: supervisor.state == LifecycleState.RUNNING)
            wait_ready(env)
            own_tree(env, launches[0])
            with caplog.at_level(logging.INFO, logger="src.process_manager"):
                assert runtime.submit_command(RestartCommand()).success
                wait_until(lambda: "Waiting up to" in caplog.text, timeout=15)
                time.sleep(1)

                started = time.monotonic()
                runtime.close()
                elapsed = time.monotonic() - started
        finally:
            runtime.close()

        assert elapsed < 2
        assert "Shutting down: not starting the server" in caplog.text
        assert len(launches) == 2 and launches[1] is None
        assert scan_for_server(normalized_dir(env.server_dir), PROCESS_NAME) is None
        assert not env.done.exists()  # run.sh was left alone
