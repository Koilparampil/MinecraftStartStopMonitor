"""Unit tests for the process backend: command building, discovery ranking and
identity bookkeeping. No server process is started here (see
test/integration/test_process_manager.py for real processes)."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest

from src.lifecycle.messages import ProcessIdentity
from src.process_manager import (
    OSProcessManager,
    PosixProcessManager,
    WindowsProcessManager,
    build_launch_command,
    create_process_manager,
    match_rank,
    normalized_dir,
    popen_platform_kwargs,
    scan_for_server,
)

CMD = r"C:\Windows\system32\cmd.exe"


@pytest.fixture
def manager(mock_settings):
    return OSProcessManager()


class TestBuildLaunchCommand:
    def test_posix_string_is_split_with_shlex(self):
        command = 'java -Xmx4G -jar "my server.jar" nogui'

        assert build_launch_command(command, windows=False) == [
            "java",
            "-Xmx4G",
            "-jar",
            "my server.jar",
            "nogui",
        ]

    def test_list_is_used_as_is(self):
        assert build_launch_command(["./run.sh", "nogui"], windows=False) == [
            "./run.sh",
            "nogui",
        ]
        assert build_launch_command(("java", "-jar", "s.jar"), windows=True) == [
            "java",
            "-jar",
            "s.jar",
        ]

    def test_windows_string_is_passed_through(self):
        command = r'java -Xmx4G -jar "C:\Servers\My Server\server.jar" nogui'

        assert build_launch_command(command, windows=True) == command

    def test_windows_batch_files_run_through_cmd(self, monkeypatch):
        monkeypatch.setenv("COMSPEC", CMD)

        assert build_launch_command("run.bat nogui", windows=True) == (
            f'"{CMD}" /d /s /c "run.bat nogui"'
        )
        assert build_launch_command(
            r'"C:\My Server\start.CMD" nogui', windows=True
        ) == (f'"{CMD}" /d /s /c ""C:\\My Server\\start.CMD" nogui"')
        assert build_launch_command(["run.bat", "nogui"], windows=True) == (
            f'"{CMD}" /d /s /c "run.bat nogui"'
        )

    def test_batch_name_is_not_special_on_posix(self):
        assert build_launch_command("./run.bat nogui", windows=False) == [
            "./run.bat",
            "nogui",
        ]

    @pytest.mark.parametrize("command", [None, "", "   ", [], [""], 42])
    def test_empty_command_is_rejected(self, command):
        with pytest.raises(ValueError):
            build_launch_command(command, windows=False)

    def test_unbalanced_quotes_are_rejected_on_posix(self):
        with pytest.raises(ValueError):
            build_launch_command('java -jar "server.jar', windows=False)


class TestPlatformKwargs:
    def test_posix_server_gets_its_own_session(self):
        assert popen_platform_kwargs(windows=False) == {"start_new_session": True}

    def test_windows_server_gets_its_own_group_and_no_window(self):
        flags = popen_platform_kwargs(windows=True)["creationflags"]

        assert flags & 0x00000200  # CREATE_NEW_PROCESS_GROUP
        assert flags & 0x08000000  # CREATE_NO_WINDOW

    def test_create_process_manager_picks_the_host_backend(self):
        def stopper():
            return True

        manager = create_process_manager(graceful_stopper=stopper)

        expected = WindowsProcessManager if os.name == "nt" else PosixProcessManager
        assert type(manager) is expected
        assert manager._graceful_stopper is stopper


class TestMatchRank:
    @pytest.mark.parametrize(
        ("info", "target", "rank"),
        [
            (
                {
                    "name": "java",
                    "exe": "/usr/lib/jvm/bin/java",
                    "cmdline": ["java", "-jar", "s.jar"],
                },
                "java",
                1,
            ),
            (
                {"name": "java.exe", "exe": r"C:\Java\bin\java.exe", "cmdline": []},
                "java",
                1,
            ),
            ({"name": "JAVA", "exe": None, "cmdline": None}, "Java", 1),
            (
                {
                    "name": "python3",
                    "exe": "/usr/bin/python3.13",
                    "cmdline": ["python3", "/t/minecraft_dummy.py"],
                },
                "minecraft_dummy",
                2,
            ),
            (
                {
                    "name": "java",
                    "exe": "",
                    "cmdline": [
                        "java",
                        "@libraries/net/minecraftforge/x/unix_args.txt",
                    ],
                },
                "minecraftforge",
                2,
            ),
            (
                {"name": "bash", "exe": "/opt/javatools/bin/bash", "cmdline": ["bash"]},
                "java",
                2,
            ),
            (
                {
                    "name": "sh",
                    "exe": "/usr/bin/dash",
                    "cmdline": ["/bin/sh", "./run.sh", "nogui"],
                },
                "java",
                0,
            ),
            (
                {"name": "grep", "exe": "/usr/bin/grep", "cmdline": ["grep", "java"]},
                "java",
                0,
            ),
            (
                {
                    "name": "tail",
                    "exe": "/usr/bin/tail",
                    "cmdline": ["tail", "-f", "logs/java.log"],
                },
                "java",
                0,
            ),
            (
                {
                    "name": "tmux: server",
                    "exe": "/usr/bin/tmux",
                    "cmdline": ["tmux", "new", "java -jar s.jar"],
                },
                "java",
                0,
            ),
            (
                {
                    "name": "SCREEN",
                    "exe": "/usr/bin/screen",
                    "cmdline": ["SCREEN", "-dmS", "mc", "java"],
                },
                "java",
                0,
            ),
            ({"name": "java", "exe": "", "cmdline": []}, "", 0),
        ],
    )
    def test_rank(self, info, target, rank):
        assert match_rank(info, target) == rank


def fake_proc(pid, name, cmdline, cwd, create_time=100.0, status="running"):
    def read_cwd():
        if isinstance(cwd, Exception):
            raise cwd
        return cwd

    info = {
        "pid": pid,
        "name": name,
        "exe": "",
        "cmdline": cmdline,
        "create_time": create_time,
        "status": status,
    }
    return SimpleNamespace(pid=pid, info=info, cwd=read_cwd)


class TestScan:
    @pytest.fixture
    def scan(self, tmp_path, monkeypatch):
        server = str(tmp_path)

        def run(*procs, target="java"):
            monkeypatch.setattr(psutil, "process_iter", lambda **_kwargs: iter(procs))
            return scan_for_server(normalized_dir(server), target)

        return SimpleNamespace(run=run, server=server, other=str(tmp_path / "other"))

    def test_program_match_beats_a_launcher_argument_match(self, scan):
        launcher = fake_proc(
            10, "bash", ["bash", "-c", "java -jar s.jar"], scan.server, 50.0
        )
        jvm = fake_proc(11, "java", ["java", "-jar", "s.jar"], scan.server, 60.0)

        assert scan.run(launcher, jvm) == ProcessIdentity(11, 60.0)

    def test_oldest_process_wins_within_a_rank(self, scan):
        child = fake_proc(21, "java", ["java", "-jar", "server.jar"], scan.server, 70.0)
        launcher = fake_proc(
            20, "java", ["java", "-jar", "starter.jar"], scan.server, 60.0
        )

        assert scan.run(child, launcher) == ProcessIdentity(20, 60.0)

    def test_only_live_processes_in_the_server_directory(self, scan):
        candidates = [
            fake_proc(os.getpid(), "java", ["java"], scan.server),  # the monitor itself
            fake_proc(31, "java", ["java"], scan.server, status=psutil.STATUS_ZOMBIE),
            fake_proc(32, "java", ["java"], scan.other),
            fake_proc(33, "java", ["java"], psutil.AccessDenied(33)),
            fake_proc(34, "grep", ["grep", "java"], scan.server),
        ]

        assert scan.run(*candidates) is None

    def test_cwd_is_compared_normalized(self, scan):
        trailing = scan.server + os.sep + "." + os.sep

        assert scan.run(fake_proc(40, "java", ["java"], trailing)) == ProcessIdentity(
            40, 100.0
        )

    def test_unconfigured_discovery_finds_nothing(self, manager, mock_settings):
        mock_settings.processName = ""
        assert manager.find_process_identity() is None
        mock_settings.processName = "java"
        mock_settings.serverDir = None
        assert manager.find_process_identity() is None


class TestIdentityBookkeeping:
    def test_pid_file_round_trip(self, manager, mock_settings):
        identity = ProcessIdentity(4242, 1762300000.25)

        manager.set_known_identity(identity)

        pid_file = mock_settings.data_path("server.pid")
        assert open(pid_file, encoding="utf-8").read() == "4242\n1762300000.25\n"
        assert OSProcessManager().tracked_identity == identity

    def test_explicit_pid_file(self, mock_settings, tmp_path):
        pid_file = tmp_path / "custom.pid"
        manager = OSProcessManager(pid_file=str(pid_file))

        manager.set_known_identity(ProcessIdentity(77, 7.0))

        assert pid_file.read_text(encoding="utf-8") == "77\n7.0\n"
        assert not os.path.exists(mock_settings.data_path("server.pid"))

    def test_bare_pid_file_reads_the_live_creation_time(self, mock_settings):
        with open(mock_settings.data_path("server.pid"), "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")

        identity = OSProcessManager().tracked_identity

        assert identity == ProcessIdentity(os.getpid(), psutil.Process().create_time())

    @pytest.mark.parametrize("content", ["", "not-a-pid\n", "12\nnot-a-time\n"])
    def test_malformed_pid_file_is_ignored(self, mock_settings, content):
        with open(mock_settings.data_path("server.pid"), "w", encoding="utf-8") as f:
            f.write(content)

        assert OSProcessManager().tracked_identity is None

    def test_compare_and_clear_never_clears_a_replacement(self, manager, mock_settings):
        original = ProcessIdentity(111, 1.0)
        replacement = ProcessIdentity(222, 2.0)
        manager.set_known_identity(original)
        manager.set_known_identity(replacement)

        assert manager.clear_identity(original) is False
        assert manager.tracked_identity == replacement
        # Same PID with a different creation time is a different process too.
        assert manager.clear_identity(ProcessIdentity(222, 3.0)) is False
        assert manager.clear_identity(None) is False

        assert manager.clear_identity(replacement) is True
        assert manager.tracked_identity is None
        assert not os.path.exists(mock_settings.data_path("server.pid"))

    def test_identity_is_running_with_no_identity(self, manager):
        assert manager.identity_is_running(None) is False
        assert manager.observe().tracked_running is False

    def test_identity_is_running_with_a_dead_pid(self, manager):
        assert manager.identity_is_running(ProcessIdentity(99999999, None)) is False


class TestTerminateBookkeeping:
    def test_nothing_tracked(self, manager):
        assert manager.terminate_process() is False
        assert manager.terminate_process(ProcessIdentity(1, 1.0)) is False

    def test_expected_identity_must_match(self, manager):
        manager.set_known_identity(ProcessIdentity(4242, 10.0))

        assert manager.terminate_process(ProcessIdentity(4242, 11.0)) is False
        assert manager.tracked_identity == ProcessIdentity(4242, 10.0)

    def test_process_already_gone_is_a_confirmed_stop(self, manager):
        identity = ProcessIdentity(4242, 10.0)
        manager.set_known_identity(identity)

        with patch(
            "src.process_manager.psutil.Process", side_effect=psutil.NoSuchProcess(4242)
        ):
            assert manager.terminate_process(identity) is True

        assert manager.tracked_identity is None


class TestLaunchFailures:
    def test_empty_start_command(self, manager, mock_settings):
        mock_settings.startCommand = ""

        assert manager.launch_process() is None
        assert manager.tracked_identity is None

    def test_missing_server_directory(self, manager, mock_settings, tmp_path):
        mock_settings.serverDir = str(tmp_path / "missing")

        assert manager.launch_process() is None

    def test_unknown_program_is_reported_in_the_console_log(
        self, manager, mock_settings
    ):
        mock_settings.startCommand = ["definitely-not-a-minecraft-server-xyz", "nogui"]

        assert manager.launch_process() is None

        log = open(
            mock_settings.data_path("server-console.log"), encoding="utf-8"
        ).read()
        assert "launching in" in log
        assert "launch failed" in log
        assert manager.tracked_identity is None
        assert not os.path.exists(mock_settings.data_path("server.pid"))
