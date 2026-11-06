"""The auto-start listener with the real sleeping-server protocol on a free port.

- mcstatus (an independent client) sees the sleeping MOTD;
- a raw handshake + Login Start wakes the server exactly once, and the port is
  free (no LISTEN socket left) by the time the start is requested;
- denied players (not whitelisted, banned) get a disconnect and wake nothing;
- the listener re-arms after SERVER_STOPPED;
- full chain with the real MinecraftController: the port is released before
  the process launch, and re-armed when the launched server exits.
"""

import contextlib
import dataclasses
import json
import logging
import socket
import struct
import threading

import psutil
import pytest
from mcstatus import JavaServer

from src.auto_start import (
    BUSY_MESSAGE,
    WHITELIST_MESSAGE,
    AutoStartManager,
    ListenerOptions,
    _RefusalLog,
)
from src.banlist_manager import BanlistManager
from src.events import Event, EventBus
from src.lifecycle.messages import LifecycleState, ProcessIdentity
from src.minecraft.protocol import safe_for_log
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


@pytest.mark.parametrize(
    "name",
    ["a\nFAKE ERROR", "b\rFAKE", "\x1b[31mRED\x1b[0m"],
    ids=["newline", "carriage-return", "ansi-escape"],
)
def test_refused_names_are_logged_escaped(listener, port, caplog, name):
    """The control characters that make a name invalid never reach the log
    (FU-2): one line per record, no control characters."""
    manager, controller, _bus = listener
    manager.refusals = _RefusalLog(interval=0.0)  # log every refusal
    caplog.set_level(logging.DEBUG)

    assert message_text(join_attempt(port, name)) == "Invalid player name."

    expected = f"Not starting the server for {safe_for_log(name)} (127.0.0.1)"
    wait_until(
        lambda: any(expected in r.getMessage() for r in caplog.records),
        message="the refusal was not logged",
    )
    assert all(record.getMessage().isprintable() for record in caplog.records)
    assert controller.calls == []


def test_a_waking_name_is_logged_escaped(listener, port, caplog):
    """Valid names may still hold invisible characters (NEL, separators)."""
    _manager, controller, _bus = listener
    caplog.set_level(logging.DEBUG)
    name = "Ev\u2028il\x85\u202e"

    join_attempt(port, name)

    assert controller.started.wait(5)
    messages = [record.getMessage() for record in caplog.records]
    assert f"{safe_for_log(name)} (127.0.0.1) is trying to join" in "\n".join(messages)
    assert all(message.isprintable() for message in messages)


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
# Floods from the joining player's own address (a shared tunnel or NAT)
# ----------------------------------------------------------------------


def hold_lane(port, payload, stop):
    """Connect, send *payload* (nothing for an idle lane) and hold the
    connection; reconnect as soon as the listener drops it."""
    while not stop.is_set():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.2)
            sock.connect(("127.0.0.1", port))
            if payload:
                sock.sendall(payload)
            while not stop.is_set():
                try:
                    if sock.recv(4096) == b"":
                        break  # dropped: reconnect at once
                except TimeoutError:
                    continue
        except OSError:
            stop.wait(0.02)  # nobody listens (any more)
        finally:
            sock.close()


@contextlib.contextmanager
def flood(port, payload, lanes):
    stop = threading.Event()
    threads = [
        threading.Thread(target=hold_lane, args=(port, payload, stop), daemon=True)
        for _ in range(lanes)
    ]
    for thread in threads:
        thread.start()
    try:
        yield
    finally:
        stop.set()
        for thread in threads:
            thread.join(2)


def wakes_within(port, attempts, starting):
    replies = []
    for _ in range(attempts):
        replies.append(message_text(join_attempt(port, "Steve")))
        if replies[-1] == starting:
            return True
    raise AssertionError(f"not woken in {attempts} attempts: {replies}")


def test_held_legacy_pings_do_not_block_a_wake(listener, port, mock_settings):
    """FU-3: four connections that send a legacy ping (0xFE 0x01) and hold
    it fill the address's slots; a legacy ping is as evictable as any ping."""
    manager, controller, _bus = listener
    with flood(port, b"\xfe\x01", lanes=4):
        wait_until(lambda: manager.active_connections == 4, message="not held")
        assert wakes_within(port, 2, mock_settings.startingMessage)
    assert controller.started.wait(5)
    assert len(controller.calls) == 1


@pytest.mark.parametrize("grace", [0.25, 5.0], ids=["default-grace", "long-grace"])
def test_idle_lanes_do_not_block_a_wake(port, mock_settings, grace):
    """FU-4: eight lanes that connect, send nothing and reconnect at once
    keep the address's slots full of young idle connections. A complete
    login evicts one of them, however young (the long grace makes every
    one of them young)."""
    controller = RecordingController(port)
    options = dataclasses.replace(OPTIONS, eviction_grace=grace)
    manager = AutoStartManager(controller, EventBus(), options=options)
    try:
        manager.listen()
        wait_until(lambda: manager.is_listening, message="listener did not bind")
        with flood(port, b"", lanes=8):
            wait_until(lambda: manager.active_connections == 4, message="not held")
            assert wakes_within(port, 2, mock_settings.startingMessage)
        assert controller.started.wait(5)
        assert len(controller.calls) == 1
    finally:
        manager.close()


def test_a_burst_of_refused_players_all_get_a_message(listener, port, server_dir):
    """FU-5: 12 players who are not whitelisted join 30 times each, all at
    once, through one address. Every join gets a message (the refusal, or
    "busy" when every slot is taken), never a bare close, and none wakes
    the server."""
    (server_dir / "server.properties").write_text("white-list=true\n", encoding="utf-8")
    (server_dir / "whitelist.json").write_text(
        json.dumps([{"name": "Alex"}]), encoding="utf-8"
    )
    manager, controller, _bus = listener
    manager.refusals = _RefusalLog(interval=3600)  # one log line, not 360
    replies, failures = [], []

    def player(number):
        for attempt in range(30):
            try:
                reply = join_attempt(port, f"Stranger{number}x{attempt}")
            except OSError as error:
                failures.append(f"{number}/{attempt}: {error!r}")
            else:
                replies.append(message_text(reply))

    players = [threading.Thread(target=player, args=(n,)) for n in range(12)]
    for thread in players:
        thread.start()
    for thread in players:
        thread.join(60)

    assert failures == []
    assert len(replies) == 12 * 30
    assert set(replies) <= {WHITELIST_MESSAGE, BUSY_MESSAGE}
    assert WHITELIST_MESSAGE in replies
    assert controller.calls == []


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
