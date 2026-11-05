"""Run the dummy Minecraft server (test/dummy/minecraft_dummy.py) from tests.

- write_server_files(server_dir, game_port, rcon_port, ...) writes
  server.properties (+ eula.txt) the dummy reads.
- dummy_start_command() is the `settings.startCommand` for the dummy; use
  DUMMY_PROCESS_NAME as `settings.processName`.
- run_dummy_server(...) spawns it directly, waits until RCON answers and
  always kills it on exit.
- dummy_rcon(...) / wait_for_dummy(...) talk to a dummy started some other way
  (e.g. by the process manager).

The RCON helper here is deliberately independent of src/minecraft so a broken
client cannot make the fixture lie.
"""

import contextlib
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

DUMMY_SCRIPT = Path(__file__).resolve().parents[1] / "dummy" / "minecraft_dummy.py"
DUMMY_PROCESS_NAME = "minecraft_dummy"
DEFAULT_RCON_PASSWORD = "dummy-rcon"
CONSOLE_LOG_NAME = "dummy-console.log"
DEFAULT_PLAYERS = ("TestPlayer1", "TestPlayer2", "TestPlayer3")


def dummy_start_command() -> list[str]:
    """[sys.executable, <absolute path of minecraft_dummy.py>]."""
    return [sys.executable, str(DUMMY_SCRIPT)]


def _property_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    escaped = []
    for char in text:
        if char in "\\=:#!":
            escaped.append("\\" + char)
        elif char == "\n":
            escaped.append("\\n")
        elif not " " <= char <= "~":
            escaped.append(f"\\u{ord(char):04X}")
        else:
            escaped.append(char)
    return "".join(escaped)


