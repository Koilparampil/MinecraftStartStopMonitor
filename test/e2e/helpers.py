"""Helpers for the e2e suite: a fully sandboxed monitor process.

`run_monitor()` writes a settings file with free ports into a tmp dir, points
`serverDir` at a tmp server prepared for the dummy Minecraft server, launches
`python -m src.main` with cwd inside the tmp dir (main writes cwd-relative
logs), waits for /healthz and tears down only what it started: the monitor
and dummy processes that are its children or run inside our unique tmp
server dir. Nothing else on the host is ever touched.
"""

import contextlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psutil
import requests
import yaml

from test.support import free_tcp_ports
from test.support.dummy_server import (
    DEFAULT_RCON_PASSWORD,
    DUMMY_PROCESS_NAME,
    dummy_start_command,
    write_server_files,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
USERNAME = "admin"
STARTUP_TIMEOUT = 30


@dataclass
class MonitorApp:
    process: subprocess.Popen
    base_url: str
    password: str
    server_dir: Path
    data_dir: Path
    work_dir: Path
    game_port: int
    rcon_port: int
    output_path: Path

    def output_tail(self, lines: int = 80) -> str:
        try:
            text = self.output_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def dummy_processes(self) -> list[psutil.Process]:
        return _dummies_in(self.server_dir)

    def banned_names(self) -> list[str]:
        path = self.server_dir / "banned-players.json"
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [str(entry.get("name")) for entry in entries]


def _settings(app_dirs, ports, password, options) -> dict:
    server_dir, data_dir = app_dirs
    game_port, rcon_port, web_port = ports
    return {
        "minecraft": {
            "serverDir": str(server_dir),
            "startCommand": dummy_start_command(),
            "host": "127.0.0.1",
            "port": game_port,
            "rconPort": rcon_port,
            "rconPassword": DEFAULT_RCON_PASSWORD,
            "pollingRate": options.get("polling_rate", 1),
            "stopTimeout": 20,
            "processName": DUMMY_PROCESS_NAME,
        },
        "web": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": web_port,
            "username": USERNAME,
            "password": password,
            "controlServer": True,
            "showServerIP": False,
            "console": True,
            "playerAvatars": False,
        },
        "features": {
            "playerTracking": True,
            "autoStart": options.get("auto_start", True),
            "autoStop": True,
        },
        "autoStart": {"bindHost": "127.0.0.1"},
        "autoStop": {"stopDelay": options.get("auto_stop_delay", 600)},
        "monitor": {"dataDir": str(data_dir)},
        "security": {
            "sessionSecretKey": secrets.token_hex(32),
            "rateLimitEnabled": True,
            "rateLimitRequests": 1000,
            "rateLimitWindow": 60,
        },
    }


def _wait_for_health(app: MonitorApp) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last_error = None
    while time.monotonic() < deadline:
        if app.process.poll() is not None:
            raise RuntimeError(
                f"monitor exited early ({app.process.returncode}):\n{app.output_tail()}"
            )
        try:
            if requests.get(app.base_url + "/healthz", timeout=1).status_code == 200:
                return
        except requests.RequestException as error:
            last_error = error
        time.sleep(0.2)
    raise RuntimeError(
        f"monitor not healthy in time ({last_error}):\n{app.output_tail()}"
    )


def _dummies_in(server_dir: Path) -> list[psutil.Process]:
    """Dummy servers whose cwd is our (unique, tmp) server dir."""
    target = os.path.realpath(server_dir)
    found = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if DUMMY_PROCESS_NAME in cmdline and os.path.realpath(proc.cwd()) == target:
                found.append(proc)
        except (psutil.Error, OSError):
            continue
    return found


def _terminate(processes, timeout=5.0) -> None:
    alive = []
    for proc in processes:
        with contextlib.suppress(psutil.Error):
            proc.terminate()
            alive.append(proc)
    _, still_alive = psutil.wait_procs(alive, timeout=timeout)
    for proc in still_alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(still_alive, timeout=timeout)


