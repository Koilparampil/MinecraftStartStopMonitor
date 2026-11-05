"""MinecraftClient: list parsing, RCON/ping fallback, caching, moderation."""

import pytest

from src.lifecycle.messages import OnlinePlayer
from src.minecraft.client import (
    MinecraftClient,
    RconAuthError,
    RconError,
    RconUnavailable,
    parse_list_response,
    roster_from_status,
)
from src.minecraft.ping import PingError, ServerStatus

ALEX = "8667ba71-b85a-4004-af54-457a9734eed7"
STEVE = "5627dd98-e6be-3c21-b8a8-e92344183641"


def names(roster):
    return [player.name for player in roster.players]


@pytest.mark.parametrize(
    ("text", "online", "maximum", "expected_names"),
    [
        # 1.13+
        (
            "There are 2 of a max of 20 players online: Alex, Steve",
            2,
            20,
            ["Alex", "Steve"],
        ),
        ("There are 0 of a max of 20 players online: ", 0, 20, []),
        ("There are 0 of a max of 20 players online:", 0, 20, []),
        ("There are 1 of a max of 5 players online: Alex", 1, 5, ["Alex"]),
        (
            "There are 2 of a maximum of 20 players online: Alex, Steve",
            2,
            20,
            ["Alex", "Steve"],
        ),
        # 1.7-1.12: names on the following line(s), or glued on by RCON
        ("There are 2/20 players online:\nAlex, Steve", 2, 20, ["Alex", "Steve"]),
        ("There are 2/20 players online:Alex, Steve", 2, 20, ["Alex", "Steve"]),
        (
            "There are 3/20 players online:\nAlex, Steve\nHerobrine",
            3,
            20,
            ["Alex", "Steve", "Herobrine"],
        ),
        ("There are 0/20 players online:", 0, 20, []),
        ("There are 0/20 players online:\n", 0, 20, []),
        # Bukkit / Essentials style
        (
            "There are 2 out of maximum 20 players online.\nDefault: Alex, Steve",
            2,
            20,
            ["Alex", "Steve"],
        ),
        (
            (
                "§6There are §c3§6 out of maximum §c20§6 players online.\n"
                "§6Admins§r: §7[AFK]§rAlex\n§6Default§r: Steve, [HIDDEN]Bob"
            ),
            3,
            20,
            ["Alex", "Steve", "Bob"],
        ),
        # formatting codes, CRLF, junk tokens
        (
            "§eThere are §a2§e of a max of §a20§e players online: §bAlex§r, §bSteve",
            2,
            20,
            ["Alex", "Steve"],
        ),
        ("There are 2/20 players online:\r\nAlex, Steve\r\n", 2, 20, ["Alex", "Steve"]),
        (
            "There are 5 of a max of 20 players online: Alex, and 4 more",
            5,
            20,
            ["Alex"],
        ),
        # more names than the count: the count is raised, never lowered
        (
            "There are 1 of a max of 20 players online: Alex, Steve",
            2,
            20,
            ["Alex", "Steve"],
        ),
    ],
)
def test_parse_list_matrix(text, online, maximum, expected_names):
    roster = parse_list_response(text)
    assert roster is not None
    assert (roster.online, roster.max_players, roster.source) == (
        online,
        maximum,
        "rcon",
    )
    assert names(roster) == expected_names
    assert all(player.uuid is None for player in roster.players)


@pytest.mark.parametrize(
    "text",
    [
        f"There are 2 of a max of 20 players online: Alex ({ALEX}), Steve ({STEVE})",
        f"There are 2/20 players online:\nAlex ({ALEX}), Steve ({STEVE})",
        f"There are 2/20 players online:Alex ({ALEX.replace('-', '')}), Steve ({STEVE.upper()})",
    ],
)
def test_parse_list_uuids(text):
    roster = parse_list_response(text)
    assert roster.players == (OnlinePlayer("Alex", ALEX), OnlinePlayer("Steve", STEVE))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Unknown or incomplete command, see below for error\nlist uuids<--[HERE]",
        "Unknown command. Try /help for a list of commands",
        "There are players online",
        None,
        42,
    ],
)
def test_parse_list_unrecognised(text):
    assert parse_list_response(text) is None


def status(online=2, sample=(), version="1.20.1", motd="A Server"):
    return ServerStatus(
        version_name=version,
        protocol=763,
        online=online,
        max_players=20,
        sample=sample,
        motd=motd,
    )


def test_roster_from_status_excludes_anonymous_entries():
    sample = (
        ("Alex", ALEX),
        ("Anonymous Player", "00000000-0000-0000-0000-000000000000"),
        ("anonymous player", STEVE),
        ("§aWelcome!", STEVE),
        ("Bob", "not-a-uuid"),
        ("Steve", STEVE.replace("-", "")),
    )
    roster = roster_from_status(status(online=7, sample=sample))
    assert roster.players == (OnlinePlayer("Alex", ALEX), OnlinePlayer("Steve", STEVE))
    assert (roster.online, roster.source, roster.version, roster.motd) == (
        7,
        "ping",
        "1.20.1",
        "A Server",
    )


