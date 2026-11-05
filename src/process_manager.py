# Copyright (c) 2024 Nomomo
# Copyright (c) 2026 Kevin Perez - Modified work
# Copyright (c) 2026 Koilparampil - Modified work (Minecraft port)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

"""OS process backends: launch, discover, gracefully stop the server process.

Passive backend: no threads, no events. The lifecycle runtime calls it from
its detection and process workers, and only the supervisor turns what it
reports into lifecycle state.

- Launch: ``settings.startCommand`` in ``settings.serverDir`` with a stdin
  pipe (kept for a graceful ``stop``) and stdout/stderr in
  ``data/server-console.log``. The server gets its own session (POSIX) or
  process group (Windows), so stopping the monitor never stops the server.
- Discovery: a live process whose cwd is ``serverDir`` and whose name, exe or
  command line contains ``settings.processName``. Launch wrappers are normal
  (Forge's ``run.sh`` is ``sh`` running ``java`` as a child), so a process
  counts together with its descendants.
- A launcher that outlives its server (Forge's ``run.bat`` ends with
  ``pause``, scripts may ``read``) does not keep the server "running": once
  the tree has had a server process, two scans without one count as an exit
  (the first also closes our stdin pipe so such a launcher can return).
- Stop: ``stop`` on our stdin pipe (or the RCON ``graceful_stopper`` for an
  adopted server; also RCON when stdin did not work within 15 s), wait
  ``stopTimeout`` for the whole tree, then terminate (SIGTERM runs the JVM
  shutdown hook, which saves the worlds) and wait ``max(30, stopTimeout)``,
  then kill. An unattended stop (auto-stop) never force-kills a server that
  cannot be asked to stop (Windows without stdin pipe or RCON).
"""

import contextlib
import logging
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime

import psutil

from src.lifecycle.messages import ProcessIdentity, ProcessSnapshot
from src.settings import settings

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
PID_FILE_NAME = "server.pid"
CONSOLE_LOG_NAME = "server-console.log"
STOP_COMMAND = b"stop\n"
DEFAULT_STOP_TIMEOUT = 120.0

_SCAN_ATTRS = ["pid", "name", "exe", "cmdline", "create_time", "status"]
_INFO_ATTRS = ["name", "exe", "cmdline"]

# Programs that can mention the server in their command line while never being
# the server (``tail -f logs/latest.log | grep java`` run in the server
# directory, or a screen/tmux session whose java has exited).
_NEVER_SERVER = frozenset(
    {
        "ack",
        "ag",
        "awk",
        "bat",
        "btop",
        "cat",
        "egrep",
        "emacs",
        "fgrep",
        "find",
        "findstr",
        "gawk",
        "gdb",
        "grep",
        "head",
        "htop",
        "journalctl",
        "less",
        "lsof",
        "man",
        "more",
        "most",
        "nano",
        "nvim",
        "pgrep",
        "pkill",
        "ps",
        "rg",
        "screen",
        "sed",
        "strace",
        "tail",
        "tmux",
        "tmux: server",
        "top",
        "vi",
        "view",
        "vim",
        "watch",
        "xargs",
        "zgrep",
    }
)


# ----------------------------------------------------------------------
# Pure helpers (platform passed in, so both paths are unit-testable)
# ----------------------------------------------------------------------


def build_launch_command(command, windows: bool = IS_WINDOWS):
    """Popen ``args`` for ``settings.startCommand``.

    A list is used as-is. A string is split with ``shlex`` on POSIX and passed
    through unchanged on Windows (CreateProcess parses it). On Windows a
    ``.bat``/``.cmd`` program runs through ``cmd.exe /d /s /c``.

    Raises:
        ValueError: the command is empty or cannot be parsed.
    """
    if isinstance(command, (list, tuple)):
        args = [str(part) for part in command]
        if not args or not args[0].strip():
            raise ValueError("minecraft.startCommand is empty")
        if windows and _is_batch_file(args[0]):
            return _cmd_wrapper(subprocess.list2cmdline(args))
        return args
    if not isinstance(command, str) or not command.strip():
        raise ValueError("minecraft.startCommand is empty")
    command = command.strip()
    if not windows:
        args = shlex.split(command)
        if not args:
            raise ValueError("minecraft.startCommand is empty")
        return args
    if _is_batch_file(_first_windows_token(command)):
        return _cmd_wrapper(command)
    return command


