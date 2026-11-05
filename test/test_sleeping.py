"""Sleeping-server responder: logins per protocol version, status (mcstatus as
an independent oracle), legacy ping, hostile and slow clients."""

import json
import queue
import random
import socket
import threading
import time
import uuid

import pytest
from mcstatus import JavaServer, LegacyServer

from src.minecraft import sleeping
from src.minecraft.ping import ping_legacy, ping_status
from src.minecraft.protocol import (
    PacketReader,
    SocketReader,
    encode_long,
    encode_string,
    encode_ushort,
    encode_varint,
    frame_packet,
)
from src.minecraft.sleeping import (
    ACCESS_ERROR_MESSAGE,
    INVALID_NAME_MESSAGE,
    ConnectionKind,
    SleepingServerConfig,
    handle_connection,
    status_json,
)

PLAYER_UUID = uuid.UUID("8667ba71-b85a-4004-af54-457a9734eed7")
CONFIG = SleepingServerConfig(
    motd="§7Server is sleeping §8- §aJoin to wake it up!",
    version_name="Sleeping",
    protocol_version=None,
    max_players=20,
    favicon=None,
    kick_message="§eThe server is starting up!\n§7Please reconnect shortly.",
)


class Sleeper:
    """Accept loop serving each connection with handle_connection."""

    def __init__(self, config=CONFIG, is_allowed=None, timeout=2.0):
        self.config = config
        self.timeout = timeout
        self.calls = []
        self.is_allowed = is_allowed or self.allow_all
        self.outcomes = queue.Queue()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def allow_all(self, name, ip):
        self.calls.append((name, ip))
        return True, None

    def _accept(self):
        while True:
            try:
                conn, addr = self.listener.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(conn, addr), daemon=True
            ).start()

    def _handle(self, conn, addr):
        started = time.monotonic()
        try:
            outcome = handle_connection(
                conn, addr[0], self.config, self.is_allowed, self.timeout
            )
        except BaseException as exc:  # noqa: BLE001 - the test fails on it
            outcome = exc
        finally:
            conn.close()
        self.outcomes.put((outcome, time.monotonic() - started))

    def outcome(self, timeout=5.0):
        outcome, _elapsed = self.outcome_and_time(timeout)
        return outcome

    def outcome_and_time(self, timeout=5.0):
        outcome, elapsed = self.outcomes.get(timeout=timeout)
        assert not isinstance(outcome, BaseException), (
            f"handle_connection raised {outcome!r}"
        )
        return outcome, elapsed

    def close(self):
        self.listener.close()


@pytest.fixture
def sleeper():
    servers = []

    def make(**kwargs):
        server = Sleeper(**kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def handshake(protocol, next_state=2, address="localhost", port=25565):
    payload = encode_varint(protocol) + encode_string(address) + encode_ushort(port)
    return frame_packet(0x00, payload + encode_varint(next_state))


def login_start(protocol, name):
    """Login Start as each protocol version sends it."""
    payload = encode_string(name)
    signature = (
        encode_long(1700000000000)
        + encode_varint(4)
        + b"key!"
        + encode_varint(3)
        + b"sig"
    )
    if protocol == 759:  # 1.19: signature data
        payload += b"\x01" + signature
    elif protocol == 760:  # 1.19.1-1.19.2: signature data + optional UUID
        payload += b"\x01" + signature + b"\x01" + PLAYER_UUID.bytes
    elif 761 <= protocol <= 763:  # 1.19.3-1.20.1: optional UUID
        payload += b"\x01" + PLAYER_UUID.bytes
    elif protocol >= 764:  # 1.20.2+: UUID always present
        payload += PLAYER_UUID.bytes
    return frame_packet(0x00, payload)


def connect(port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(5)
    return sock


def read_disconnect(sock):
    packet_id, payload = SocketReader(sock, time.monotonic() + 5).read_packet()
    assert packet_id == 0x00  # Login Disconnect
    return json.loads(PacketReader(payload).read_string(262144))


VERSIONS = {
    5: "\0FML\0",  # 1.7.10
    340: "\0FML\0",  # 1.12.2
    754: "\0FML2\0",  # 1.16.5
    759: "\0FML3\0",  # 1.19
    760: "\0FML3\0",  # 1.19.2
    763: "\0FML3\0",  # 1.20.1
    764: "\0FORGE",  # 1.20.2
    767: "\0FORGE",  # 1.21.1
}


@pytest.mark.parametrize("modded", [False, True], ids=["vanilla", "forge"])
@pytest.mark.parametrize("protocol", sorted(VERSIONS))
def test_login_wakes_for_every_protocol(sleeper, protocol, modded):
    server = sleeper()
    address = "play.example.com" + (VERSIONS[protocol] if modded else "")
    with connect(server.port) as sock:
        sock.sendall(
            handshake(protocol, 2, address) + login_start(protocol, "Steve_01")
        )
        assert read_disconnect(sock) == {"text": CONFIG.kick_message}
    outcome = server.outcome()
    assert outcome.kind == ConnectionKind.LOGIN
    assert (outcome.player_name, outcome.protocol_version, outcome.wake) == (
        "Steve_01",
        protocol,
        True,
    )
    assert server.calls == [("Steve_01", "127.0.0.1")]


def test_login_sent_in_pieces(sleeper):
    server = sleeper()
    data = handshake(767) + login_start(767, "Alex")
    with connect(server.port) as sock:
        for index in range(0, len(data), 3):
            sock.sendall(data[index : index + 3])
            time.sleep(0.005)
        assert read_disconnect(sock)["text"] == CONFIG.kick_message
    assert server.outcome().wake is True


def test_transfer_intent_counts_as_login(sleeper):
    server = sleeper()
    with connect(server.port) as sock:
        sock.sendall(handshake(767, 3) + login_start(767, "Alex"))
        read_disconnect(sock)
    assert server.outcome().kind == ConnectionKind.LOGIN


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (
            (False, "You are banned from this server."),
            "You are banned from this server.",
        ),
        ((False, None), CONFIG.whitelist_message),
    ],
)
def test_denied_login(sleeper, verdict, expected):
    server = sleeper(is_allowed=lambda name, ip: verdict)
    with connect(server.port) as sock:
        sock.sendall(handshake(763) + login_start(763, "Griefer"))
        assert read_disconnect(sock)["text"] == expected
    outcome = server.outcome()
    assert (outcome.kind, outcome.player_name, outcome.wake) == (
        ConnectionKind.LOGIN,
        "Griefer",
        False,
    )


