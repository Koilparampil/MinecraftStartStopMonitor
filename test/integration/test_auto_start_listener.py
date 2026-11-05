"""The auto-start listener with the real sleeping-server protocol on a free port.

- mcstatus (an independent client) sees the sleeping MOTD;
- a raw handshake + Login Start wakes the server exactly once, and the port is
  free (no LISTEN socket left) by the time the start is requested;
- denied players (not whitelisted, banned) get a disconnect and wake nothing;
- the listener re-arms after SERVER_STOPPED;
- full chain with the real MinecraftController: the port is released before
  the process launch, and re-armed when the launched server exits.
"""

import json
import socket
import struct
import threading

import psutil
import pytest
from mcstatus import JavaServer

from src.auto_start import AutoStartManager, ListenerOptions
from src.banlist_manager import BanlistManager
from src.events import Event, EventBus
from src.lifecycle.messages import LifecycleState, ProcessIdentity
from src.minecraft_control import MinecraftController
from src.player_manager import PlayerManager
from test.support import free_tcp_port, wait_until
from test.support.fakes import FakeClient, FakeProcessManager

OPTIONS = ListenerOptions(
    bind_attempts=3,
    retry_delay=0.1,
    slow_retry_delay=0.2,
    accept_timeout=0.05,
    connection_timeout=3.0,
)
PROTOCOL_1_20_1 = 763


# ----------------------------------------------------------------------
# Minimal independent client for the Java protocol
# ----------------------------------------------------------------------


def varint(value):
    out = b""
    value &= 0xFFFFFFFF
    while True:
        byte = value & 0x7F
        value >>= 7
        out += bytes([byte | (0x80 if value else 0)])
        if not value:
            return out


def mc_string(text):
    data = text.encode("utf-8")
    return varint(len(data)) + data


def frame(packet_id, payload):
    body = varint(packet_id) + payload
    return varint(len(body)) + body


def read_varint(data, pos):
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed before a whole packet arrived")
        data += chunk
    return data


