"""End-to-end checks of our protocol code against the dummy server, with
mcstatus as an independent oracle. The dummy is stdlib-only and shares no
code with src/, so agreement here cross-checks both implementations."""

import json
import os
import signal
import socket
import struct
import subprocess
import time

import pytest
from mcstatus import JavaServer, LegacyServer

from src.minecraft.client import MinecraftClient, roster_from_status
from src.minecraft.ping import ping_legacy, ping_status
from src.minecraft.protocol import (
    PacketReader,
    SocketReader,
    encode_string,
    encode_ushort,
    encode_varint,
    frame_packet,
)
from src.minecraft.rcon import RconAuthError, RconClient
from src.minecraft.server_files import (
    offline_uuid,
    read_json_list,
    read_properties,
    read_server_icon,
)
from test.dummy.minecraft_dummy import long_text
from test.support import free_tcp_ports, wait_until
from test.support.dummy_server import (
    DEFAULT_RCON_PASSWORD,
    DUMMY_PROCESS_NAME,
    dummy_rcon,
    dummy_start_command,
    run_dummy_server,
    wait_for_dummy,
    write_server_files,
)

PLAYERS = ["TestPlayer1", "TestPlayer2", "TestPlayer3"]


def _png64():
    import zlib

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    raw = b"".join(b"\x00" + b"\x7f" * 256 for _ in range(64))
    ihdr = struct.pack(">IIBBBBB", 64, 64, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(scope="module")
def dummy(tmp_path_factory):
    """A shared dummy for read-only checks (TestPlayer2 hides from listings)."""
    server_dir = tmp_path_factory.mktemp("dummy")
    (server_dir / "server-icon.png").write_bytes(_png64())
    game_port, rcon_port = free_tcp_ports(2)
    with run_dummy_server(
        server_dir,
        game_port=game_port,
        rcon_port=rcon_port,
        players=PLAYERS,
        env={"DUMMY_ANONYMOUS": "TestPlayer2"},
        properties={"motd": "§aDummy §lServer", "max_players": 12},
    ) as server:
        yield server


@pytest.fixture
def ports():
    return free_tcp_ports(2)


@pytest.fixture
def client_for():
    clients = []

    def make(server, password=DEFAULT_RCON_PASSWORD):
        client = MinecraftClient(
            "127.0.0.1", server.game_port, server.rcon_port, password, timeout=3
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


# ----------------------------------------------------------------------
# Read-only checks on the shared dummy
# ----------------------------------------------------------------------


def test_support_files_parse_with_server_files(dummy):
    props = read_properties(str(dummy.server_dir / "server.properties"))
    assert props["server-port"] == str(dummy.game_port)
    assert props["rcon.port"] == str(dummy.rcon_port)
    assert props["enable-rcon"] == "true"
    assert props["motd"] == "§aDummy §lServer"
    assert props["max-players"] == "12"
    assert DUMMY_PROCESS_NAME in " ".join(dummy_start_command())


def test_roster_over_rcon(dummy, client_for):
    roster = client_for(dummy).get_roster()
    assert (roster.source, roster.online, roster.max_players) == ("rcon", 3, 12)
    # src's offline_uuid and the dummy's independent implementation agree.
    assert [(p.name, p.uuid) for p in roster.players] == [
        (name, offline_uuid(name)) for name in PLAYERS
    ]
    assert (roster.version, roster.motd) == ("1.20.1", "Dummy Server")


def test_status_ping_agrees_with_mcstatus(dummy):
    ours = ping_status("127.0.0.1", dummy.game_port, timeout=3)
    theirs = JavaServer("127.0.0.1", dummy.game_port, timeout=3).status(tries=1)
    assert ours.online == theirs.players.online == 3
    assert ours.max_players == theirs.players.max == 12
    assert ours.version_name == theirs.version.name == "1.20.1"
    assert ours.protocol == theirs.version.protocol == 763
    assert ours.motd == theirs.motd.to_plain() == "Dummy Server"
    assert list(ours.sample) == [(p.name, p.id) for p in theirs.players.sample]
    assert ours.favicon and theirs.icon == read_server_icon(str(dummy.server_dir))
    assert ours.latency_ms is not None


def test_ping_roster_excludes_anonymous_players(dummy):
    roster = roster_from_status(ping_status("127.0.0.1", dummy.game_port, timeout=3))
    assert roster.online == 3
    assert [p.name for p in roster.players] == ["TestPlayer1", "TestPlayer3"]


def test_legacy_ping_agrees_with_mcstatus(dummy):
    ours = ping_legacy("127.0.0.1", dummy.game_port, timeout=3)
    theirs = LegacyServer("127.0.0.1", dummy.game_port, timeout=3).status(tries=1)
    assert (
        (ours.online, ours.max_players)
        == (theirs.players.online, theirs.players.max)
        == (3, 12)
    )
    assert ours.version_name == theirs.version.name == "1.20.1"
    assert ours.motd == theirs.motd.to_plain() == "Dummy Server"


@pytest.mark.parametrize(
    ("count", "unicode"), [(10000, False), (4096, False), (9000, True)]
)
def test_multi_packet_rcon_reply(dummy, count, unicode):
    client = RconClient(
        "127.0.0.1", dummy.rcon_port, DEFAULT_RCON_PASSWORD, read_timeout=3
    )
    try:
        command = f"dummy long {count}" + (" unicode" if unicode else "")
        assert client.command(command) == long_text(count, unicode=unicode)
        assert client.command("list").startswith("There are 3 of a max of 12")
    finally:
        client.close()


def test_wrong_rcon_password(dummy):
    client = RconClient(
        "127.0.0.1", dummy.rcon_port, "not-the-password", read_timeout=3
    )
    with pytest.raises(RconAuthError):
        client.command("list")
    roster = MinecraftClient(
        "127.0.0.1", dummy.game_port, dummy.rcon_port, "nope", timeout=3
    ).get_roster()
    assert roster.source == "ping"  # falls back instead of failing


def test_dummy_enforces_one_rcon_packet_per_read(dummy):
    """Like vanilla, coalesced packets close the connection (our client never
    sends two packets without waiting for a reply)."""
    password = DEFAULT_RCON_PASSWORD.encode()
    auth = struct.pack("<iii", len(password) + 10, 1, 3) + password + b"\0\0"
    command = struct.pack("<iii", 14, 2, 2) + b"list\0\0"
    with socket.create_connection(("127.0.0.1", dummy.rcon_port), timeout=3) as sock:
        sock.sendall(auth + command)
        try:
            data = sock.recv(4096)
        except ConnectionResetError:
            data = b""
    assert data == b""


def test_dummy_rejects_logins(dummy):
    with socket.create_connection(("127.0.0.1", dummy.game_port), timeout=3) as sock:
        handshake = (
            encode_varint(763)
            + encode_string("localhost\0FML3\0")
            + encode_ushort(dummy.game_port)
        )
        sock.sendall(
            frame_packet(0, handshake + encode_varint(2))
            + frame_packet(0, encode_string("Alex"))
        )
        packet_id, payload = SocketReader(sock, time.monotonic() + 3).read_packet()
    assert packet_id == 0
    assert "dummy" in json.loads(PacketReader(payload).read_string())["text"]


# ----------------------------------------------------------------------
# State-changing checks: one dummy each
# ----------------------------------------------------------------------


def test_moderation_round_trip(tmp_path, ports, client_for):
    with run_dummy_server(
        tmp_path, game_port=ports[0], rcon_port=ports[1], players=PLAYERS
    ) as server:
        client = client_for(server)
        result = client.ban_player("TestPlayer3", "griefing")
        assert result.success and result.message == "Banned TestPlayer3: griefing"
        bans = read_json_list(str(tmp_path / "banned-players.json"))
        assert [(b["name"], b["uuid"], b["reason"], b["expires"]) for b in bans] == [
            ("TestPlayer3", offline_uuid("TestPlayer3"), "griefing", "forever")
        ]
        assert set(bans[0]) == {
            "uuid",
            "name",
            "created",
            "source",
            "expires",
            "reason",
        }
        assert client.ban_player("TestPlayer3").success  # "already banned" counts
        assert client.get_roster().online == 2  # the ban kicked the player
        assert client.kick_player("TestPlayer1").success
        assert not client.kick_player("Nobody").success
        assert client.unban_player("TestPlayer3").success
        assert client.unban_player("TestPlayer3").success  # "isn't banned" counts
        assert read_json_list(str(tmp_path / "banned-players.json")) == []
        cache = {
            e["name"]: e["uuid"]
            for e in read_json_list(str(tmp_path / "usercache.json"))
        }
        assert cache == {name: offline_uuid(name) for name in PLAYERS}


def test_long_banlist_is_reassembled(tmp_path, ports, client_for):
    with run_dummy_server(
        tmp_path, game_port=ports[0], rcon_port=ports[1], players=[]
    ) as server:
        client = client_for(server)
        reason = "a rather long reason to make the ban list exceed one RCON packet"
        for index in range(60):
            assert client.ban_player(f"Griefer{index:02d}", reason).success
        output = client.run_command("banlist")
        assert len(output) > 4096
        assert output.startswith("There are 60 ban(s):")
        assert all(
            f"Griefer{index:02d} was banned by Rcon: {reason}" in output
            for index in range(60)
        )


@pytest.mark.parametrize(
    ("env", "with_uuids"),
    [
        ({}, True),
        ({"DUMMY_LIST_FORMAT": "legacy"}, True),
        ({"DUMMY_NO_UUIDS": "1"}, False),
        ({"DUMMY_LIST_FORMAT": "legacy", "DUMMY_NO_UUIDS": "1"}, False),
    ],
)
def test_list_formats(tmp_path, ports, client_for, env, with_uuids):
    with run_dummy_server(
        tmp_path, game_port=ports[0], rcon_port=ports[1], players=PLAYERS, env=env
    ) as server:
        client = client_for(server)
        for _ in range(2):  # the second sample uses the remembered capability
            roster = client.get_roster()
            assert (roster.source, roster.online, roster.max_players) == ("rcon", 3, 20)
            assert [p.name for p in roster.players] == PLAYERS
            assert all(
                (p.uuid == offline_uuid(p.name)) if with_uuids else p.uuid is None
                for p in roster.players
            )
        assert client._list_uuids is with_uuids
        server.set_players([])
        assert client.get_roster().online == 0


def test_reconnects_after_the_server_restarts(tmp_path, ports, client_for):
    with run_dummy_server(
        tmp_path, game_port=ports[0], rcon_port=ports[1], players=["Before"]
    ) as server:
        client = client_for(server)
        assert [p.name for p in client.get_roster().players] == ["Before"]
    assert client.get_roster() is None  # down: RCON and ping both fail
    with run_dummy_server(
        tmp_path, game_port=ports[0], rcon_port=ports[1], players=["After"]
    ):
        assert [p.name for p in client.get_roster().players] == ["After"]
        assert client.run_command("list").startswith("There are 1 of a max")


def test_ping_fallback_without_rcon(tmp_path, ports):
    with run_dummy_server(
        tmp_path,
        game_port=ports[0],
        rcon_port=ports[1],
        players=PLAYERS,
        env={"DUMMY_ANONYMOUS": "TestPlayer1"},
        properties={"enable_rcon": False},
    ) as server:
        client = MinecraftClient(
            "127.0.0.1", server.game_port, server.rcon_port, None, timeout=3
        )
        roster = client.get_roster()
        assert (roster.source, roster.online) == ("ping", 3)
        assert [p.name for p in roster.players] == ["TestPlayer2", "TestPlayer3"]


def test_graceful_stop_on_stdin(tmp_path, ports):
    with run_dummy_server(tmp_path, game_port=ports[0], rcon_port=ports[1]) as server:
        assert server.stop() == 0
        log = server.log_text()
    for line in ("Stopping server", "Saving worlds", "All chunks are saved"):
        assert line in log


def test_request_stop_over_rcon(tmp_path, ports, client_for):
    with run_dummy_server(tmp_path, game_port=ports[0], rcon_port=ports[1]) as server:
        assert client_for(server).request_stop() is True
        assert server.process.wait(10) == 0
        assert "Stopping the server" in server.log_text()


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM is a hard kill on Windows")
def test_sigterm_is_a_graceful_stop(tmp_path, ports):
    with run_dummy_server(tmp_path, game_port=ports[0], rcon_port=ports[1]) as server:
        server.process.send_signal(signal.SIGTERM)
        assert server.process.wait(10) == 0
        assert "All chunks are saved" in server.log_text()


def test_ignore_stop(tmp_path, ports, client_for):
    with run_dummy_server(
        tmp_path, game_port=ports[0], rcon_port=ports[1], env={"DUMMY_IGNORE_STOP": "1"}
    ) as server:
        assert client_for(server).request_stop() is True
        if os.name != "nt":
            server.process.send_signal(signal.SIGTERM)
        with pytest.raises(subprocess.TimeoutExpired):
            server.process.wait(0.7)
        assert "DUMMY_IGNORE_STOP" in server.log_text()
    assert server.process.poll() is not None  # the context manager killed it


def test_crash_hook(tmp_path, ports):
    with run_dummy_server(tmp_path, game_port=ports[0], rcon_port=ports[1]) as server:
        with pytest.raises(OSError):
            server.rcon("dummy crash", timeout=3)
        assert server.process.wait(10) == 1


def test_ports_open_only_after_the_startup_delay(tmp_path, ports):
    game_port, rcon_port = ports
    write_server_files(tmp_path, game_port, rcon_port)
    env = dict(os.environ, DUMMY_STARTUP_DELAY="1.0")
    started = time.monotonic()
    process = subprocess.Popen(
        dummy_start_command(),
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until(lambda: (tmp_path / "logs" / "latest.log").exists(), timeout=10)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", game_port), timeout=1).close()
        wait_for_dummy(rcon_port, timeout=10, process=process)
        assert time.monotonic() - started >= 1.0
        assert dummy_rcon(rcon_port, command="list").startswith(
            "There are 3 of a max of 20"
        )
    finally:
        process.kill()
        process.wait(5)