def _stop_monitor(app: MonitorApp) -> None:
    try:
        children = psutil.Process(app.process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    if app.process.poll() is None:
        if os.name == "nt":
            app.process.terminate()
        else:
            app.process.send_signal(signal.SIGINT)  # main's clean shutdown path
        try:
            app.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.process.kill()
            app.process.wait(timeout=5)
    # The monitor deliberately leaves the Minecraft server running (it is
    # adopted on the next start); in tests we clean it up ourselves.
    leftovers = {proc.pid: proc for proc in children}
    leftovers.update({proc.pid: proc for proc in _dummies_in(app.server_dir)})
    _terminate([proc for proc in leftovers.values() if _is_running(proc)])


def _is_running(proc: psutil.Process) -> bool:
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


@contextlib.contextmanager
def run_monitor(tmp_dir: Path, dummy_env=None, **options) -> Iterator[MonitorApp]:
    """Start a sandboxed monitor; options: auto_stop_delay, polling_rate, auto_start."""
    tmp_dir = Path(tmp_dir)
    server_dir, data_dir, work_dir = (
        tmp_dir / name for name in ("server", "data", "cwd")
    )
    for directory in (data_dir, work_dir):
        directory.mkdir(parents=True, exist_ok=True)
    ports = free_tcp_ports(3)
    write_server_files(server_dir, ports[0], ports[1], motd="E2E Test Server")
    password = secrets.token_urlsafe(12)
    settings_path = tmp_dir / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(_settings((server_dir, data_dir), ports, password, options)),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "MINECRAFT_MONITOR_SETTINGS": str(settings_path),
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(PROJECT_ROOT), os.environ.get("PYTHONPATH")])
        ),
        "PYTHONUNBUFFERED": "1",
        **(dummy_env or {}),
    }
    output_path = tmp_dir / "monitor-output.log"
    with open(output_path, "wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "src.main"],
            cwd=work_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        app = MonitorApp(
            process=process,
            base_url=f"http://127.0.0.1:{ports[2]}",
            password=password,
            server_dir=server_dir,
            data_dir=data_dir,
            work_dir=work_dir,
            game_port=ports[0],
            rcon_port=ports[1],
            output_path=output_path,
        )
        try:
            _wait_for_health(app)
            yield app
        finally:
            _stop_monitor(app)


# ----------------------------------------------------------------------
# HTTP client for the dashboard API
# ----------------------------------------------------------------------


class MonitorSession:
    """Logged-in requests session (CSRF handled like the browser does)."""

    def __init__(self, app: MonitorApp):
        self.app = app
        self.http = requests.Session()
        self.csrf = None

    def login(self) -> "MonitorSession":
        base = self.app.base_url
        page = self.http.get(base + "/login", timeout=5)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = self.http.post(
            base + "/login",
            data={
                "username": USERNAME,
                "password": self.app.password,
                "csrf_token": token,
            },
            allow_redirects=False,
            timeout=5,
        )
        assert response.status_code == 302, response.text
        dashboard = self.http.get(base + "/", timeout=5)
        self.csrf = re.search(
            r'name="csrf-token" content="([^"]+)"', dashboard.text
        ).group(1)
        return self

    def post(self, path: str, **data) -> dict:
        response = self.http.post(
            self.app.base_url + path, data={**data, "csrf_token": self.csrf}, timeout=30
        )
        return response.json()

    def snapshot(self) -> dict:
        return self.post("/action", action="getStatus")["data"]

    def wait_for(self, predicate, timeout=60.0, message="state not reached") -> dict:
        deadline = time.monotonic() + timeout
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            if predicate(snapshot):
                return snapshot
            time.sleep(0.3)
        raise AssertionError(
            f"{message}; last snapshot: {snapshot}\n{self.app.output_tail()}"
        )

    @contextlib.contextmanager
    def stream(self) -> Iterator[Iterator[dict]]:
        """Iterator of committed snapshots from /stream (SSE)."""
        with self.http.get(
            self.app.base_url + "/stream", stream=True, timeout=(5, 60)
        ) as response:
            response.raise_for_status()
            yield (
                json.loads(line[6:])["data"]
                for line in response.iter_lines(decode_unicode=True)
                if line and line.startswith("data: ")
            )


def is_online(snapshot: dict) -> bool:
    return snapshot["state"] == "RUNNING" and bool(snapshot.get("ready"))


def is_stopped(snapshot: dict) -> bool:
    return snapshot["state"] == "STOPPED"


# ----------------------------------------------------------------------
# Minimal Minecraft protocol client (independent of src/minecraft)
# ----------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    value &= 0xFFFFFFFF
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _string(text: str) -> bytes:
    data = text.encode("utf-8")
    return _varint(len(data)) + data


def _packet(packet_id: int, payload: bytes) -> bytes:
    body = _varint(packet_id) + payload
    return _varint(len(body)) + body


def _read_varint(sock: socket.socket) -> int:
    result = 0
    for shift in range(0, 35, 7):
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("connection closed while reading a VarInt")
        result |= (byte[0] & 0x7F) << shift
        if not byte[0] & 0x80:
            return result
    raise ValueError("VarInt too long")


def _read_packet(sock: socket.socket) -> tuple[int, bytes]:
    length = _read_varint(sock)
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("connection closed mid-packet")
        data += chunk
    packet_id, offset = 0, 0
    for shift in range(0, 35, 7):
        byte = data[offset]
        offset += 1
        packet_id |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
    return packet_id, data[offset:]


def _read_string(payload: bytes) -> str:
    length, offset = 0, 0
    for shift in range(0, 35, 7):
        byte = payload[offset]
        offset += 1
        length |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
    return payload[offset : offset + length].decode("utf-8")


def _handshake(port: int, next_state: int, protocol: int = 763) -> bytes:
    payload = (
        _varint(protocol)
        + _string("127.0.0.1")
        + port.to_bytes(2, "big")
        + _varint(next_state)
    )
    return _packet(0x00, payload)


def status_ping(port: int, timeout: float = 5.0) -> dict:
    """Server-list ping: the parsed Status Response JSON."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(_handshake(port, 1) + _packet(0x00, b""))
        packet_id, payload = _read_packet(sock)
        assert packet_id == 0x00, packet_id
        return json.loads(_read_string(payload))


def join_attempt(port: int, name: str, timeout: float = 5.0) -> str:
    """Handshake + Login Start; returns the disconnect reason JSON text."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        login_start = _string(name) + b"\x00"  # 1.20.1: name, has_uuid=false
        sock.sendall(_handshake(port, 2) + _packet(0x00, login_start))
        packet_id, payload = _read_packet(sock)
        assert packet_id == 0x00, f"expected Login Disconnect, got 0x{packet_id:02x}"
        return _read_string(payload)


def port_accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def strip_codes(text: str) -> str:
    return re.sub("§.", "", text)
