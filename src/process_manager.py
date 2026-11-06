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
- Identity: PID plus a start key that does not move when the wall clock is
  stepped (Linux: the boot-relative start time from ``/proc/<pid>/stat``;
  elsewhere the absolute creation time). Scans, liveness checks and stops
  read it fresh from the same source.
- Unknown is not gone: when the OS cannot be asked (EMFILE, ENFILE, ...),
  ``observe()``, ``identity_is_running()`` and ``find_process_identity()``
  raise ``ProcessStateUnknown`` and the caller keeps its last state.
- Discovery: a live process whose cwd is ``serverDir`` and whose name, exe or
  command line contains ``settings.processName``. Launch wrappers are normal
  (Forge's ``run.sh`` is ``sh`` running ``java`` as a child), so the server is
  the tracked process *and its descendants*: they are remembered, the tracked
  server runs while any of them lives (a launcher can die before its JVM has
  saved), and none of them is ever reported as another server.
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
import ntpath
import os
import shlex
import subprocess
import sys
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

_INFO_ATTRS = ["name", "exe", "cmdline"]
# cmd.exe cannot pass these through a quoted list-form .bat/.cmd element.
_CMD_UNQUOTABLE = frozenset('"%')

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


def build_launch_command(command, windows: bool = IS_WINDOWS, server_dir=None):
    """Popen ``args`` for ``settings.startCommand``.

    A list is used as-is. A string is split with ``shlex`` on POSIX and passed
    through unchanged on Windows (CreateProcess parses it). On Windows:

    - a ``.bat``/``.cmd`` program runs through ``cmd.exe /d /s /c``. A list
      is double-quoted element by element, so ``& | < > ^`` stay literal;
      elements with ``"`` or ``%`` are refused (cmd.exe cannot pass them),
      while a string is written in cmd.exe quoting by the admin;
    - a relative program with a path separator, or one that exists in
      ``server_dir``, is resolved against ``server_dir``: CreateProcess
      searches the monitor's directory, not the child's cwd (POSIX resolves
      it after changing into ``server_dir`` anyway).

    Raises:
        ValueError: the command is empty or cannot be parsed.
    """
    if isinstance(command, (list, tuple)):
        return _list_command([str(part) for part in command], windows, server_dir)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("minecraft.startCommand is empty")
    command = command.strip()
    if not windows:
        args = shlex.split(command)
        if not args:
            raise ValueError("minecraft.startCommand is empty")
        return args
    program, rest = _split_windows_program(command)
    if _is_batch_file(program):
        return _cmd_wrapper(command)
    resolved = _resolve_windows_program(program, server_dir)
    if resolved == program:
        return command
    return _quote_windows(resolved) + rest


def _list_command(args: list, windows: bool, server_dir):
    if not args or not args[0].strip():
        raise ValueError("minecraft.startCommand is empty")
    if not windows:
        return args
    if _is_batch_file(args[0]):
        return _cmd_wrapper(" ".join(_cmd_quote(arg) for arg in args))
    return [_resolve_windows_program(args[0], server_dir), *args[1:]]


def _cmd_quote(arg: str) -> str:
    """One element of a cmd.exe line, double-quoted so ``& | < > ^`` stay
    literal. cmd.exe expands ``%`` even inside quotes and cannot escape a
    quote there, so such elements are refused."""
    if _CMD_UNQUOTABLE.intersection(arg) or any(ord(c) < 32 for c in arg):
        raise ValueError(
            f"minecraft.startCommand: cmd.exe cannot pass {arg!r} (it contains "
            '" or %, or a control character). Write the command as one string, '
            "quoted for cmd.exe, instead of a list."
        )
    # Backslashes before the closing quote would escape it for the program.
    trailing = len(arg) - len(arg.rstrip("\\"))
    return '"' + arg + "\\" * trailing + '"'


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


def _split_windows_program(command: str) -> tuple[str, str]:
    """The program of a Windows command line, and the rest (separator kept)."""
    if command.startswith('"'):
        end = command.find('"', 1)
        if end < 0:
            return command[1:], ""
        return command[1:end], command[end + 1 :]
    program = command.split(None, 1)[0]
    return program, command[len(program) :]


def _resolve_windows_program(program: str, server_dir) -> str:
    if not server_dir or not program or ntpath.isabs(program):
        return program
    has_separator = "/" in program or "\\" in program
    if has_separator or os.path.isfile(os.path.join(server_dir, program)):
        return ntpath.normpath(ntpath.join(server_dir, program))
    return program


def _quote_windows(path: str) -> str:
    return f'"{path}"' if any(c in path for c in " \t") else path


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
# Process probes: identity, liveness and discovery
# ----------------------------------------------------------------------


class ProcessStateUnknown(Exception):
    """The OS could not be asked about a process (EMFILE, ENFILE, EIO, ...).

    It means "unknown, keep the last state": never "exited", never "absent".
    """


_LINUX = sys.platform.startswith("linux")
_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if _LINUX else 100


def _unknown(error: Exception) -> ProcessStateUnknown:
    return ProcessStateUnknown(f"cannot inspect processes: {error}")


def _probe_linux(pid: int):
    """(start key, zombie) from /proc/<pid>/stat; None: no such process.

    The key is the start time in seconds since boot. Unlike psutil's
    create_time() it does not move when the wall clock is stepped (NTP,
    resume, WSL, a manual date change)."""
    path = f"{getattr(psutil, 'PROCFS_PATH', '/proc')}/{pid}/stat"
    try:
        with open(path, "rb") as file:
            data = file.read()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except PermissionError:
        return None, False  # it exists; its start time is unknown
    except OSError as e:
        raise _unknown(e) from e
    fields = data[data.rfind(b")") + 2 :].split()
    try:
        return int(fields[19]) / _CLOCK_TICKS, fields[0] in (b"Z", b"X", b"x")
    except (IndexError, ValueError):
        return None, False


def _probe_psutil(pid: int):
    """(creation time, zombie): absolute, and stable off Linux."""
    try:
        process = psutil.Process(pid)
        return process.create_time(), process.status() == psutil.STATUS_ZOMBIE
    except psutil.ZombieProcess:
        return None, True
    except (psutil.NoSuchProcess, ValueError):
        return None
    except psutil.AccessDenied:
        return None, False
    except (psutil.Error, OSError) as e:
        raise _unknown(e) from e


def _probe(pid: int):
    """(start key or None, zombie), or None when the PID does not exist.
    Raises ProcessStateUnknown when the OS cannot be asked."""
    return _probe_linux(pid) if _LINUX else _probe_psutil(pid)


def process_start_key(pid: int) -> float | None:
    """The identity key of a live PID (see the module docstring)."""
    probe = _probe(int(pid))
    return probe[0] if probe is not None else None


def _identity_for_pid(pid: int) -> ProcessIdentity:
    try:
        key = process_start_key(pid)
    except ProcessStateUnknown:
        key = None
    return ProcessIdentity(int(pid), key)


def _liveness(identity: ProcessIdentity) -> bool:
    """The identity's process exists, is the same process and is no zombie.
    Raises ProcessStateUnknown when the OS cannot be asked."""
    probe = _probe(identity.pid)
    if probe is None or probe[1]:
        return False
    return identity.matches(ProcessIdentity(identity.pid, probe[0]))


def _is_alive(process: psutil.Process) -> bool:
    """For psutil objects (tree members): psutil's own identity check is
    clock-step-proof. An OS error counts as alive: unknown is never exited."""
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return True
    except psutil.Error:
        return False


def _info_of(process) -> dict:
    """name/exe/cmdline ({} once gone). Raises ProcessStateUnknown on OS errors."""
    try:
        return process.as_dict(attrs=_INFO_ATTRS, ad_value=None)
    except psutil.Error:
        return {}
    except OSError as e:
        raise _unknown(e) from e


def _servers_among(processes, target: str) -> list:
    """The processes that match processName (all of them when unknown)."""
    try:
        return [p for p in processes if match_rank(_info_of(p), target)]
    except ProcessStateUnknown:
        return list(processes)


def _cwd_matches(process, server_dir: str) -> bool:
    try:
        cwd = process.cwd()
    except psutil.Error:
        return False
    except OSError as e:
        raise _unknown(e) from e
    return bool(cwd) and normalized_dir(cwd) == server_dir


def _with_descendants(processes) -> set:
    tree = set(processes)
    for process in list(tree):
        if not _is_alive(process):
            continue
        with contextlib.suppress(psutil.Error, OSError):
            tree.update(process.children(recursive=True))
    return tree


def _start_order(process) -> float:
    try:
        probe = _probe(process.pid)
    except ProcessStateUnknown:
        probe = None
    key = probe[0] if probe is not None else None
    return key if key is not None else float("inf")


def _signal_all(processes, method: str):
    # Oldest first: a wrapper with a restart loop dies before it can respawn.
    for process in sorted(processes, key=_start_order):
        with contextlib.suppress(psutil.Error, OSError):
            getattr(process, method)()


def _fresh_process(pid: int):
    """A new psutil.Process (nothing cached from a reused PID), or None."""
    try:
        return psutil.Process(pid)
    except (psutil.Error, ValueError):
        return None
    except OSError as e:
        raise _unknown(e) from e


def _may_match_linux(pid: int, target: str) -> bool:
    """Cheap prefilter: does processName appear in comm, cmdline or exe?"""
    base = f"{getattr(psutil, 'PROCFS_PATH', '/proc')}/{pid}"
    text = b""
    for name in ("comm", "cmdline"):
        try:
            with open(f"{base}/{name}", "rb") as file:
                text += file.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return False
        except OSError as e:
            raise _unknown(e) from e
    with contextlib.suppress(OSError):  # other users' exe links are unreadable
        text += os.fsencode(os.readlink(f"{base}/exe"))
    return target.encode() in text.lower()


def _scan_candidate(pid: int, server_dir: str, target: str):
    """(rank, start key, identity) for a matching process, else None.
    Raises ProcessStateUnknown when the OS cannot be asked."""
    if _LINUX and not _may_match_linux(pid, target):
        return None
    process = _fresh_process(pid)
    if process is None:
        return None
    rank = match_rank(_info_of(process), target)
    if not rank:
        return None
    probe = _probe(pid)
    if probe is None or probe[1] or not _cwd_matches(process, server_dir):
        return None
    key = probe[0]
    return rank, key if key is not None else float("inf"), ProcessIdentity(pid, key)


def scan_for_server(server_dir: str, target: str) -> ProcessIdentity | None:
    """Best match in ``server_dir``: program matches before argument matches,
    then the oldest process (a launcher before the JVM it spawned).

    Reads fresh process objects and start keys every time (psutil's
    process_iter() cache keeps creation times from before a clock step).
    Raises ProcessStateUnknown when the process table cannot be read.
    """
    own_pid = os.getpid()
    try:
        pids = psutil.pids()
    except OSError as e:
        raise _unknown(e) from e
    best = None
    for pid in pids:
        if pid == own_pid:
            continue
        candidate = _scan_candidate(pid, server_dir, target)
        if candidate is not None and (best is None or candidate[:2] < best[:2]):
            best = candidate
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
    LEFTOVER_GRACE = 1.0  # for leftovers of a stopped server, before/after SIGTERM

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
        self._members: set = set()  # descendants of the tracked process seen
        self._stopping: ProcessIdentity | None = None  # identity being stopped
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
        """True while the process lives, or (for the tracked identity) a
        remembered descendant matching processName does: a JVM whose launcher
        is gone. Other leftovers (a tunnel or backup helper the launch script
        started) never count. Raises ProcessStateUnknown."""
        if identity is None:
            return False
        return self._root_alive(identity) or self._members_alive(identity)

    def _root_alive(self, identity: ProcessIdentity) -> bool:
        popen = self._popen_for(identity)
        self._reap(popen)
        if _liveness(identity):
            return True
        self._reap(popen)  # it may have exited between the two checks
        return False

    def _members_of(self, identity: ProcessIdentity | None) -> list:
        with self._lock:
            if identity is None or not identity.matches(self._identity):
                return []
            return list(self._members)

    def _members_alive(self, identity: ProcessIdentity) -> bool:
        """A remembered descendant matching processName still lives."""
        target = str(settings.processName or "")
        return bool(_servers_among(self._live_members(identity), target))

    def _live_members(self, identity: ProcessIdentity | None) -> list:
        return [member for member in self._members_of(identity) if _is_alive(member)]

    def _remember(self, processes, identity: ProcessIdentity | None = None):
        """Record descendants of the tracked process (the root excluded)."""
        with self._lock:
            current = self._identity
            if current is None or (
                identity is not None and not identity.matches(current)
            ):
                return
            self._members.update(p for p in processes if p.pid != current.pid)

    def started_at(self, identity: ProcessIdentity) -> float | None:
        """Wall-clock creation time, for the dashboard's startedAt only."""
        try:
            return psutil.Process(identity.pid).create_time()
        except (psutil.Error, OSError, ValueError):
            return None

    def find_process_identity(self) -> ProcessIdentity | None:
        """A live (non-zombie) process whose cwd is settings.serverDir and whose
        name/cmdline contains settings.processName; prefers the tracked one
        and never reports a descendant of it as another server; never this
        monitor process. Raises ProcessStateUnknown when the OS cannot be
        asked."""
        server_dir = normalized_dir(settings.serverDir)
        target = str(settings.processName or "").strip().lower()
        if not server_dir or not target:
            return None
        tracked = self.tracked_identity
        if tracked is not None and self._is_tracked_server(tracked, server_dir, target):
            return tracked
        found = scan_for_server(server_dir, target)
        if found is not None and any(
            member.pid == found.pid and _is_alive(member)
            for member in self._members_of(tracked)
        ):
            return tracked  # part of the tracked server, not a replacement
        return found

    def _is_tracked_server(
        self, tracked: ProcessIdentity, server_dir: str, target: str
    ) -> bool:
        # The tracked process may be a wrapper (sh run.sh, cmd /c run.bat)
        # whose child is the JVM; the tree is the server.
        if tracked.pid == os.getpid() or not self._root_alive(tracked):
            return False
        try:
            process = psutil.Process(tracked.pid)
            tree = [process, *process.children(recursive=True)]
        except psutil.Error:
            return False
        except OSError as e:
            raise _unknown(e) from e
        if not _cwd_matches(process, server_dir):
            return False
        return any(match_rank(_info_of(member), target) for member in tree)

    def observe(self) -> ProcessSnapshot:
        """I/O-only observation for the lifecycle supervisor (one per scan).

        Raises ProcessStateUnknown when the OS cannot be asked; the runtime
        then keeps the last state."""
        tracked = self.tracked_identity
        running = tracked is not None and self._tracked_running(tracked)
        return ProcessSnapshot(
            tracked=tracked,
            tracked_running=running,
            found=self.find_process_identity(),
        )

    def _tracked_running(self, tracked: ProcessIdentity) -> bool:
        root_alive = self._root_alive(tracked)
        if root_alive:
            self._remember_descendants(tracked)
        members = self._live_members(tracked)
        if not root_alive and not members:
            return False
        return not self._launcher_outlived_server(tracked, root_alive, members)

    def _remember_descendants(self, tracked: ProcessIdentity):
        try:
            children = psutil.Process(tracked.pid).children(recursive=True)
        except (psutil.Error, OSError):
            return
        self._remember(children, tracked)

    def _tree_has_server(
        self, tracked: ProcessIdentity, root_alive: bool, members: list, target: str
    ) -> bool | None:
        """Whether the tracked process, a live descendant or a remembered one
        matches processName (None: unknown)."""
        try:
            tree = list(members)
            if root_alive:
                root = psutil.Process(tracked.pid)
                tree += [root, *root.children(recursive=True)]
            return any(_is_alive(p) and match_rank(_info_of(p), target) for p in tree)
        except (psutil.Error, OSError, ProcessStateUnknown):
            return None

    def _launcher_outlived_server(
        self, tracked: ProcessIdentity, root_alive: bool, members: list
    ) -> bool:
        """True once no process matching processName has been left in the
        tracked tree for SERVER_GONE_SCANS scans in a row while other
        processes of it linger: a launcher in ``pause``/``read``, or a helper
        the launch script started in the background (a tunnel, a backup
        loop), still a child or orphaned after the launcher exited.

        While the launcher lives, only after the tree has had a server
        process (it may take a moment to start the JVM). The first such scan
        closes our stdin pipe, so a launcher waiting for input returns.
        """
        has_server = self._tree_has_server(
            tracked, root_alive, members, str(settings.processName or "")
        )
        with self._lock:
            if has_server is None or not tracked.matches(self._identity):
                return False
            if has_server:
                self._server_seen, self._missing_scans = True, 0
                return False
            if root_alive and not self._server_seen:
                return False
            self._missing_scans += 1
            missing = self._missing_scans
        if missing == 1:
            logger.info(
                "The server process is gone but processes started with it remain "
                f"(pid {tracked.pid} and its descendants); closing its console input"
            )
            popen = self._popen_for(tracked)
            if popen is not None:
                self._close_stdin(popen)
        elif missing == self.SERVER_GONE_SCANS:
            logger.warning(
                "Only non-server processes remain from the server's launch (pid "
                f"{tracked.pid} and its descendants); treating the server as stopped"
            )
        return missing >= self.SERVER_GONE_SCANS

    def _reset_tree_watch_locked(self):
        self._server_seen = False
        self._missing_scans = 0
        self._members = set()

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
        problem = self._server_dir_problem(server_dir)
        if problem is not None:
            logger.error(f"Cannot launch the Minecraft server: {problem}")
            return None
        try:
            args = build_launch_command(settings.startCommand, self.windows, server_dir)
        except ValueError as e:
            logger.error(f"Cannot launch the Minecraft server: {e}")
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

    def _server_dir_problem(self, server_dir) -> str | None:
        if not server_dir or not os.path.isdir(server_dir):
            return f"no directory {server_dir!r}"
        if not self.windows and not os.access(server_dir, os.R_OK | os.X_OK):
            return (
                f"the server folder {server_dir} is not accessible to the "
                "monitor's user (it needs read and execute permission)"
            )
        return None

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
            hint = _launch_failure_hint(e, cwd, self.windows)
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
        The tree is the tracked process plus its remembered descendants, so a
        launcher that dies first does not end the stop while its JVM saves.
        False when the process state cannot be read (never "gone").
        """
        with self._lock:
            current = self._identity
        if current is None:
            return False
        if expected is not None and not current.matches(expected):
            return False

        popen = self._popen_for(current)
        self._stopping = current
        try:
            tree = self._live_tree(current, popen)
            if tree and not self._stop_tree(tree, current.pid, popen, force):
                return False
        except ProcessStateUnknown as e:
            logger.warning(f"Not stopping the server now: {e}")
            return False
        finally:
            self._stopping = None
        self.clear_identity(current)
        return True

    def _live_tree(self, identity: ProcessIdentity, popen) -> set:
        """The tracked process (if it is still that process), its descendants
        and remembered descendants that outlived it, alive ones only.
        Raises ProcessStateUnknown."""
        tree = {member for member in self._members_of(identity) if _is_alive(member)}
        if self._root_alive(identity):
            root = _fresh_process(identity.pid)
            if root is not None:
                tree.add(root)
        tree = _with_descendants(tree)
        self._remember(tree, identity)
        return {process for process in tree if _is_alive(process)}

    def _stop_tree(self, tree: set, pid: int, popen, force: bool) -> bool:
        timeout = _seconds(settings.stopTimeout, DEFAULT_STOP_TIMEOUT)
        term_wait = max(self.TERMINATE_TIMEOUT, timeout)  # never kill mid-save
        channel = self._request_graceful_stop(pid, popen)
        if channel is None:
            return self._stop_unrequested(tree, popen, pid, force)
        if not self._wait_graceful(tree, popen, channel, timeout, pid):
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
        """Wait until the server is gone; returns what still keeps it alive.

        Descendants started meanwhile join the tree and are remembered, so
        detection reports the server running until it is gone. The server is
        gone once no tree process matching processName is alive (if the tree
        ever had one; otherwise once the whole tree is gone). What it leaves
        behind (a launcher in ``pause``, a tunnel or backup helper) is then
        ended best-effort, without waiting ``stopTimeout`` for it. A process
        whose state cannot be read counts as alive.
        """
        target = str(settings.processName or "")
        deadline = time.monotonic() + max(0.0, timeout)
        seen_server = self._server_seen
        while True:
            self._reap(popen)
            tree.update(_with_descendants(tree))
            if self._stopping is not None:
                self._remember(tree, self._stopping)
            alive = [process for process in tree if _is_alive(process)]
            servers = _servers_among(alive, target)
            seen_server = seen_server or bool(servers)
            if not alive:
                return []
            if not servers and popen is not None:
                self._close_stdin(popen)  # Forge's run.bat ends with `pause`
            if seen_server and not servers:
                self._dismiss_leftovers(alive, popen)
                return []
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return servers if seen_server else alive
            time.sleep(min(self.WAIT_INTERVAL, remaining))

    def _dismiss_leftovers(self, leftovers: list, popen):
        """The server is gone: end what it left behind without waiting
        ``stopTimeout`` for it. Best-effort; the stop has succeeded anyway."""
        leftovers = self._await_exit(leftovers, self.LEFTOVER_GRACE, popen)
        if not leftovers:
            return
        pids = sorted(process.pid for process in leftovers)
        logger.info(f"The server stopped; ending leftover processes {pids}")
        _signal_all(leftovers, "terminate")
        leftovers = self._await_exit(leftovers, self.LEFTOVER_GRACE, popen)
        _signal_all(leftovers, "kill")

    def _await_exit(self, processes: list, timeout: float, popen) -> list:
        deadline = time.monotonic() + timeout
        while True:
            self._reap(popen)
            processes = [process for process in processes if _is_alive(process)]
            if not processes or time.monotonic() >= deadline:
                return processes
            time.sleep(self.WAIT_INTERVAL)

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


def _launch_failure_hint(error: Exception, cwd: str, windows: bool) -> str:
    if windows or not isinstance(error, PermissionError):
        return ""
    if error.filename is not None and normalized_dir(error.filename) == normalized_dir(
        cwd
    ):
        return " (the server folder is not accessible to the monitor's user)"
    return " (is the start script executable? try chmod +x)"
