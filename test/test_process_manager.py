"""Unit tests for the process backend: command building, discovery ranking and
identity bookkeeping. No server process is started here (see
test/integration/test_process_manager.py for real processes)."""

import errno
import ntpath
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest

from src.lifecycle.messages import ProcessIdentity
from src.process_manager import (
    OSProcessManager,
    PosixProcessManager,
    WindowsProcessManager,
    _is_alive,
    _probe_linux,
    build_launch_command,
    create_process_manager,
    match_rank,
    normalized_dir,
    ProcessStateUnknown,
    popen_platform_kwargs,
    process_start_key,
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
            f'"{CMD}" /d /s /c ""run.bat" "nogui""'
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

    def as_dict(attrs=None, ad_value=None):
        return {key: info[key] for key in (attrs or info)}

    return SimpleNamespace(pid=pid, info=info, cwd=read_cwd, as_dict=as_dict)


def install_fake_processes(monkeypatch, procs):
    """Make discovery see only ``procs`` (fresh objects, probes, pids)."""
    table = {proc.pid: proc for proc in procs}
    monkeypatch.setattr(psutil, "pids", lambda: list(table))
    monkeypatch.setattr("src.process_manager.psutil.Process", lambda pid: table[pid])
    monkeypatch.setattr("src.process_manager._may_match_linux", lambda *_args: True)

    def probe(pid):
        info = table[pid].info
        return info["create_time"], info["status"] == psutil.STATUS_ZOMBIE

    monkeypatch.setattr("src.process_manager._probe", probe)


class TestScan:
    @pytest.fixture
    def scan(self, tmp_path, monkeypatch):
        server = str(tmp_path)

        def run(*procs, target="java"):
            install_fake_processes(monkeypatch, procs)
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

        assert identity == ProcessIdentity(os.getpid(), process_start_key(os.getpid()))

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

        with patch("src.process_manager._probe", return_value=None):
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


class TestLauncherOutlivingTheServer:
    """observe(): a launcher left behind by its server (run.bat's `pause`)."""

    def scan(self, manager, monkeypatch, tree_has_server):
        manager.set_known_identity(ProcessIdentity(4242, 10.0))
        monkeypatch.setattr(manager, "_root_alive", lambda _identity: True)
        monkeypatch.setattr(manager, "_remember_descendants", lambda _identity: None)
        monkeypatch.setattr(manager, "find_process_identity", lambda: None)
        return self.scan_again(manager, monkeypatch, tree_has_server)

    def test_two_scans_without_the_server_end_it(self, manager, monkeypatch):
        assert self.scan(manager, monkeypatch, [True, False, False]) == [
            True,
            True,
            False,
        ]

    def test_a_returning_server_resets_the_count(self, manager, monkeypatch):
        answers = [True, False, True, False, True]

        assert self.scan(manager, monkeypatch, answers) == [True] * 5

    def test_a_launcher_that_never_had_a_server_is_left_alone(
        self, manager, monkeypatch
    ):
        # Still launching: the JVM has not appeared yet.
        assert self.scan(manager, monkeypatch, [False, False, False]) == [True] * 3

    def test_an_unreadable_tree_changes_nothing(self, manager, monkeypatch):
        assert self.scan(manager, monkeypatch, [True, None, None]) == [True] * 3

    def test_a_new_identity_starts_over(self, manager, monkeypatch):
        assert self.scan(manager, monkeypatch, [True, False]) == [True, True]
        manager.set_known_identity(ProcessIdentity(5252, 20.0))

        assert self.scan_again(manager, monkeypatch, [False, False]) == [True, True]

    @staticmethod
    def scan_again(manager, monkeypatch, tree_has_server):
        answers = iter(tree_has_server)
        monkeypatch.setattr(manager, "_tree_has_server", lambda *_args: next(answers))
        return [manager.observe().tracked_running for _ in tree_has_server]


LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="/proc start times"
)


def emfile(*_args, **_kwargs):
    raise OSError(errno.EMFILE, "Too many open files")


def unknown(*_args, **_kwargs):
    raise ProcessStateUnknown("cannot inspect processes: EMFILE")