def popen_platform_kwargs(windows: bool = IS_WINDOWS) -> dict:
    """Detach the server from the monitor's terminal, signals and console."""
    if windows:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def describe_command(args) -> str:
    if isinstance(args, str):
        return args
    return shlex.join(args)


def _is_batch_file(program: str) -> bool:
    return program.strip().strip('"').lower().endswith((".bat", ".cmd"))


def _first_windows_token(command: str) -> str:
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else command[1:]
    return command.split(None, 1)[0]


def _cmd_wrapper(command_line: str) -> str:
    # /s strips exactly the outer quotes, so any quoting inside survives.
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return f'"{comspec}" /d /s /c "{command_line}"'


def _program_name(text: str) -> str:
    base = os.path.basename(text.strip()).lower()
    return base.removesuffix(".exe")


def match_rank(info: dict, target: str) -> int:
    """How well a process (psutil ``info`` dict) matches ``processName``.

    1: the program itself (name, exe or argv[0] basename) contains it.
    2: only the full exe path or another argument contains it (a launcher
       script, or ``python minecraft_dummy.py``).
    0: no match, or a tool that is never the server.
    """
    target = (target or "").strip().lower()
    if not target:
        return 0
    name = str(info.get("name") or "").lower()
    exe = str(info.get("exe") or "").lower()
    cmdline = [str(arg).lower() for arg in (info.get("cmdline") or [])]
    program = _program_name(cmdline[0]) if cmdline else ""
    if _program_name(name) in _NEVER_SERVER or program in _NEVER_SERVER:
        return 0
    if target in name or target in _program_name(exe) or target in program:
        return 1
    if target in exe or any(target in arg for arg in cmdline):
        return 2
    return 0


def normalized_dir(path) -> str | None:
    if not path:
        return None
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))
    except (OSError, ValueError):
        return None


