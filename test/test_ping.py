"""Status ping client and response parsing."""

import json
import socket
import threading
import time

import pytest

from src.minecraft.ping import (
    PingError,
    parse_legacy_response,
    parse_status,
    ping_legacy,
    ping_status,
)
from src.minecraft.protocol import (
    PacketReader,
    SocketReader,
    encode_legacy_kick,
    encode_string,
    encode_varint,
    frame_packet,
)
from test.support import free_tcp_port

STATUS = {
    "version": {"name": "§6Forge 1.20.1", "protocol": 763},
    "players": {
        "max": 20,
        "online": 3,
        "sample": [
            {"name": "Steve", "id": "8667ba71-b85a-4004-af54-457a9734eed7"},
            {"name": "Anonymous Player", "id": "00000000-0000-0000-0000-000000000000"},
            {"broken": True},
            "junk",
        ],
    },
    "description": {
        "text": "§aA ",
        "extra": [{"text": "Modded", "bold": True}, " Server"],
    },
    "favicon": "data:image/png;base64,AAAA",
    "forgeData": {"mods": []},
}


def test_parse_status_full():
    status = parse_status(json.dumps(STATUS))
    assert status.version_name == "Forge 1.20.1"
    assert status.protocol == 763
    assert (status.online, status.max_players) == (3, 20)
    assert status.sample == (
        ("Steve", "8667ba71-b85a-4004-af54-457a9734eed7"),
        ("Anonymous Player", "00000000-0000-0000-0000-000000000000"),
    )
    assert status.motd == "A Modded Server"
    assert status.favicon is True
    assert status.raw["forgeData"] == {"mods": []}


@pytest.mark.parametrize(
    ("description", "motd"),
    [
        ("§cPlain §lstring", "Plain string"),
        ([{"text": "A"}, {"text": "B"}], "AB"),
        ({"translate": "x", "fallback": "Fallback"}, "Fallback"),
        (None, ""),
    ],
)
def test_parse_status_description_forms(description, motd):
    raw = {"players": {"online": 0, "max": 5}, "description": description}
    assert parse_status(raw).motd == motd


def test_parse_status_tolerates_missing_optional_fields():
    status = parse_status('{"players": {"online": "2"}}')
    assert (
        status.online,
        status.max_players,
        status.version_name,
        status.protocol,
    ) == (2, 0, "", None)
    assert status.sample == () and status.favicon is False


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        "{}",
        '{"players": {"max": 20}}',
        '{"players": {"online": true}}',
        '{"players": {"online": -1}}',
        '{"players": "many"}',
        "[" * 100000,
    ],
)
def test_parse_status_rejects_unusable_responses(raw):
    with pytest.raises(PingError):
        parse_status(raw)


def test_parse_status_survives_deep_description():
    description = {"text": "x"}
    for _ in range(300):
        description = {"text": "", "extra": [description]}
    assert (
        parse_status({"players": {"online": 1}, "description": description}).online == 1
    )


# ----------------------------------------------------------------------
# Against scripted servers
# ----------------------------------------------------------------------


class ScriptedServer:
    """Accepts one connection and runs script(conn, reader)."""

    def __init__(self, script):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.handshake = None
        self.error = None
        self._script = script
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        with conn:
            reader = SocketReader(conn, time.monotonic() + 5)
            try:
                packet_id, payload = reader.read_packet()
                body = PacketReader(payload)
                self.handshake = (
                    packet_id,
                    body.read_varint(),
                    body.read_string(255),
                    body.read_ushort(),
                    body.read_varint(),
                )
                self._script(conn, reader)
            except Exception as exc:  # noqa: BLE001 - surfaced by the test
                self.error = exc

    def close(self):
        self.listener.close()
        self._thread.join(5)


@pytest.fixture
def scripted():
    servers = []

    def make(script):
        server = ScriptedServer(script)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def answer_status(text, pong=True):
    def script(conn, reader):
        assert reader.read_packet() == (0, b"")
        conn.sendall(frame_packet(0, encode_string(text, None)))
        if pong:
            packet_id, payload = reader.read_packet()
            assert packet_id == 1
            conn.sendall(frame_packet(1, payload))

    return script