class TestUnknownIsNotGone:
    """EMFILE and other OS errors mean "unknown, keep the last state"."""

    def test_liveness_observation_and_discovery_report_unknown(
        self, manager, monkeypatch
    ):
        identity = ProcessIdentity(4242, 10.0)
        manager.set_known_identity(identity)
        monkeypatch.setattr("src.process_manager._probe", unknown)

        with pytest.raises(ProcessStateUnknown):
            manager.identity_is_running(identity)
        with pytest.raises(ProcessStateUnknown):
            manager.observe()
        assert manager.tracked_identity == identity

    def test_a_stop_never_declares_an_unknown_server_gone(
        self, manager, mock_settings, monkeypatch
    ):
        identity = ProcessIdentity(4242, 10.0)
        manager.set_known_identity(identity)
        monkeypatch.setattr("src.process_manager._probe", unknown)

        assert manager.terminate_process(identity) is False

        assert manager.tracked_identity == identity
        assert os.path.exists(mock_settings.data_path("server.pid"))

    def test_an_unreadable_process_table_is_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(psutil, "pids", emfile)

        with pytest.raises(ProcessStateUnknown):
            scan_for_server(normalized_dir(tmp_path), "java")

    def test_an_unreadable_candidate_is_unknown(self, tmp_path, monkeypatch):
        jvm = fake_proc(11, "java", ["java"], OSError(errno.EMFILE, "Too many"))
        install_fake_processes(monkeypatch, [jvm])

        with pytest.raises(ProcessStateUnknown):
            scan_for_server(normalized_dir(tmp_path), "java")

    def test_a_tree_member_that_cannot_be_read_counts_as_alive(self):
        member = SimpleNamespace(is_running=emfile, status=emfile)

        assert _is_alive(member) is True

    @LINUX_ONLY
    def test_proc_read_errors_become_unknown(self, monkeypatch):
        monkeypatch.setattr("src.process_manager.open", emfile, raising=False)

        with pytest.raises(ProcessStateUnknown):
            _probe_linux(os.getpid())


@LINUX_ONLY
class TestClockStepProofIdentity:
    def test_start_key_is_boot_relative(self):
        process = psutil.Process()
        expected = process.create_time() - psutil.boot_time()

        assert process_start_key(os.getpid()) == pytest.approx(expected, abs=0.02)

    def test_a_clock_step_changes_nothing(self, manager, monkeypatch):
        identity = ProcessIdentity(os.getpid(), process_start_key(os.getpid()))
        before = psutil.Process().create_time()
        boot_time = psutil._pslinux.boot_time
        # A wall-clock step moves /proc/stat btime and so psutil's create_time.
        monkeypatch.setattr(psutil._pslinux, "boot_time", lambda: boot_time() + 3600)

        assert psutil.Process().create_time() == pytest.approx(before + 3600)
        assert process_start_key(os.getpid()) == identity.create_time
        assert manager.identity_is_running(identity) is True


class TestWindowsCommandLine:
    @pytest.mark.parametrize(
        "command",
        [
            ["run.bat", "say %PATH%"],
            ["C:\\100%\\run.bat", "nogui"],
            ["run.cmd", 'say "hi there"'],
            ["run.bat", "line\nbreak"],
        ],
    )
    def test_batch_list_with_unquotable_elements_is_refused(self, command):
        with pytest.raises(ValueError, match="as one string"):
            build_launch_command(command, windows=True)

    def test_batch_list_elements_are_quoted_so_metacharacters_are_literal(
        self, monkeypatch
    ):
        monkeypatch.setenv("COMSPEC", CMD)
        command = [r"C:\Servers\R&D Server\run.bat", "-Dmotd=Tom&Jerry", "a^b|<c>"]

        assert build_launch_command(command, windows=True) == (
            f'"{CMD}" /d /s /c ""C:\\Servers\\R&D Server\\run.bat" '
            '"-Dmotd=Tom&Jerry" "a^b|<c>""'
        )

    def test_a_trailing_backslash_cannot_escape_the_closing_quote(self):
        line = build_launch_command(["run.bat", "C:\\worlds\\"], windows=True)

        assert line.endswith('"C:\\worlds\\\\""')

    def test_relative_program_is_resolved_against_the_server_dir(self):
        server = r"C:\Servers\mc"

        assert build_launch_command(
            ["jre/bin/java.exe", "-jar", "server.jar"], windows=True, server_dir=server
        ) == [r"C:\Servers\mc\jre\bin\java.exe", "-jar", "server.jar"]
        assert (
            build_launch_command(
                r"jre\bin\java.exe -jar server.jar nogui",
                windows=True,
                server_dir=server,
            )
            == r"C:\Servers\mc\jre\bin\java.exe -jar server.jar nogui"
        )
        assert (
            build_launch_command(
                r'"my jre\java.exe" -jar s.jar', windows=True, server_dir=server
            )
            == r'"C:\Servers\mc\my jre\java.exe" -jar s.jar'
        )

    def test_programs_on_the_path_and_absolute_ones_are_kept(self):
        server = r"C:\mc"

        assert (
            build_launch_command("java -jar s.jar", windows=True, server_dir=server)
            == "java -jar s.jar"
        )
        absolute = [r"C:\Java\bin\java.exe", "-jar", "s.jar"]
        assert build_launch_command(absolute, windows=True, server_dir=server) == (
            absolute
        )

    def test_a_bare_program_found_in_the_server_dir_is_resolved(self, tmp_path):
        (tmp_path / "java.exe").write_text("", encoding="utf-8")

        args = build_launch_command(
            ["java.exe", "-jar", "s.jar"], windows=True, server_dir=str(tmp_path)
        )

        assert args[0] == ntpath.normpath(ntpath.join(str(tmp_path), "java.exe"))

    def test_posix_keeps_relative_programs(self):
        assert build_launch_command(
            ["jre/bin/java", "-jar"], windows=False, server_dir="/srv/mc"
        ) == ["jre/bin/java", "-jar"]


