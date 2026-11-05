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
- Stop: ``stop`` on our stdin pipe (or the RCON ``graceful_stopper`` for an
  adopted server), wait ``stopTimeout`` for the whole tree, then terminate
  (SIGTERM runs the JVM shutdown hook, which saves the worlds), then kill.
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
    TERMINATE_TIMEOUT = 30.0  # after SIGTERM (TerminateProcess on Windows)
    KILL_TIMEOUT = 10.0  # after SIGKILL
    WAIT_INTERVAL = 0.2

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
            self._identity = identity
            self._write_pid_file(identity)

    def clear_identity(self, expected: ProcessIdentity | None) -> bool:
        """Compare-and-clear: never forget a replacement process."""
        with self._lock:
            current = self._identity
            if current is None or expected is None or not current.matches(expected):
                return False
            self._identity = None
            self._remove_pid_file()
            if self._popen is not None and self._popen.pid == current.pid:
                self._release_popen_locked()
            return True

    def identity_is_running(self, identity: ProcessIdentity | None) -> bool:
        if identity is None:
            return False
        self._reap(self._popen_for(identity))
        return _identity_is_live(identity)

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
        """I/O-only observation for the lifecycle supervisor."""
        tracked = self.tracked_identity
        return ProcessSnapshot(
            tracked=tracked,
            tracked_running=self.identity_is_running(tracked),
            found=self.find_process_identity(),
        )

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

    def terminate_process(self, expected: ProcessIdentity | None = None) -> bool | None:
        """Graceful stop chain. True=gone, False=still alive, None=pending.

        Blocking (runs on the runtime's process worker): ``stop`` on our
        stdin, else ``graceful_stopper()``; wait up to ``stopTimeout`` for the
        process tree; terminate the tree and wait ``TERMINATE_TIMEOUT``; kill.
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
        if not self._stop_tree(root, popen):
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

    def _stop_tree(self, root: psutil.Process, popen) -> bool:
        tree = _with_descendants({root})
        timeout = _seconds(settings.stopTimeout, DEFAULT_STOP_TIMEOUT)
        if self._request_graceful_stop(root.pid, popen):
            term_wait = self.TERMINATE_TIMEOUT
        elif self.windows:
            logger.warning(
                f"Could not ask server process {root.pid} to stop (no console pipe, "
                f"RCON unavailable); waiting {timeout:g}s before terminating it"
            )
            term_wait = self.TERMINATE_TIMEOUT
        else:
            # SIGTERM runs the JVM shutdown hook, which saves the worlds.
            logger.warning(
                f"Could not send 'stop' to server process {root.pid} (no console "
                "pipe, RCON unavailable); sending SIGTERM"
            )
            return self._force_stop(tree, popen, max(self.TERMINATE_TIMEOUT, timeout))

        if not self._wait_for_exit(tree, timeout, popen):
            return True
        logger.warning(f"The server did not stop within {timeout:g}s; terminating it")
        return self._force_stop(tree, popen, term_wait)

    def _request_graceful_stop(self, pid: int, popen) -> bool:
        if popen is not None and self._send_console_stop(popen):
            logger.info(f"Sent 'stop' to the server console (pid {pid})")
            return True
        stopper = self._graceful_stopper
        if stopper is None:
            return False
        try:
            sent = bool(stopper())
        except Exception:
            logger.exception("The graceful stop request (RCON) failed")
            return False
        if sent:
            logger.info(f"Requested a graceful stop over RCON (pid {pid})")
        else:
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