def recv_packet(sock):
    """One length-prefixed frame, like a real client reads it."""
    length = shift = 0
    while True:
        byte = recv_exact(sock, 1)[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return recv_exact(sock, length)


def join_attempt(port, name, protocol=PROTOCOL_1_20_1):
    """Handshake (next_state=2) + Login Start; returns the disconnect JSON.
    Closes right after the disconnect packet, as the game client does."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        handshake = varint(protocol) + mc_string("localhost") + struct.pack(">H", port)
        sock.sendall(frame(0x00, handshake + varint(2)))
        sock.sendall(frame(0x00, mc_string(name) + b"\x00"))  # no UUID (1.19.3-1.20.1)
        packet = recv_packet(sock)
    packet_id, pos = read_varint(packet, 0)
    assert packet_id == 0x00, f"expected Login Disconnect, got {packet_id:#x}"
    text_length, pos = read_varint(packet, pos)
    return json.loads(packet[pos : pos + text_length].decode("utf-8"))


def message_text(component):
    if isinstance(component, str):
        return component
    text = component.get("text", "")
    return text + "".join(message_text(part) for part in component.get("extra", []))


def can_bind(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # like the JVM
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def listening_here(port):
    return any(
        c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port
        for c in psutil.Process().net_connections(kind="tcp")
    )


def status(port):
    return JavaServer("127.0.0.1", port, timeout=3).status(tries=1)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


class RecordingController:
    def __init__(self, port):
        self.port = port
        self.state = LifecycleState.STOPPED
        self.calls = []
        self.started = threading.Event()

    def start_server(self):
        self.calls.append(
            {"port_free": can_bind(self.port), "listening": listening_here(self.port)}
        )
        self.started.set()
        return True


@pytest.fixture
def port(mock_settings):
    port = free_tcp_port()
    mock_settings.serverPort = port
    mock_settings.listenerBindHost = "127.0.0.1"
    return port


@pytest.fixture
def listener(port):
    bus = EventBus()
    controller = RecordingController(port)
    manager = AutoStartManager(controller, bus, options=OPTIONS)
    manager.listen()
    wait_until(lambda: manager.is_listening, message="listener did not bind")
    yield manager, controller, bus
    manager.close()


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_mcstatus_sees_the_sleeping_server(listener, port, server_dir, mock_settings):
    response = status(port)
    assert response.motd.to_plain() == "Server is sleeping - Join to wake it up!"
    assert response.version.name == "Sleeping"
    assert response.players.online == 0
    assert response.players.max == 20
    _manager, controller, _bus = listener
    assert controller.calls == []


def test_server_properties_drive_max_players(port, server_dir):
    (server_dir / "server.properties").write_text("max-players=7\n", encoding="utf-8")
    manager = AutoStartManager(RecordingController(port), EventBus(), options=OPTIONS)
    try:
        manager.listen()
        wait_until(lambda: manager.is_listening)
        assert status(port).players.max == 7
    finally:
        manager.close()


def test_join_attempt_wakes_once_and_releases_the_port(listener, port, mock_settings):
    _manager, controller, _bus = listener
    reply = join_attempt(port, "Steve")
    assert message_text(reply) == mock_settings.startingMessage
    assert controller.started.wait(5)
    assert controller.calls == [{"port_free": True, "listening": False}]
    assert can_bind(port)  # what the real server does seconds later
    with pytest.raises(OSError):
        join_attempt(port, "Alex")  # nobody listens until SERVER_STOPPED
    assert len(controller.calls) == 1


def test_not_whitelisted_player_does_not_wake(listener, port, server_dir):
    (server_dir / "server.properties").write_text("white-list=true\n", encoding="utf-8")
    (server_dir / "whitelist.json").write_text(
        json.dumps([{"name": "Alex"}]), encoding="utf-8"
    )
    _manager, controller, _bus = listener
    assert "not white-listed" in message_text(join_attempt(port, "Stranger"))
    assert not controller.started.wait(0.3)
    assert status(port).version.name == "Sleeping"  # still asleep and listening
    join_attempt(port, "alex")
    assert controller.started.wait(5)


def test_banned_player_does_not_wake(listener, port, server_dir):
    (server_dir / "banned-players.json").write_text(
        json.dumps([{"name": "Griefer", "expires": "forever", "reason": "Grief"}]),
        encoding="utf-8",
    )
    _manager, controller, _bus = listener
    text = message_text(join_attempt(port, "Griefer"))
    assert "banned" in text
    assert "Grief" in text
    assert not controller.started.wait(0.3)
    assert len(controller.calls) == 0


def test_listener_rearms_after_server_stopped(listener, port):
    manager, controller, bus = listener
    join_attempt(port, "Steve")
    assert controller.started.wait(5)
    assert not manager.is_listening

    bus.publish(Event.SERVER_STOPPED, {"state": LifecycleState.STOPPED})
    wait_until(lambda: manager.is_listening, message="listener did not re-arm")
    assert status(port).version.name == "Sleeping"
    controller.started.clear()
    join_attempt(port, "Steve")
    assert controller.started.wait(5)
    assert [call["port_free"] for call in controller.calls] == [True, True]


# ----------------------------------------------------------------------
# Full chain with the real controller (fake process/client backends)
# ----------------------------------------------------------------------


class PortCheckingProcessManager(FakeProcessManager):
    def __init__(self, port):
        super().__init__()
        self.port = port
        self.port_free_at_launch = []

    def launch_process(self):
        self.port_free_at_launch.append(can_bind(self.port))
        return super().launch_process()


def test_full_chain_release_before_launch_and_rearm_on_exit(port, mock_settings):
    mock_settings.pollingRate = 0.1
    bus = EventBus()
    process_manager = PortCheckingProcessManager(port)
    process_manager.launch_identity = ProcessIdentity(424242, 1.0)
    controller = MinecraftController(
        client=FakeClient(),
        process_manager=process_manager,
        player_manager=PlayerManager(bus),
        banlist_manager=BanlistManager(bus),
        bus=bus,
    )
    manager = AutoStartManager(
        controller, bus, options=OPTIONS
    )  # like main: before start()
    controller.start()
    try:
        # The supervisor's first scan finds no server and commits STOPPED.
        wait_until(lambda: manager.is_listening, message="not armed after startup scan")
        join_attempt(port, "Steve")
        wait_until(
            lambda: controller.state == LifecycleState.RUNNING, message="not started"
        )
        assert process_manager.port_free_at_launch == [True]
        assert not manager.is_listening

        process_manager.exit(process_manager.launch_identity)  # server exits
        wait_until(
            lambda: controller.state == LifecycleState.STOPPED, message="exit not seen"
        )
        wait_until(lambda: manager.is_listening, message="not re-armed after exit")
        assert status(port).version.name == "Sleeping"
    finally:
        manager.close()
        controller.close()