def write_server_files(
    server_dir,
    game_port: int,
    rcon_port: int,
    rcon_password: str = DEFAULT_RCON_PASSWORD,
    **props,
) -> Path:
    """Write server.properties and eula.txt (eula=true) into server_dir.

    Defaults bind to 127.0.0.1 with RCON enabled. Extra properties are keyword
    arguments, underscores becoming dashes (``max_players=5`` ->
    ``max-players=5``); pass dotted keys with ``**{"query.port": 1}``. Booleans
    become true/false. Returns the server.properties path.
    """
    server_dir = Path(server_dir)
    server_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "server-ip": "127.0.0.1",
        "server-port": game_port,
        "enable-rcon": True,
        "rcon.port": rcon_port,
        "rcon.password": rcon_password,
        "max-players": 20,
        "motd": "A Minecraft Server",
        "online-mode": False,
        "white-list": False,
        "broadcast-rcon-to-ops": False,
    }
    values.update({key.replace("_", "-"): value for key, value in props.items()})
    lines = ["#Minecraft server properties", "#written by test.support.dummy_server"]
    lines += [f"{key}={_property_value(value)}" for key, value in values.items()]
    path = server_dir / "server.properties"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    eula = server_dir / "eula.txt"
    if not eula.exists():
        eula.write_text("#EULA accepted for tests\neula=true\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# Minimal independent RCON client (one packet in flight at a time, like
# vanilla requires)
# ----------------------------------------------------------------------


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("RCON connection closed")
        data += chunk
    return data


def _rcon_send(sock, request_id, packet_type, text):
    payload = text.encode("utf-8")
    sock.sendall(
        struct.pack("<iii", len(payload) + 10, request_id, packet_type)
        + payload
        + b"\0\0"
    )


def _rcon_read(sock):
    (length,) = struct.unpack("<i", _recv_exact(sock, 4))
    body = _recv_exact(sock, length)
    request_id, packet_type = struct.unpack_from("<ii", body)
    return request_id, packet_type, body[8:].rstrip(b"\0")


def dummy_rcon(
    port: int,
    password: str = DEFAULT_RCON_PASSWORD,
    command: str = "list",
    *,
    host: str = "127.0.0.1",
    timeout: float = 5.0,
) -> str:
    """Run one RCON command on a fresh connection and return the reply.

    Raises OSError (connection) or PermissionError (bad password).
    """
    with socket.create_connection((host, port), timeout=timeout) as sock:
        _rcon_send(sock, 1, 3, password)
        while True:
            request_id, packet_type, _ = _rcon_read(sock)
            if request_id == -1:
                raise PermissionError("RCON authentication failed")
            if request_id == 1 and packet_type == 2:
                break
        _rcon_send(sock, 2, 2, command)
        request_id, _, payload = _rcon_read(sock)
        fragments = [payload]
        if (
            len(payload) >= 4096
        ):  # vanilla splits long replies: collect until a sentinel
            _rcon_send(sock, 3, 100, "")
            while True:
                request_id, _, payload = _rcon_read(sock)
                if request_id == 3:
                    break
                fragments.append(payload)
        return b"".join(fragments).decode("utf-8", "replace")


def wait_for_dummy(
    rcon_port: int,
    password: str = DEFAULT_RCON_PASSWORD,
    *,
    host: str = "127.0.0.1",
    timeout: float = 20.0,
    process: subprocess.Popen | None = None,
) -> None:
    """Block until the dummy answers RCON `list`. If `process` is given and
    exits first, raise RuntimeError instead of waiting out the timeout."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"dummy server exited with code {process.returncode}")
        try:
            dummy_rcon(rcon_port, password, "list", host=host, timeout=1.0)
            return
        except PermissionError:
            raise
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(
        f"dummy server did not answer RCON on port {rcon_port}: {last_error}"
    )


# ----------------------------------------------------------------------
# Spawning
# ----------------------------------------------------------------------


class DummyServer:
    """Handle for a dummy started by run_dummy_server()."""

    def __init__(self, process, server_dir, game_port, rcon_port, rcon_password):
        self.process = process
        self.server_dir = Path(server_dir)
        self.game_port = game_port
        self.rcon_port = rcon_port
        self.rcon_password = rcon_password

    @property
    def pid(self) -> int:
        return self.process.pid

    def rcon(self, command: str, timeout: float = 5.0) -> str:
        return dummy_rcon(self.rcon_port, self.rcon_password, command, timeout=timeout)

    def set_players(self, names) -> str:
        """Replace the online roster (the `dummy players` hook)."""
        return self.rcon("dummy players " + ",".join(names))

    def console(self, line: str) -> None:
        """Type a command on the server console (stdin)."""
        self.process.stdin.write((line.rstrip("\n") + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def stop(self, timeout: float = 10.0) -> int:
        """Graceful `stop` on stdin; returns the exit code."""
        with contextlib.suppress(OSError, ValueError):
            self.console("stop")
        return self.process.wait(timeout)

    def log_text(self) -> str:
        path = self.server_dir / "logs" / "latest.log"
        return (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )

    def console_text(self) -> str:
        path = self.server_dir / CONSOLE_LOG_NAME
        return (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


@contextlib.contextmanager
def run_dummy_server(
    server_dir,
    *,
    game_port: int,
    rcon_port: int,
    rcon_password: str = DEFAULT_RCON_PASSWORD,
    players=None,
    env: dict | None = None,
    properties: dict | None = None,
    startup_timeout: float = 20.0,
):
    """Write server files, spawn the dummy (cwd=server_dir), wait until RCON
    answers (or, with enable_rcon=False, until "Done" is logged), yield a
    DummyServer handle, and always kill the child on exit.

    `players`: None keeps the dummy default (TestPlayer1..3); [] = nobody.
    `env`: extra DUMMY_* variables. `properties`: extra server.properties.
    stdout/stderr go to <server_dir>/dummy-console.log.
    """
    server_dir = Path(server_dir)
    write_server_files(
        server_dir, game_port, rcon_port, rcon_password, **(properties or {})
    )
    child_env = dict(os.environ)
    if players is not None:
        child_env["DUMMY_PLAYERS"] = ",".join(players)
    child_env.update({key: str(value) for key, value in (env or {}).items()})
    timeout = startup_timeout + float(child_env.get("DUMMY_STARTUP_DELAY", 0) or 0)
    with open(server_dir / CONSOLE_LOG_NAME, "wb") as console:
        process = subprocess.Popen(
            dummy_start_command(),
            cwd=server_dir,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=console,
            stderr=subprocess.STDOUT,
            **_popen_kwargs(),
        )
        handle = DummyServer(process, server_dir, game_port, rcon_port, rcon_password)
        try:
            try:
                if _rcon_enabled(properties):
                    wait_for_dummy(
                        rcon_port, rcon_password, timeout=timeout, process=process
                    )
                else:
                    _wait_for_done(handle, timeout)
            except (RuntimeError, TimeoutError) as exc:
                raise RuntimeError(
                    f"{exc}\n--- dummy console ---\n{handle.console_text()}"
                ) from exc
            yield handle
        finally:
            _kill(process)


def _rcon_enabled(properties: dict | None) -> bool:
    props = {key.replace("_", "-"): value for key, value in (properties or {}).items()}
    return str(props.get("enable-rcon", True)).lower() == "true"


def _wait_for_done(handle: DummyServer, timeout: float) -> None:
    """Readiness without RCON: "Done" logged (the game port is bound first)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if handle.process.poll() is not None:
            raise RuntimeError(
                f"dummy server exited with code {handle.process.returncode}"
            )
        if "Done (" in handle.log_text():
            return
        time.sleep(0.05)
    raise TimeoutError("dummy server did not log Done")


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    if process.stdin:
        with contextlib.suppress(OSError):
            process.stdin.close()