# ----------------------------------------------------------------------
# MinecraftClient with fakes
# ----------------------------------------------------------------------


class FakeRcon:
    def __init__(self, replies=None, error=None):
        self.replies = replies or {}
        self.error = error
        self.commands = []
        self.closed = False
        self.final = []

    def command(self, command):
        self.commands.append(command)
        if self.error:
            raise self.error
        reply = self.replies.get(command, "")
        if isinstance(reply, Exception):
            raise reply
        return reply

    def send_final(self, command):
        self.final.append(command)
        if self.error:
            raise self.error
        return True

    def close(self):
        self.closed = True


class FakePinger:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, timeout):
        self.calls.append(timeout)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_client(rcon=None, pinger=None, clock=None):
    return MinecraftClient(
        "127.0.0.1",
        25565,
        25575,
        "pw" if rcon else None,
        rcon=rcon,
        pinger=pinger or FakePinger(PingError("down")),
        clock=clock or Clock(),
    )


UUID_LIST = f"There are 1 of a max of 20 players online: Alex ({ALEX})"
PLAIN_LIST = "There are 1 of a max of 20 players online: Alex"
EMPTY_LIST = "There are 0 of a max of 20 players online: "
UNKNOWN = "Unknown or incomplete command, see below for error"


def test_rcon_roster_prefers_list_uuids_and_remembers_it():
    rcon = FakeRcon({"list uuids": UUID_LIST})
    client = make_client(rcon, FakePinger(status()))
    roster = client.get_roster()
    assert roster.players == (OnlinePlayer("Alex", ALEX),)
    assert (roster.source, roster.version, roster.motd) == (
        "rcon",
        "1.20.1",
        "A Server",
    )
    client.get_roster()
    assert rcon.commands == ["list uuids", "list uuids"]


def test_unknown_list_uuids_falls_back_to_list_and_remembers():
    rcon = FakeRcon({"list uuids": UNKNOWN, "list": PLAIN_LIST})
    client = make_client(rcon)
    assert names(client.get_roster()) == ["Alex"]
    assert names(client.get_roster()) == ["Alex"]
    assert rcon.commands == ["list uuids", "list", "list"]


def test_list_uuids_without_uuids_is_used_once():
    rcon = FakeRcon({"list uuids": PLAIN_LIST, "list": PLAIN_LIST})
    client = make_client(rcon)
    assert names(client.get_roster()) == ["Alex"]
    client.get_roster()
    assert rcon.commands == ["list uuids", "list"]


def test_empty_roster_does_not_decide_the_capability():
    rcon = FakeRcon({"list uuids": EMPTY_LIST})
    client = make_client(rcon)
    assert client.get_roster().online == 0
    rcon.replies["list uuids"] = UUID_LIST
    assert client.get_roster().players[0].uuid == ALEX
    assert rcon.commands == ["list uuids", "list uuids"]


def test_unparseable_rcon_reply_uses_the_ping():
    rcon = FakeRcon({"list uuids": "???", "list": "???"})
    client = make_client(rcon, FakePinger(status(online=1, sample=(("Alex", ALEX),))))
    roster = client.get_roster()
    assert (roster.source, roster.online, names(roster)) == ("ping", 1, ["Alex"])
    # A failed `list` does not mark `list uuids` unsupported.
    client.get_roster()
    assert rcon.commands == ["list uuids", "list", "list uuids", "list"]


@pytest.mark.parametrize(
    "error", [RconUnavailable("down"), RconError("bad"), RconAuthError("pw")]
)
def test_rcon_failure_falls_back_to_ping(error):
    sample = (
        ("Alex", ALEX),
        ("Anonymous Player", "00000000-0000-0000-0000-000000000000"),
    )
    client = make_client(
        FakeRcon(error=error), FakePinger(status(online=2, sample=sample))
    )
    roster = client.get_roster()
    assert roster.source == "ping" and roster.online == 2
    assert roster.players == (OnlinePlayer("Alex", ALEX),)


def test_everything_down_returns_none():
    assert make_client(FakeRcon(error=RconUnavailable("x"))).get_roster() is None
    assert make_client().get_roster() is None


def test_unexpected_exceptions_never_escape(caplog):
    client = make_client(
        FakeRcon(error=RuntimeError("boom")), FakePinger(ValueError("bad"))
    )
    assert client.get_roster() is None
    client = make_client(None, FakePinger(KeyError("x")))
    assert client.get_roster() is None


def test_unexpected_rcon_exception_still_falls_back_to_ping():
    client = make_client(
        FakeRcon(error=RuntimeError("boom")), FakePinger(status(online=4))
    )
    roster = client.get_roster()
    assert (roster.source, roster.online) == ("ping", 4)