def test_failing_access_check_denies(sleeper):
    def broken(name, ip):
        raise RuntimeError("whitelist unreadable")

    server = sleeper(is_allowed=broken)
    with connect(server.port) as sock:
        sock.sendall(handshake(763) + login_start(763, "Alex"))
        assert read_disconnect(sock)["text"] == ACCESS_ERROR_MESSAGE
    assert server.outcome().wake is False


@pytest.mark.parametrize("name", ["", "two words", "tab\tname"])
def test_invalid_names_never_wake(sleeper, name):
    server = sleeper()
    with connect(server.port) as sock:
        sock.sendall(handshake(763) + login_start(340, name))
        assert read_disconnect(sock)["text"] == INVALID_NAME_MESSAGE
    assert server.outcome().wake is False
    assert server.calls == []


def test_overlong_name_is_invalid(sleeper):
    server = sleeper()
    with connect(server.port) as sock:
        sock.sendall(handshake(763) + login_start(340, "x" * 17))
    assert server.outcome().kind == ConnectionKind.INVALID


# ----------------------------------------------------------------------
# Status (mcstatus is the independent oracle)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("client_protocol", [47, 340, 763, 767])
def test_mcstatus_status_echoes_the_client_protocol(sleeper, client_protocol):
    server = sleeper()
    response = JavaServer("127.0.0.1", server.port, timeout=3).status(
        tries=1, version=client_protocol
    )
    assert response.version.protocol == client_protocol
    assert response.version.name == "Sleeping"
    assert response.motd.to_plain() == "Server is sleeping - Join to wake it up!"
    assert (response.players.online, response.players.max) == (0, 20)
    assert response.players.sample == []
    assert response.icon is None
    outcome = server.outcome()
    assert (outcome.kind, outcome.protocol_version, outcome.wake) == (
        ConnectionKind.STATUS,
        client_protocol,
        False,
    )


def test_configured_protocol_and_favicon(sleeper):
    favicon = "data:image/png;base64,iVBORw0KGgo="
    config = SleepingServerConfig(
        motd="Zzz",
        version_name="Sleeping",
        protocol_version=999,
        max_players=7,
        favicon=favicon,
        kick_message="k",
    )
    server = sleeper(config=config)
    response = JavaServer("127.0.0.1", server.port, timeout=3).status(
        tries=1, version=763
    )
    assert (response.version.protocol, response.players.max, response.icon) == (
        999,
        7,
        favicon,
    )


def test_mcstatus_ping_without_status_request(sleeper):
    server = sleeper()
    assert JavaServer("127.0.0.1", server.port, timeout=3).ping(tries=1) >= 0
    assert server.outcome().kind == ConnectionKind.STATUS