class TestLaunchHints:
    @pytest.fixture
    def posix(self, mock_settings):
        mock_settings.startCommand = "./run.sh nogui"
        return PosixProcessManager()

    def test_an_inaccessible_server_folder_is_named(self, posix, monkeypatch, caplog):
        monkeypatch.setattr("src.process_manager.os.access", lambda *_args: False)

        assert posix.launch_process() is None

        assert "not accessible to the monitor's user" in caplog.text
        assert "chmod" not in caplog.text

    @pytest.mark.parametrize(
        "folder,hint",
        [
            (True, "the server folder is not accessible to the monitor's user"),
            (False, "is the start script executable? try chmod +x"),
        ],
    )
    def test_permission_errors_point_at_the_right_culprit(
        self, posix, mock_settings, monkeypatch, caplog, folder, hint
    ):
        culprit = mock_settings.serverDir if folder else "./run.sh"

        def deny(*_args, **_kwargs):
            raise PermissionError(errno.EACCES, "Permission denied", culprit)

        monkeypatch.setattr("src.process_manager.subprocess.Popen", deny)

        assert posix.launch_process() is None

        assert hint in caplog.text
        log = open(mock_settings.data_path("server-console.log"), encoding="utf-8")
        with log:
            assert hint in log.read()


class FakeMember:
    """A remembered descendant (psutil.Process stand-in)."""

    def __init__(self, pid, cmdline, alive=True):
        self.pid = pid
        self.cmdline = cmdline
        self.alive = alive

    def is_running(self):
        return self.alive

    def status(self):
        return psutil.STATUS_RUNNING

    def as_dict(self, attrs=None, ad_value=None):
        name = os.path.basename(self.cmdline[0])
        return {"name": name, "exe": "", "cmdline": self.cmdline}


class TestLeftoversAfterTheLauncherExited:
    """The launcher is gone; only processes it started remain (R2, R14)."""

    @pytest.fixture
    def tracked(self, manager, mock_settings, monkeypatch):
        mock_settings.processName = "java"
        identity = ProcessIdentity(4242, 10.0)
        manager.set_known_identity(identity)
        monkeypatch.setattr(manager, "_root_alive", lambda _identity: False)
        monkeypatch.setattr(manager, "find_process_identity", lambda: None)
        return identity

    def test_a_background_helper_does_not_keep_the_server_running(
        self, manager, tracked
    ):
        manager._remember([FakeMember(5001, ["sleep", "600"])], tracked)

        assert manager.identity_is_running(tracked) is False  # start guard
        scans = [manager.observe().tracked_running for _ in range(3)]
        assert scans == [True, False, False]

    def test_the_jvm_outliving_its_launcher_keeps_it_running(self, manager, tracked):
        jvm = FakeMember(5002, ["java", "-jar", "server.jar"])
        manager._remember([jvm, FakeMember(5001, ["sleep", "600"])], tracked)

        assert manager.identity_is_running(tracked) is True
        assert [manager.observe().tracked_running for _ in range(3)] == [True] * 3

        jvm.alive = False  # it saved and exited; the helper lingers
        assert [manager.observe().tracked_running for _ in range(2)] == [True, False]

    def test_nothing_left_is_an_exit_at_once(self, manager, tracked):
        manager._remember([FakeMember(5001, ["sleep", "600"], alive=False)], tracked)

        assert manager.observe().tracked_running is False