def test_without_rcon_only_the_ping_is_used():
    client = make_client(None, FakePinger(status(online=0)))
    assert not client.rcon_enabled
    assert client.get_roster().source == "ping"
    with pytest.raises(RconUnavailable):
        client.run_command("list")
    assert client.request_stop() is False


def test_server_info_cached_for_sixty_seconds():
    clock = Clock()
    pinger = FakePinger(status(version="1.20.1"))
    client = make_client(FakeRcon({"list uuids": UUID_LIST}), pinger, clock)
    assert client.get_roster().version == "1.20.1"
    clock.now += 59
    client.get_roster()
    assert len(pinger.calls) == 1
    clock.now += 2
    pinger.result = status(version="1.20.2")
    assert client.get_roster().version == "1.20.2"
    assert len(pinger.calls) == 2
    assert all(timeout <= 2.0 for timeout in pinger.calls)  # best effort, short


def test_failed_info_ping_keeps_last_info_and_never_fails_the_roster():
    clock = Clock()
    pinger = FakePinger(status(version="1.20.1", motd="Hello"))
    client = make_client(FakeRcon({"list uuids": UUID_LIST}), pinger, clock)
    client.get_roster()
    clock.now += 61
    pinger.result = PingError("timeout")
    roster = client.get_roster()
    assert (roster.source, roster.version, roster.motd) == ("rcon", "1.20.1", "Hello")
    clock.now += 30
    client.get_roster()
    assert len(pinger.calls) == 2  # failed attempts are rate-limited too


def test_info_absent_when_never_pinged():
    roster = make_client(FakeRcon({"list uuids": UUID_LIST})).get_roster()
    assert roster.version is None and roster.motd is None


def test_run_command_strips_formatting():
    client = make_client(FakeRcon({"seed": "Seed: [§a1234§r]"}))
    assert client.run_command("seed") == "Seed: [1234]"


@pytest.mark.parametrize(
    ("method", "args", "command", "reply", "success"),
    [
        (
            "kick_player",
            ("Alex",),
            "kick Alex",
            "Kicked Alex: Kicked by an operator",
            True,
        ),
        (
            "kick_player",
            ("Alex", "be\nnice"),
            "kick Alex be nice",
            "Kicked Alex from the game",
            True,
        ),
        ("kick_player", ("Ghost",), "kick Ghost", "No player was found", False),
        ("kick_player", ("Ghost",), "kick Ghost", "That player cannot be found", False),
        (
            "ban_player",
            ("Alex", "griefing"),
            "ban Alex griefing",
            "Banned Alex: griefing",
            True,
        ),
        ("ban_player", ("Alex",), "ban Alex", "Banned player Alex", True),
        (
            "ban_player",
            ("Alex",),
            "ban Alex",
            "Nothing changed. The player is already banned",
            True,
        ),
        ("ban_player", ("Ghost",), "ban Ghost", "That player does not exist", False),
        ("ban_player", ("Ghost",), "ban Ghost", "Could not ban player Ghost", False),
        ("unban_player", ("Alex",), "pardon Alex", "Unbanned Alex", True),
        ("unban_player", ("Alex",), "pardon Alex", "Unbanned player Alex", True),
        (
            "unban_player",
            ("Alex",),
            "pardon Alex",
            "Nothing changed. The player isn't banned",
            True,
        ),
        (
            "unban_player",
            ("Alex",),
            "pardon Alex",
            "Could not unban player Alex",
            False,
        ),
        ("ban_player", ("Alex",), "ban Alex", "", False),
    ],
)
def test_moderation_results(method, args, command, reply, success):
    rcon = FakeRcon({command: reply})
    result = getattr(make_client(rcon), method)(*args)
    assert rcon.commands == [command]
    assert result.success is success
    assert result.output == reply
    assert result.message


@pytest.mark.parametrize("name", ["", "two words", "a;b\nc", "§cRed", "x" * 65, None])
def test_moderation_rejects_bad_names_without_sending(name):
    rcon = FakeRcon()
    result = make_client(rcon).kick_player(name)
    assert result.success is False and rcon.commands == []


def test_moderation_when_rcon_fails():
    result = make_client(FakeRcon(error=RconUnavailable("down"))).ban_player("Alex")
    assert result.success is False and "down" in result.message
    assert make_client(None).unban_player("Alex").success is False


def test_say_request_stop_and_close():
    rcon = FakeRcon()
    client = make_client(rcon)
    assert client.say("hello\nworld") is True
    assert client.say("   ") is False
    assert rcon.commands == ["say hello world"]
    assert client.request_stop() is True and rcon.final == ["stop"]
    client.close()
    assert rcon.closed
    failing = make_client(FakeRcon(error=RconUnavailable("down")))
    assert failing.request_stop() is False
    assert failing.say("x") is False


def test_default_construction_without_password():
    client = MinecraftClient("127.0.0.1", 25565, 25575, None)
    assert client.rcon_enabled is False
    client.close()
    assert MinecraftClient("127.0.0.1", 25565, 25575, "pw").rcon_enabled is True
