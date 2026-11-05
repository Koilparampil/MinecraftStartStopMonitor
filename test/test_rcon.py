"""RCON client against an in-process fake server that reads like vanilla."""

import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.minecraft.protocol import ProtocolError, SocketReader
from src.minecraft.rcon import (
    MAX_PAYLOAD,
    TYPE_SENTINEL,
    RconAuthError,
    RconClient,
    RconError,
    RconUnavailable,
    encode_packet,
    read_packet,
)
from test.support import free_tcp_port

PASSWORD = "secret"


class Context:
    def __init__(self, conn):
        self.conn = conn
        self.authed = False

    def send(self, request_id, packet_type, payload=b""):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.conn.sendall(
            struct.pack("<iii", len(payload) + 10, request_id, packet_type)
            + payload
            + b"\0\0"
        )

    def reply(self, request_id, text):
        """Vanilla splitting: 4096 characters per packet."""
        chunks = [text[i : i + 4096] for i in range(0, len(text), 4096)] or [""]
        for chunk in chunks:
            self.send(request_id, 0, chunk)


class FakeRcon:
    """Reads one packet per recv(1460) like vanilla and records violations."""

    def __init__(self, handler):
        self.handler = handler
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        self.received = []  # (connection number, request id, type, body)
        self.violations = []
        self._threads = []
        self._conns = []
        thread = threading.Thread(target=self._accept, daemon=True)
        thread.start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            self._conns.append(conn)
            thread = threading.Thread(
                target=self._serve, args=(conn, self.connections), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _serve(self, conn, number):
        ctx = Context(conn)
        try:
            while True:
                data = conn.recv(1460)
                if not data:
                    return
                (length,) = struct.unpack_from("<i", data)
                if length != len(data) - 4:
                    self.violations.append(len(data))
                    return
                request_id, packet_type = struct.unpack_from("<ii", data, 4)
                body = data[12:-2].decode("utf-8")
                self.received.append((number, request_id, packet_type, body))
                if self.handler(ctx, request_id, packet_type, body) is False:
                    return
        except OSError:
            return
        finally:
            conn.close()

    def types(self):
        return [packet_type for _, _, packet_type, _ in self.received]

    def close(self):
        self.listener.close()
        for conn in self._conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def vanilla(replies=None, *, preamble=False, sentinel=True):
    """A handler behaving like the vanilla RconClient thread."""
    replies = replies or {}

    def handler(ctx, request_id, packet_type, body):
        if packet_type == 3:
            ctx.authed = body == PASSWORD
            if ctx.authed and preamble:
                ctx.send(request_id, 0)
            ctx.send(request_id if ctx.authed else -1, 2)
        elif packet_type == 2:
            if not ctx.authed:
                ctx.send(-1, 2)
                return None
            reply = replies.get(body, f"echo:{body}")
            if callable(reply):
                return reply(ctx, request_id)
            ctx.reply(request_id, reply)
        elif sentinel:
            ctx.reply(request_id, f"Unknown request {packet_type & 0xFFFFFFFF:x}")
        return None

    return handler


@pytest.fixture
def make_server():
    servers = []

    def make(handler):
        server = FakeRcon(handler)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


@pytest.fixture
def make_client():
    clients = []

    def make(server, password=PASSWORD, **kwargs):
        kwargs.setdefault("read_timeout", 2.0)
        client = RconClient("127.0.0.1", server.port, password, **kwargs)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


# ----------------------------------------------------------------------


def test_packet_round_trip():
    left, right = socket.socketpair()
    with left, right:
        left.sendall(encode_packet(7, 2, b"list") + encode_packet(-1, 2))
        reader = SocketReader(right)
        first = read_packet(reader, time.monotonic() + 1)
        second = read_packet(reader, time.monotonic() + 1)
    assert (first.request_id, first.packet_type, first.payload) == (7, 2, b"list")
    assert (second.request_id, second.payload) == (-1, b"")


@pytest.mark.parametrize("length", [7, -5, 1 << 30])
def test_read_packet_rejects_bad_lengths(length):
    left, right = socket.socketpair()
    with left, right:
        left.sendall(struct.pack("<i", length) + b"\0" * 16)
        with pytest.raises(ProtocolError):
            read_packet(SocketReader(right), time.monotonic() + 1)


def test_auth_then_command(make_server, make_client):
    server = make_server(
        vanilla({"list": "There are 0 of a max of 20 players online: "})
    )
    client = make_client(server)
    assert client.command("list") == "There are 0 of a max of 20 players online: "
    assert client.command("say hi") == "echo:say hi"
    assert server.connections == 1  # persistent connection
    assert server.types() == [3, 2, 2]
    assert server.violations == []
    assert "secret" not in repr(client)


def test_wrong_password(make_server, make_client):
    server = make_server(vanilla())
    client = make_client(server, password="wrong")
    with pytest.raises(RconAuthError):
        client.command("list")
    assert not client.connected


def test_auth_skips_stray_type0_packet(make_server, make_client):
    server = make_server(vanilla(preamble=True))
    assert make_client(server).command("x") == "echo:x"


@pytest.mark.parametrize("size", [4096, 4097, 10000, 3 * 4096])
def test_multi_packet_reply(make_server, make_client, size):
    text = "".join(f"<{i:06d}>" for i in range(size // 8 + 1))[:size]
    server = make_server(vanilla({"long": text}))
    client = make_client(server)
    assert client.command("long") == text
    # The sentinel goes out only after the first fragment came back.
    assert server.types() == [3, 2, TYPE_SENTINEL]
    assert server.violations == []
    assert client.command("short") == "echo:short"


def test_multi_packet_reply_with_multibyte_characters(make_server, make_client):
    text = "é✓" * 2600  # 4096-char fragments of 2- and 3-byte characters
    server = make_server(vanilla({"long": text}))
    assert make_client(server).command("long") == text


def test_short_reply_sends_no_sentinel(make_server, make_client):
    server = make_server(vanilla())
    make_client(server).command("a")
    assert server.types() == [3, 2]


def test_unanswered_sentinel_returns_what_arrived(make_server, make_client):
    text = "x" * 5000
    server = make_server(vanilla({"long": text}, sentinel=False))
    client = make_client(server, sentinel_timeout=0.2)
    started = time.monotonic()
    assert client.command("long") == text
    assert time.monotonic() - started < 1.5
    assert client.command("next") == "echo:next"  # same connection still usable


def test_replies_to_other_requests_are_skipped(make_server, make_client):
    def stale_first(ctx, request_id):
        ctx.send(request_id + 1000, 0, "stale")
        ctx.reply(request_id, "fresh")

    server = make_server(vanilla({"cmd": stale_first}))
    assert make_client(server).command("cmd") == "fresh"


def test_payload_limit_checked_before_sending(make_server, make_client):
    server = make_server(vanilla())
    client = make_client(server)
    with pytest.raises(RconError, match="too long"):
        client.command("x" * (MAX_PAYLOAD + 1))
    with pytest.raises(RconError, match="NUL"):
        client.command("a\0b")
    assert server.connections == 0
    # The largest allowed packet still arrives in one 1460-byte read.
    assert client.command("y" * MAX_PAYLOAD) == "echo:" + "y" * MAX_PAYLOAD
    assert server.violations == []


def test_connection_refused_is_unavailable():
    client = RconClient("127.0.0.1", free_tcp_port(), PASSWORD, connect_timeout=1)
    with pytest.raises(RconUnavailable, match="refused"):
        client.command("list")


def test_read_timeout_is_unavailable(make_server, make_client):
    server = make_server(vanilla({"hang": lambda ctx, request_id: None}))
    client = make_client(server, read_timeout=0.3)
    started = time.monotonic()
    with pytest.raises(RconUnavailable, match="timed out"):
        client.command("hang")
    assert time.monotonic() - started < 2
    assert not client.connected


def test_reconnects_after_the_server_dropped_the_connection(make_server, make_client):
    def reply_then_close(ctx, request_id):
        ctx.reply(request_id, "bye")
        return False

    server = make_server(vanilla({"once": reply_then_close}))
    client = make_client(server)
    assert client.command("once") == "bye"
    time.sleep(0.05)  # the server side is closed now
    assert client.command("again") == "echo:again"  # transparently reconnected
    assert server.connections == 2


def test_unauthenticated_command_reply(make_server, make_client):
    def forget_auth(ctx, request_id):
        ctx.send(-1, 2)

    server = make_server(vanilla({"cmd": forget_auth}))
    with pytest.raises(RconAuthError):
        make_client(server).command("cmd")


def test_send_final_counts_a_dropped_connection_as_sent(make_server, make_client):
    server = make_server(vanilla({"stop": lambda ctx, request_id: False}))
    client = make_client(server)
    client.command("warmup")
    assert client.send_final("stop") is True
    assert server.connections == 2  # stop goes over a fresh connection
    assert not client.connected


def test_send_final_with_reply(make_server, make_client):
    server = make_server(vanilla({"stop": "Stopping the server"}))
    assert make_client(server).send_final("stop") is True


def test_send_final_when_down():
    client = RconClient("127.0.0.1", free_tcp_port(), PASSWORD, connect_timeout=1)
    with pytest.raises(RconUnavailable):
        client.send_final("stop")


def test_invalid_utf8_is_replaced(make_server, make_client):
    server = make_server(
        vanilla({"bad": lambda ctx, request_id: ctx.send(request_id, 0, b"ok\xff")})
    )
    assert make_client(server).command("bad") == "ok�"


def test_garbage_is_a_protocol_error(make_server, make_client):
    def garbage(ctx, request_id):
        ctx.conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")

    server = make_server(vanilla({"cmd": garbage}))
    with pytest.raises(RconError, match="invalid RCON") as info:
        make_client(server).command("cmd")
    assert not isinstance(info.value, RconUnavailable)


def test_concurrent_commands_keep_one_packet_in_flight(make_server, make_client):
    server = make_server(vanilla())
    client = make_client(server)
    commands = [f"cmd {i}" for i in range(40)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(client.command, commands))
    assert replies == [f"echo:{command}" for command in commands]
    assert server.violations == []
    assert server.connections == 1