def test_ping_status_full_exchange(scripted):
    server = scripted(answer_status(json.dumps(STATUS)))
    status = ping_status("127.0.0.1", server.port, timeout=2)
    assert status.online == 3 and status.version_name == "Forge 1.20.1"
    assert status.latency_ms is not None and status.latency_ms >= 0
    server.close()
    assert server.error is None
    assert server.handshake == (0, -1, "127.0.0.1", server.port, 1)


def test_ping_status_without_pong_uses_status_latency(scripted):
    server = scripted(answer_status(json.dumps(STATUS), pong=False))
    status = ping_status("127.0.0.1", server.port, timeout=2)
    assert status.online == 3 and status.latency_ms is not None


def test_ping_status_custom_protocol(scripted):
    server = scripted(answer_status(json.dumps(STATUS)))
    ping_status("127.0.0.1", server.port, timeout=2, protocol=767)
    server.close()
    assert server.handshake[1] == 767


def test_ping_status_accepts_large_forge_responses(scripted):
    big = dict(
        STATUS,
        forgeData={
            "mods": [{"modId": f"mod{i}", "modmarker": "x" * 40} for i in range(2000)]
        },
    )
    server = scripted(answer_status(json.dumps(big)))
    assert ping_status("127.0.0.1", server.port, timeout=3).online == 3


@pytest.mark.parametrize(
    "script",
    [
        lambda conn, reader: conn.sendall(
            b"\xff\xff\xff\xff\xff\xff"
        ),  # overlong VarInt
        lambda conn, reader: conn.sendall(
            encode_varint(50_000_000) + b"\x00"
        ),  # oversized frame
        lambda conn, reader: conn.sendall(frame_packet(0x05, b"")),  # wrong packet id
        lambda conn, reader: conn.sendall(frame_packet(0, encode_string("{bad json"))),
        lambda conn, reader: None,  # closes without answering
    ],
)
def test_ping_status_errors_are_ping_errors(scripted, script):
    server = scripted(script)
    with pytest.raises(PingError):
        ping_status("127.0.0.1", server.port, timeout=2)


def test_ping_status_silent_server_times_out(scripted):
    release = threading.Event()
    server = scripted(lambda conn, reader: release.wait(5))
    started = time.monotonic()
    try:
        with pytest.raises(PingError):
            ping_status("127.0.0.1", server.port, timeout=0.3)
    finally:
        release.set()
    assert time.monotonic() - started < 2


def test_ping_status_nothing_listening():
    with pytest.raises(PingError):
        ping_status("127.0.0.1", free_tcp_port(), timeout=1)


# ----------------------------------------------------------------------
# Legacy
# ----------------------------------------------------------------------


def test_parse_legacy_response_modern_format():
    status = parse_legacy_response("§1\x00127\x00§aSleeping\x00§7Zzz\x000\x0020")
    assert (status.protocol, status.version_name, status.motd) == (
        127,
        "Sleeping",
        "Zzz",
    )
    assert (status.online, status.max_players) == (0, 20)


def test_parse_legacy_response_pre_14_format():
    status = parse_legacy_response("A §bServer§3§10")
    assert (status.protocol, status.motd, status.online, status.max_players) == (
        None,
        "A Server",
        3,
        10,
    )


@pytest.mark.parametrize(
    "text", ["", "§1\x00127\x00x", "no separators", "§1\x00127\x00v\x00m\x00x\x00y"]
)
def test_parse_legacy_response_rejects_garbage(text):
    with pytest.raises(PingError):
        parse_legacy_response(text)


def test_ping_legacy_against_a_scripted_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    received = []

    def serve():
        conn, _ = listener.accept()
        with conn:
            received.append(conn.recv(16))
            conn.sendall(encode_legacy_kick("§1\x00127\x001.20.1\x00Hello\x002\x0010"))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        status = ping_legacy("127.0.0.1", listener.getsockname()[1], timeout=2)
    finally:
        thread.join(5)
        listener.close()
    assert received == [b"\xfe\x01"]
    assert (status.version_name, status.motd, status.online, status.max_players) == (
        "1.20.1",
        "Hello",
        2,
        10,
    )