def _seconds(value, default: float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return seconds if seconds >= 0 else default


# ----------------------------------------------------------------------
# psutil helpers
# ----------------------------------------------------------------------


def _identity_of(process: psutil.Process) -> ProcessIdentity:
    """Identity of a process, with create time left unknown if unreadable."""
    try:
        return ProcessIdentity(process.pid, process.create_time())
    except psutil.Error:
        return ProcessIdentity(process.pid, None)


def _identity_for_pid(pid: int) -> ProcessIdentity:
    try:
        return _identity_of(psutil.Process(int(pid)))
    except (psutil.Error, ValueError):
        return ProcessIdentity(int(pid), None)


def _identity_is_live(identity: ProcessIdentity) -> bool:
    """The identity's process exists, is the same process and is no zombie."""
    try:
        process = psutil.Process(identity.pid)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return psutil.pid_exists(identity.pid)
    except (psutil.Error, ValueError, OSError):
        return False
    try:
        if identity.create_time is not None and not identity.matches(
            _identity_of(process)
        ):
            return False  # the PID now belongs to another process
        return process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:  # includes ZombieProcess
        return False
    except psutil.AccessDenied:
        return True
    except (psutil.Error, OSError):
        return False


def _is_alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True
    except (psutil.Error, OSError):
        return False


def _info_of(process: psutil.Process) -> dict:
    try:
        return process.as_dict(attrs=_INFO_ATTRS, ad_value=None)
    except (psutil.Error, OSError):
        return {}


def _cwd_matches(process: psutil.Process, server_dir: str) -> bool:
    try:
        cwd = process.cwd()
    except (psutil.Error, OSError):
        return False
    return bool(cwd) and normalized_dir(cwd) == server_dir


def _with_descendants(processes) -> set:
    tree = set(processes)
    for process in list(tree):
        if not _is_alive(process):
            continue
        with contextlib.suppress(psutil.Error, OSError):
            tree.update(process.children(recursive=True))
    return tree


def _tree_has_server(pid: int, target: str) -> bool | None:
    """Whether the process or a live descendant matches processName (None: unknown)."""
    try:
        root = psutil.Process(pid)
        tree = [root, *root.children(recursive=True)]
    except (psutil.Error, OSError):
        return None
    return any(match_rank(_info_of(p), target) and _is_alive(p) for p in tree)


def _oldest_first(processes) -> list:
    def created(process):
        try:
            return process.create_time()
        except (psutil.Error, OSError):
            return float("inf")

    return sorted(processes, key=created)


def _signal_all(processes, method: str):
    # Oldest first: a wrapper with a restart loop dies before it can respawn.
    for process in _oldest_first(processes):
        with contextlib.suppress(psutil.Error, OSError):
            getattr(process, method)()


def _scan_candidate(proc, server_dir: str, target: str, own_pid: int):
    """(rank, create_time, identity) for a matching process, else None."""
    try:
        info = proc.info
        if info.get("pid") == own_pid or info.get("status") == psutil.STATUS_ZOMBIE:
            return None
        rank = match_rank(info, target)
        if not rank or not _cwd_matches(proc, server_dir):
            return None
        created = info.get("create_time")
        order = created if created is not None else float("inf")
        return rank, order, ProcessIdentity(info["pid"], created)
    except (psutil.Error, OSError, KeyError):
        return None


def scan_for_server(server_dir: str, target: str) -> ProcessIdentity | None:
    """Best match in ``server_dir``: program matches before argument matches,
    then the oldest process (a launcher before the JVM it spawned)."""
    own_pid = os.getpid()
    best = None
    try:
        for proc in psutil.process_iter(attrs=_SCAN_ATTRS, ad_value=None):
            candidate = _scan_candidate(proc, server_dir, target, own_pid)
            if candidate is not None and (best is None or candidate[:2] < best[:2]):
                best = candidate
    except Exception:
        logger.exception("Process scan failed")
    return best[2] if best is not None else None


# ----------------------------------------------------------------------
# Backend
# ----------------------------------------------------------------------


class OSProcessManager:
    """Process backend for the host OS (see the module docstring).

    ``windows`` selects the platform behavior; ``create_process_manager``
    picks the subclass for the host.
    """

    windows = IS_WINDOWS
    TERMINATE_TIMEOUT = 30.0  # at least, after SIGTERM: max(this, stopTimeout)
    KILL_TIMEOUT = 10.0  # after SIGKILL
    WAIT_INTERVAL = 0.2
    STDIN_STOP_GRACE = 15.0  # then also send `stop` over RCON
    SERVER_GONE_SCANS = 2  # scans without the server before its launcher is ignored

    def __init__(
        self,
        graceful_stopper: Callable[[], bool] | None = None,
        pid_file: str | None = None,
    ):
        self._graceful_stopper = graceful_stopper
        self._pid_file = pid_file
        self._lock = threading.RLock()
        self._identity: ProcessIdentity | None = None
        self._popen: subprocess.Popen | None = None
        self._server_seen = False  # the tracked tree has had a server process
        self._missing_scans = 0  # consecutive scans without one since then
        self._load_identity_from_file()

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------

    @property
    def tracked_identity(self) -> ProcessIdentity | None:
        with self._lock:
            return self._identity

    @property
    def pid_file(self) -> str:
        return self._pid_file or settings.data_path(PID_FILE_NAME)

    def set_known_identity(self, identity: ProcessIdentity) -> None:
        """Record the process we own before any SERVER_STARTED is committed."""
        with self._lock:
            if self._popen is not None and self._popen.pid != identity.pid:
                self._release_popen_locked()
            if not identity.matches(self._identity):
                self._reset_tree_watch_locked()
            self._identity = identity
            self._write_pid_file(identity)

    def clear_identity(self, expected: ProcessIdentity | None) -> bool:
        """Compare-and-clear: never forget a replacement process."""
        with self._lock:
            current = self._identity
            if current is None or expected is None or not current.matches(expected):
                return False
            self._identity = None
            self._reset_tree_watch_locked()
            self._remove_pid_file()
            if self._popen is not None and self._popen.pid == current.pid:
                self._release_popen_locked()
            return True

    def identity_is_running(self, identity: ProcessIdentity | None) -> bool:
        if identity is None:
            return False
        popen = self._popen_for(identity)
        self._reap(popen)
        live = _identity_is_live(identity)
        if not live:
            self._reap(popen)  # it may have exited between the two checks
        return live

    def find_process_identity(self) -> ProcessIdentity | None:
        """A live (non-zombie) process whose cwd is settings.serverDir and whose
        name/cmdline contains settings.processName; prefers the tracked one;
        never this monitor process."""
        server_dir = normalized_dir(settings.serverDir)
        target = str(settings.processName or "").strip().lower()
        if not server_dir or not target:
            return None
        tracked = self.tracked_identity
        if tracked is not None and self._is_tracked_server(tracked, server_dir, target):
            return tracked
        return scan_for_server(server_dir, target)

    def _is_tracked_server(
        self, tracked: ProcessIdentity, server_dir: str, target: str
    ) -> bool:
        # The tracked process may be a wrapper (sh run.sh, cmd /c run.bat)
        # whose child is the JVM; the tree is the server.
        if tracked.pid == os.getpid() or not self.identity_is_running(tracked):
            return False
        try:
            process = psutil.Process(tracked.pid)
            if not _cwd_matches(process, server_dir):
                return False
            tree = [process, *process.children(recursive=True)]
        except (psutil.Error, OSError):
            return False
        return any(match_rank(_info_of(member), target) for member in tree)

    def observe(self) -> ProcessSnapshot:
        """I/O-only observation for the lifecycle supervisor (one per scan)."""
        tracked = self.tracked_identity
        running = self.identity_is_running(tracked)
        if running and self._launcher_outlived_server(tracked):
            running = False
        return ProcessSnapshot(
            tracked=tracked,
            tracked_running=running,
            found=self.find_process_identity(),
        )

    def _launcher_outlived_server(self, tracked: ProcessIdentity) -> bool:
        """True once the server has been missing from the tracked tree for
        SERVER_GONE_SCANS scans in a row while its launcher lingers.

        Only after the tree has had a server process (a launcher may take a
        moment to start the JVM). The first such scan closes our stdin pipe,
        so a launcher waiting in ``pause``/``read`` returns by itself.
        """
        has_server = _tree_has_server(tracked.pid, str(settings.processName or ""))
        with self._lock:
            if has_server is None or not tracked.matches(self._identity):
                return False
            if has_server:
                self._server_seen, self._missing_scans = True, 0
                return False
            if not self._server_seen:
                return False
            self._missing_scans += 1
            missing = self._missing_scans
        if missing == 1:
            logger.info(
                f"The server exited but its launcher (pid {tracked.pid}) is still "
                "running; closing its console input"
            )
            popen = self._popen_for(tracked)
            if popen is not None:
                self._close_stdin(popen)
        elif missing == self.SERVER_GONE_SCANS:
            logger.warning(
                f"The launcher (pid {tracked.pid}) outlived the server; treating "
                "the server as stopped"
            )
        return missing >= self.SERVER_GONE_SCANS

    def _reset_tree_watch_locked(self):
        self._server_seen = False
        self._missing_scans = 0

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    def launch_process(self) -> ProcessIdentity | None:
        """Spawn settings.startCommand in settings.serverDir."""
        server_dir = settings.serverDir
        try:
            log_path = settings.data_path(CONSOLE_LOG_NAME)
        except OSError as e:
            logger.error(f"Cannot create the monitor data directory: {e}")
            return None
        try:
            args = build_launch_command(settings.startCommand, self.windows)
        except ValueError as e:
            logger.error(f"Cannot launch the Minecraft server: {e}")
            return None
        if not server_dir or not os.path.isdir(server_dir):
            logger.error(
                f"Cannot launch the Minecraft server: no directory {server_dir!r}"
            )
            return None

        popen = self._spawn(args, server_dir, log_path)
        if popen is None:
            return None
        identity = _identity_for_pid(popen.pid)
        with self._lock:
            self._release_popen_locked()
            self._popen = popen
            self._identity = identity
            self._reset_tree_watch_locked()
            self._write_pid_file(identity)
        logger.info(
            f"Launched the Minecraft server (pid {popen.pid}) in {server_dir}: "
            f"{describe_command(args)}"
        )
        return identity

    def _spawn(self, args, cwd: str, log_path: str) -> subprocess.Popen | None:
        with contextlib.ExitStack() as stack:
            try:
                # Truncated per launch; the child keeps its own handle.
                log_file = stack.enter_context(open(log_path, "wb"))
            except OSError as e:
                logger.error(f"Cannot open the server console log {log_path}: {e}")
                return None
            _write_log_line(log_file, f"launching in {cwd}: {describe_command(args)}")
            return self._popen_server(args, cwd, log_file)

    def _popen_server(self, args, cwd: str, log_file) -> subprocess.Popen | None:
        try:
            return subprocess.Popen(
                args,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                **popen_platform_kwargs(self.windows),
            )
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            hint = ""
            if isinstance(e, PermissionError) and not self.windows:
                hint = " (is the start script executable? try chmod +x)"
            logger.error(f"Failed to launch the Minecraft server: {e}{hint}")
            _write_log_line(log_file, f"launch failed: {e}{hint}")
            return None

    # ------------------------------------------------------------------
    # Graceful stop chain
    # ------------------------------------------------------------------

    def terminate_process(
        self, expected: ProcessIdentity | None = None, force: bool = True
    ) -> bool | None:
        """Graceful stop chain. True=gone, False=still alive, None=pending.

        Blocking (runs on the runtime's process worker): ``stop`` on our
        stdin (plus RCON if that has not worked within STDIN_STOP_GRACE),
        else ``graceful_stopper()``; wait up to ``stopTimeout`` for the
        process tree; terminate the tree and wait ``max(30, stopTimeout)``;
        kill. ``force=False`` (an unattended auto-stop) never force-kills a
        server that could not be asked to stop (Windows: no graceful signal).
        """
        with self._lock:
            current = self._identity
        if current is None:
            return False
        if expected is not None and not current.matches(expected):
            return False

        popen = self._popen_for(current)
        root = self._live_process(current, popen)
        if root is None:
            self.clear_identity(current)
            return True
        if not self._stop_tree(root, popen, force):
            return False
        self.clear_identity(current)
        return True

    def _live_process(
        self, identity: ProcessIdentity, popen: subprocess.Popen | None
    ) -> psutil.Process | None:
        self._reap(popen)
        try:
            process = psutil.Process(identity.pid)
        except (psutil.Error, ValueError, OSError):
            return None
        if not identity.matches(_identity_of(process)):
            return None  # the PID now belongs to another process: ours is gone
        return process if _is_alive(process) else None

    def _stop_tree(self, root: psutil.Process, popen, force: bool) -> bool:
        tree = _with_descendants({root})
        timeout = _seconds(settings.stopTimeout, DEFAULT_STOP_TIMEOUT)
        term_wait = max(self.TERMINATE_TIMEOUT, timeout)  # never kill mid-save
        channel = self._request_graceful_stop(root.pid, popen)
        if channel is None:
            return self._stop_unrequested(tree, popen, root.pid, force)
        if not self._wait_graceful(tree, popen, channel, timeout, root.pid):
            return True
        logger.warning(f"The server did not stop within {timeout:g}s; terminating it")
        return self._force_stop(tree, popen, term_wait)

    def _stop_unrequested(self, tree: set, popen, pid: int, force: bool) -> bool:
        """No `stop` could be delivered (no console pipe, RCON unavailable)."""
        timeout = _seconds(settings.stopTimeout, DEFAULT_STOP_TIMEOUT)
        term_wait = max(self.TERMINATE_TIMEOUT, timeout)
        if not self.windows:
            # SIGTERM runs the JVM shutdown hook, which saves the worlds.
            logger.warning(
                f"Could not send 'stop' to server process {pid} (no console pipe, "
                "RCON unavailable); sending SIGTERM"
            )
            return self._force_stop(tree, popen, term_wait)
        if not force:
            logger.warning(
                f"Auto-stop skipped: server process {pid} cannot be asked to stop "
                "(no console pipe, RCON unavailable) and Windows could only "
                "force-kill it. It keeps running; enable RCON so it can be saved "
                "and stopped automatically."
            )
            return False
        logger.warning(
            f"Could not ask server process {pid} to stop (no console pipe, RCON "
            f"unavailable); waiting {timeout:g}s, then force-terminating it on "
            "request (progress since the last autosave may be lost)"
        )
        if not self._wait_for_exit(tree, timeout, popen):
            return True
        return self._force_stop(tree, popen, term_wait)

    def _wait_graceful(
        self, tree: set, popen, channel: str, timeout: float, pid: int
    ) -> list:
        """Wait for the tree after a graceful request. When `stop` went to
        stdin but the tree lives on after STDIN_STOP_GRACE (a launch script
        that does not pass stdin to the JVM), also ask over RCON."""
        grace = self.STDIN_STOP_GRACE
        if (
            channel == "stdin"
            and self._graceful_stopper is not None
            and timeout > grace
        ):
            if not self._wait_for_exit(tree, grace, popen):
                return []
            logger.info(
                f"Server process {pid} is still running {grace:g}s after 'stop' "
                "on its console; also sending 'stop' over RCON"
            )
            self._call_stopper()
            timeout -= grace
        return self._wait_for_exit(tree, timeout, popen)

    def _request_graceful_stop(self, pid: int, popen) -> str | None:
        """'stdin', 'rcon' or None (no stop could be delivered)."""
        if popen is not None and self._send_console_stop(popen):
            logger.info(f"Sent 'stop' to the server console (pid {pid})")
            return "stdin"
        if self._call_stopper():
            logger.info(f"Requested a graceful stop over RCON (pid {pid})")
            return "rcon"
        return None

    def _call_stopper(self) -> bool:
        stopper = self._graceful_stopper
        if stopper is None:
            return False
        try:
            sent = bool(stopper())
        except Exception:
            logger.exception("The graceful stop request (RCON) failed")
            return False
        if not sent:
            logger.warning("The graceful stop request (RCON) was not delivered")
        return sent

    @staticmethod
    def _send_console_stop(popen: subprocess.Popen) -> bool:
        stdin = popen.stdin
        if stdin is None or stdin.closed:
            return False
        try:
            stdin.write(STOP_COMMAND)
            stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _force_stop(self, tree: set, popen, term_wait: float) -> bool:
        tree = _with_descendants(tree)
        if self.windows:
            logger.warning(
                "Windows has no graceful stop signal: force-terminating the server "
                "process tree (progress since the last autosave may be lost)"
            )
        _signal_all([p for p in tree if _is_alive(p)], "terminate")
        alive = self._wait_for_exit(tree, term_wait, popen)
        if not alive:
            return True
        logger.error(
            f"The server ignored the termination for {term_wait:g}s; killing it"
        )
        _signal_all(alive, "kill")
        alive = self._wait_for_exit(set(alive), self.KILL_TIMEOUT, popen)
        if alive:
            pids = sorted(process.pid for process in alive)
            logger.error(f"Server processes {pids} are still alive after SIGKILL")
        return not alive

    def _wait_for_exit(self, tree: set, timeout: float, popen) -> list:
        """Wait until every process of the tree is gone; returns the survivors.

        Descendants started meanwhile join the tree. Once only launch wrappers
        are left (no process matching processName), our stdin pipe is closed:
        Forge's run.bat ends with ``pause``, which waits for input.
        """
        target = str(settings.processName or "")
        deadline = time.monotonic() + max(0.0, timeout)
        stdin_closed = False
        while True:
            self._reap(popen)
            tree.update(_with_descendants(tree))
            alive = [process for process in tree if _is_alive(process)]
            if not alive:
                return []
            if (
                popen is not None
                and not stdin_closed
                and not any(match_rank(_info_of(p), target) for p in alive)
            ):
                self._close_stdin(popen)
                stdin_closed = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return alive
            time.sleep(min(self.WAIT_INTERVAL, remaining))

    # ------------------------------------------------------------------
    # Popen and PID file bookkeeping
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close our pipes. Never stops the server (it is adopted next time)."""
        with self._lock:
            popen = self._popen
        if popen is not None:
            self._close_stdin(popen)
            self._reap(popen)

    def _popen_for(self, identity: ProcessIdentity) -> subprocess.Popen | None:
        with self._lock:
            popen = self._popen
        if popen is not None and popen.pid == identity.pid:
            return popen
        return None

    @staticmethod
    def _reap(popen: subprocess.Popen | None):
        # Our own exited child stays a zombie until it is waited for.
        if popen is not None:
            with contextlib.suppress(Exception):
                popen.poll()

    @staticmethod
    def _close_stdin(popen: subprocess.Popen):
        if popen.stdin is not None:
            with contextlib.suppress(Exception):
                popen.stdin.close()

    def _release_popen_locked(self):
        popen, self._popen = self._popen, None
        if popen is not None:
            self._close_stdin(popen)
            self._reap(popen)

    def _write_pid_file(self, identity: ProcessIdentity):
        path = self.pid_file
        text = f"{identity.pid}\n"
        if identity.create_time is not None:
            text += f"{identity.create_time!r}\n"
        temp_path = f"{path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as file:
                file.write(text)
            os.replace(temp_path, path)
        except OSError as e:
            logger.warning(f"Could not write the PID file {path}: {e}")

    def _remove_pid_file(self):
        with contextlib.suppress(OSError):
            os.remove(self.pid_file)

    def _load_identity_from_file(self):
        try:
            path = self.pid_file
            with open(path, encoding="utf-8") as file:
                fields = file.read().split()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning(f"Could not read the PID file: {e}")
            return
        try:
            pid = int(fields[0])
            create_time = float(fields[1]) if len(fields) > 1 else None
        except (IndexError, ValueError):
            logger.warning(f"Ignoring a malformed PID file: {path}")
            return
        if create_time is None:
            self._identity = _identity_for_pid(pid)
        else:
            self._identity = ProcessIdentity(pid, create_time)


class PosixProcessManager(OSProcessManager):
    windows = False


class WindowsProcessManager(OSProcessManager):
    windows = True


def create_process_manager(
    graceful_stopper: Callable[[], bool] | None = None,
) -> OSProcessManager:
    """Composition helper: pick the backend for this host."""
    backend = WindowsProcessManager if IS_WINDOWS else PosixProcessManager
    return backend(graceful_stopper=graceful_stopper)


def _write_log_line(log_file, message: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with contextlib.suppress(OSError, ValueError):
        log_file.write(
            f"[minecraft-monitor {stamp}] {message}\n".encode("utf-8", "replace")
        )
        log_file.flush()