def test_our_ping_client_against_the_sleeping_server(sleeper):
    server = sleeper()
    status = ping_status("127.0.0.1", server.port, timeout=3)
    assert (
        status.version_name,
        status.protocol,
        status.online,
        status.max_players,
    ) == ("Sleeping", -1, 0, 20)
    assert status.motd == "Server is sleeping - Join to wake it up!"
    assert status.latency_ms is not None
    assert server.outcome().kind == ConnectionKind.STATUS


def test_legacy_ping(sleeper):
    server = sleeper()
    response = LegacyServer("127.0.0.1", server.port, timeout=3).status(tries=1)
    assert (response.version.name, response.version.protocol) == ("Sleeping", 127)
    assert response.motd.to_plain() == "Server is sleeping - Join to wake it up!"
    assert (response.players.online, response.players.max) == (0, 20)
    assert server.outcome().kind == ConnectionKind.LEGACY_PING
    legacy = ping_legacy("127.0.0.1", server.port, timeout=3)
    assert (legacy.version_name, legacy.online, legacy.max_players) == (
        "Sleeping",
        0,
        20,
    )
    assert server.outcome().kind == ConnectionKind.LEGACY_PING


def test_status_json_respects_the_client_limit():
    big = "data:image/png;base64," + "A" * 40000
    config = SleepingServerConfig("Zzz", "Sleeping", None, 20, big, "k")
    assert "favicon" not in json.loads(status_json(config, 763))
    config = SleepingServerConfig("Z" * 40000, "Sleeping", None, 20, None, "k")
    assert len(status_json(config, 763)) < 32767
    config = SleepingServerConfig("Zzz", "Sleeping", None, "not a number", None, "k")
    assert json.loads(status_json(config, 5))["players"]["max"] == 20


# ----------------------------------------------------------------------
# Hostile and slow clients
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        random.Random(1234).randbytes(64).replace(b"\xfe", b"\x00"),
        b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
        encode_varint(40000) + b"\x00" * 100,  # frame above the 32 KiB limit
        b"\xff\xff\xff\xff\xff\x01",  # overlong VarInt
        frame_packet(0x05, b"\x00"),  # not a handshake
        handshake(763, 7),  # unknown next state
        handshake(763, 1) + frame_packet(0x07, b""),  # neither status request nor ping
        handshake(763, 2) + frame_packet(0x02, b"\x00"),  # not Login Start
        handshake(763, 2)
        + frame_packet(0x00, encode_varint(-1)),  # negative string length
        b"",  # connect and close
    ],
)
def test_garbage_is_invalid(sleeper, data):
    server = sleeper()
    with connect(server.port) as sock:
        if data:
            sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        try:
            sock.recv(1024)
        except OSError:
            pass
    outcome = server.outcome()
    assert outcome.kind == ConnectionKind.INVALID and outcome.wake is False


def test_silent_client_times_out(sleeper):
    server = sleeper(timeout=0.3)
    with connect(server.port):
        outcome, elapsed = server.outcome_and_time()
    assert outcome.kind == ConnectionKind.INVALID
    assert 0.25 <= elapsed < 1.5


def test_trickling_client_is_bounded_by_the_overall_timeout(sleeper):
    server = sleeper(timeout=0.4)
    data = handshake(763) + login_start(763, "Slowpoke")
    with connect(server.port) as sock:
        for byte in data:
            try:
                sock.sendall(bytes([byte]))
            except OSError:
                break
            if not server.outcomes.empty():
                break
            time.sleep(0.1)
        outcome, elapsed = server.outcome_and_time()
    assert outcome.kind == ConnectionKind.INVALID
    assert elapsed < 1.5


def test_client_that_never_closes_is_released(sleeper, monkeypatch):
    monkeypatch.setattr(sleeping, "DRAIN_SECONDS", 0.2)
    server = sleeper()
    with connect(server.port) as sock:
        sock.sendall(handshake(763) + login_start(763, "Alex"))
        outcome, elapsed = server.outcome_and_time()
        assert outcome.wake is True and elapsed < 1.5
        # The message is still readable after the server half-closed.
        assert read_disconnect(sock)["text"] == CONFIG.kick_message
        assert sock.recv(10) == b""  # FIN from the server


def test_closed_socket_never_raises():
    sock = socket.socket()
    sock.close()
    outcome = handle_connection(
        sock, "1.2.3.4", CONFIG, lambda name, ip: (True, None), timeout=0.5
    )
    assert outcome.kind == ConnectionKind.INVALID


@pytest.mark.parametrize("timeout", [None, 0, -1])
def test_bad_timeout_values_fall_back_to_the_default(timeout):
    left, right = socket.socketpair()
    with left, right:
        left.close()
        outcome = handle_connection(
            right, "1.2.3.4", CONFIG, lambda name, ip: (True, None), timeout=timeout
        )
    assert outcome.kind == ConnectionKind.INVALID
