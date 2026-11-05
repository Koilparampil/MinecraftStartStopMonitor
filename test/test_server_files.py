"""server.properties / JSON lists / misc server files."""

import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
import zlib

import pytest

from src.minecraft.server_files import (
    MAX_ICON_BYTES,
    list_mods,
    offline_uuid,
    read_eula,
    read_json_list,
    read_properties,
    read_server_icon,
    tail_file,
    update_properties,
    write_eula,
    write_json_list,
)

SAMPLE = (
    "#Minecraft server properties\n"
    "#Tue Nov 05 10:00:00 UTC 2026\n"
    "! bang comment\n"
    "motd=A \\u00A7aGreen\\u00a7r Server\n"
    "key\\=with\\:sep = value  with spaces\n"
    "spaced   :   x\n"
    "ws value\n"
    "   indented=yes\n"
    "multi = one, \\\n"
    "    two, \\\n"
    "    three\n"
    "# comment ending in a backslash \\\n"
    "next=after\n"
    "emoji=\\uD83D\\uDE00!\n"
    "tabs=a\\tb\\nc\n"
    "path=C:\\\\Games\\\\mc\n"
    "empty=\n"
    "onlykey\n"
    "dup=first\n"
    "dup=second\n"
)


@pytest.fixture
def props_file(tmp_path):
    path = tmp_path / "server.properties"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_read_properties_java_syntax(props_file):
    props = read_properties(str(props_file))
    assert props == {
        "motd": "A §aGreen§r Server",
        "key=with:sep": "value  with spaces",
        "spaced": "x",
        "ws": "value",
        "indented": "yes",
        "multi": "one, two, three",
        "next": "after",
        "emoji": "😀!",
        "tabs": "a\tb\nc",
        "path": "C:\\Games\\mc",
        "empty": "",
        "onlykey": "",
        "dup": "second",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a=1\r\nb=2\r\n", {"a": "1", "b": "2"}),
        ("a=1\rb=2", {"a": "1", "b": "2"}),
        ("a=x\\\n\nb=2\n", {"a": "x", "b": "2"}),  # empty continuation line ends it
        ("a=x\\\n  #not a comment\n", {"a": "x#not a comment"}),
        ("a=x\\", {"a": "x"}),  # dangling backslash at EOF
        ("a=\\ lead\n", {"a": " lead"}),
        ("a=trail  \n", {"a": "trail  "}),
        ("a=bad\\uZZ\n", {"a": "baduZZ"}),  # malformed \u: tolerated
        ("\ufeffa=1\n", {"a": "1"}),  # UTF-8 BOM
        ("", {}),
    ],
)
def test_read_properties_edge_cases(tmp_path, text, expected):
    path = tmp_path / "p.properties"
    path.write_bytes(text.encode("utf-8"))
    assert read_properties(str(path)) == expected


def test_read_properties_latin1_fallback(tmp_path):
    path = tmp_path / "p.properties"
    path.write_bytes("motd=\xa7aCaf\xe9\n".encode("latin-1"))
    assert read_properties(str(path)) == {"motd": "§aCafé"}


def test_read_properties_missing(tmp_path):
    assert read_properties(str(tmp_path / "nope.properties")) == {}


def test_update_preserves_everything_else(props_file):
    original = props_file.read_text(encoding="utf-8")
    assert (
        update_properties(str(props_file), {"motd": "New § motd", "spaced": "y"})
        is True
    )
    updated = props_file.read_text(encoding="utf-8")
    assert updated.splitlines()[:3] == original.splitlines()[:3]
    assert "motd=New \\u00A7 motd\n" in updated
    assert "spaced=y\n" in updated
    assert "multi = one, \\\n    two, \\\n    three\n" in updated
    assert "# comment ending in a backslash \\\n" in updated
    props = read_properties(str(props_file))
    assert props["motd"] == "New § motd"
    assert props["spaced"] == "y"
    assert props["multi"] == "one, two, three"
    assert props["key=with:sep"] == "value  with spaces"


def test_update_appends_missing_keys_in_order(tmp_path):
    path = tmp_path / "server.properties"
    path.write_text("#c\nexisting=1", encoding="utf-8")  # no trailing newline
    update_properties(
        str(path), {"enable-rcon": True, "rcon.port": 25575, "existing": "2"}
    )
    assert (
        path.read_text(encoding="utf-8")
        == "#c\nexisting=2\nenable-rcon=true\nrcon.port=25575\n"
    )


def test_update_rewrites_every_duplicate(props_file):
    update_properties(str(props_file), {"dup": "third"})
    text = props_file.read_text(encoding="utf-8")
    assert text.count("dup=third\n") == 2
    assert read_properties(str(props_file))["dup"] == "third"


def test_update_unchanged_returns_false_and_does_not_write(props_file):
    update_properties(str(props_file), {"motd": "Same"})
    before = os.stat(props_file).st_mtime_ns
    os.utime(props_file, ns=(before - 10**9, before - 10**9))
    assert update_properties(str(props_file), {"motd": "Same"}) is False
    assert os.stat(props_file).st_mtime_ns == before - 10**9


def test_update_creates_missing_file(tmp_path):
    path = tmp_path / "server.properties"
    assert update_properties(str(path), {"server-port": 25566}) is True
    assert path.read_text(encoding="utf-8") == "server-port=25566\n"


def test_update_keeps_crlf_and_latin1(tmp_path):
    path = tmp_path / "server.properties"
    path.write_bytes("motd=Caf\xe9\r\nport=1\r\n".encode("latin-1"))
    update_properties(str(path), {"port": "2", "new": "x"})
    assert path.read_bytes() == "motd=Caf\xe9\r\nport=2\r\nnew=x\r\n".encode("latin-1")


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        " leading space",
        "trailing space ",
        "a=b:c#d!e",
        "back\\slash",
        "tab\tnew\nline\rcr\fff",
        "§aCafé 😀 \u2028",
        "\\u0041 literal",
        "",
    ],
)
def test_update_round_trips_any_value(tmp_path, value):
    path = tmp_path / "server.properties"
    update_properties(str(path), {"k": value, "odd key=:": value})
    assert read_properties(str(path)) == {"k": value, "odd key=:": value}
    path.read_bytes().decode("ascii")  # everything outside printable ASCII is escaped


def test_update_is_atomic_and_keeps_mode(tmp_path):
    path = tmp_path / "server.properties"
    path.write_text("a=1\n", encoding="utf-8")
    os.chmod(path, 0o640)
    update_properties(str(path), {"a": "2"})
    assert sorted(os.listdir(tmp_path)) == ["server.properties"]  # no temp files left
    if os.name != "nt":
        assert os.stat(path).st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_update_follows_symlinks(tmp_path):
    real = tmp_path / "real.properties"
    real.write_text("a=1\n", encoding="utf-8")
    link = tmp_path / "server.properties"
    link.symlink_to(real)
    update_properties(str(link), {"a": "2"})
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "a=2\n"


# ----------------------------------------------------------------------
# JSON lists
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [None, "", "not json", "{}", '{"a": 1}', "[" * 100000],
)
def test_read_json_list_tolerates_bad_files(tmp_path, content):
    path = tmp_path / "banned-players.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert read_json_list(str(path)) == []


def test_read_json_list_skips_non_objects(tmp_path):
    path = tmp_path / "ops.json"
    path.write_bytes(b'\xef\xbb\xbf[{"name": "A"}, 3, "x", null, {"name": "B"}]')
    assert read_json_list(str(path)) == [{"name": "A"}, {"name": "B"}]


def test_write_json_list_round_trip(tmp_path):
    path = tmp_path / "banned-players.json"
    entries = [{"uuid": "u", "name": "Zoë", "reason": "x"}]
    write_json_list(str(path), entries)
    text = path.read_text(encoding="utf-8")
    assert text.startswith('[\n  {\n    "uuid": "u"')  # vanilla 2-space style
    assert json.loads(text) == entries
    assert read_json_list(str(path)) == entries
    assert sorted(os.listdir(tmp_path)) == ["banned-players.json"]


# ----------------------------------------------------------------------
# Misc
# ----------------------------------------------------------------------


def _java_name_uuid(name):
    """Independent port of Java's UUID.nameUUIDFromBytes."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    text = digest.hex()
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def test_offline_uuid_known_value():
    assert offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f"


@pytest.mark.parametrize("name", ["Steve", "alex", "Player_123", "Zoë"])
def test_offline_uuid_matches_java_algorithm(name):
    assert offline_uuid(name) == _java_name_uuid(name)


def test_list_mods(tmp_path):
    assert list_mods(str(tmp_path)) == []
    mods = tmp_path / "mods"
    mods.mkdir()
    for name in ["zeta.jar", "Alpha.JAR", "beta.jar.disabled", "notes.txt"]:
        (mods / name).write_bytes(b"")
    (mods / "folder.jar").mkdir()
    assert list_mods(str(tmp_path)) == ["Alpha.JAR", "zeta.jar"]


def _png(width, height, extra=b""):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * (width * 4) for _ in range(height))
    body = chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + extra
    return b"\x89PNG\r\n\x1a\n" + body + chunk(b"IEND", b"")


def test_read_server_icon(tmp_path):
    assert read_server_icon(str(tmp_path)) is None
    icon = _png(64, 64)
    (tmp_path / "server-icon.png").write_bytes(icon)
    value = read_server_icon(str(tmp_path))
    assert value.startswith("data:image/png;base64,")
    assert base64.b64decode(value.split(",", 1)[1]) == icon


@pytest.mark.parametrize(
    "data",
    [
        _png(32, 32),
        b"GIF89a not a png",
        _png(64, 64, extra=b"x" * (MAX_ICON_BYTES + 1)),
    ],
)
def test_read_server_icon_rejects_unusable_files(tmp_path, data):
    (tmp_path / "server-icon.png").write_bytes(data)
    assert read_server_icon(str(tmp_path)) is None


def test_eula_round_trip(tmp_path):
    assert read_eula(str(tmp_path)) is None
    write_eula(str(tmp_path), False)
    assert read_eula(str(tmp_path)) is False
    text = (tmp_path / "eula.txt").read_text(encoding="utf-8")
    assert text.startswith("#By changing the setting below to TRUE")
    write_eula(str(tmp_path), True)
    assert read_eula(str(tmp_path)) is True
    assert (
        (tmp_path / "eula.txt").read_text(encoding="utf-8").startswith("#By changing")
    )


@pytest.mark.parametrize(
    ("value", "expected"), [("TRUE", True), ("true ", False), ("yes", False)]
)
def test_read_eula_is_as_strict_as_the_server(tmp_path, value, expected):
    (tmp_path / "eula.txt").write_text(f"eula={value}\n", encoding="utf-8")
    assert read_eula(str(tmp_path)) is expected


def test_tail_file_small(tmp_path):
    path = tmp_path / "latest.log"
    assert tail_file(str(path)) == []
    path.write_bytes(b"one\r\ntwo\nthree")
    assert tail_file(str(path)) == ["one", "two", "three"]
    assert tail_file(str(path), lines=2) == ["two", "three"]
    assert tail_file(str(path), lines=0) == []


def test_tail_file_large_reads_only_the_end(tmp_path):
    path = tmp_path / "latest.log"
    lines = [f"[10:00:00] [Server thread/INFO]: line {i} é" for i in range(20000)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert tail_file(str(path), lines=5) == lines[-5:]
    # The byte cap wins over the line count; the cut first line is dropped.
    capped = tail_file(str(path), lines=10000, max_bytes=1000)
    assert 0 < len(capped) < 30
    assert capped == lines[-len(capped) :]


def test_tail_file_replaces_invalid_utf8(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"ok\n\xff\xfe bad\n")
    assert tail_file(str(path)) == ["ok", "\ufffd\ufffd bad"]


# ----------------------------------------------------------------------
# Appending after a dangling continuation (Java would join the next line)
# ----------------------------------------------------------------------

DANGLING = [
    ("level-name=C:\\worlds\\", {"level-name": "C:worlds"}),
    ("level-name=C:\\worlds\\\n", {"level-name": "C:worlds"}),
    ("motd=hi\r\nlevel-name=a\\\r\n", {"motd": "hi", "level-name": "a"}),
    ("a=x\\\n\\", {"a": "x"}),  # dropping one backslash exposes another
    ("a=x\\\n  y\\", {"a": "xy"}),
    ("a=\\\\\\", {"a": "\\"}),
    ("key\\", {"key": ""}),
    ("a=x\\\n\n", {"a": "x"}),  # already ended by an empty line
    ("#comment\\", {}),  # comments never continue
]
NEW_KEYS = {"enable-rcon": "true", "rcon.port": "25575", "rcon.password": "p=w:d #1 é"}


@pytest.mark.parametrize(("text", "before"), DANGLING)
def test_append_after_a_dangling_continuation(tmp_path, text, before):
    path = tmp_path / "server.properties"
    path.write_bytes(text.encode("utf-8"))
    assert read_properties(str(path)) == before
    assert update_properties(str(path), NEW_KEYS) is True
    assert read_properties(str(path)) == {**before, **NEW_KEYS}
    written = path.read_bytes()
    assert update_properties(str(path), NEW_KEYS) is False  # idempotent
    assert path.read_bytes() == written


JAVA_DUMP = r"""
import java.io.*; import java.nio.charset.StandardCharsets; import java.nio.file.*; import java.util.*;
public class Dump {
    static String hex(String s) {
        StringBuilder b = new StringBuilder("x");
        for (char c : s.toCharArray()) b.append(String.format("%04x", (int) c));
        return b.toString();
    }
    public static void main(String[] args) throws Exception {
        for (String a : args) {
            Properties p = new Properties();
            try (Reader r = Files.newBufferedReader(Paths.get(a), StandardCharsets.UTF_8)) { p.load(r); }
            System.out.println("FILE");
            for (String k : new TreeSet<>(p.stringPropertyNames()))
                System.out.println(hex(k) + " " + hex(p.getProperty(k)));
        }
    }
}
"""


def _java_properties(tmp_path, files):
    source = tmp_path / "Dump.java"
    source.write_text(JAVA_DUMP, encoding="utf-8")
    out = subprocess.run(
        ["java", str(source), *map(str, files)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    result = []
    for line in out.splitlines():
        if line == "FILE":
            result.append({})
        elif line:
            key, value = (
                bytes.fromhex(part[1:]).decode("utf-16-be", "surrogatepass")
                for part in line.split(" ")
            )
            result[-1][key] = value
    return result


@pytest.mark.skipif(shutil.which("java") is None, reason="needs a Java runtime")
def test_java_reads_updated_files_like_we_do(tmp_path):
    """Differential check against java.util.Properties (what the server uses)."""
    files = []
    for index, text in enumerate([SAMPLE, *(text for text, _ in DANGLING)]):
        path = tmp_path / f"case{index}.properties"
        path.write_bytes(text.encode("utf-8"))
        update_properties(
            str(path), {**NEW_KEYS, "motd": " §a lead", "multi": "a\\b\nc"}
        )
        files.append(path)
    assert _java_properties(tmp_path, files) == [
        read_properties(str(path)) for path in files
    ]


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to chown"
)
def test_writes_keep_the_owner(tmp_path):
    """A wizard run with sudo must not leave root-owned server files."""
    server = tmp_path / "server"
    server.mkdir()
    os.chown(server, 4321, 4322)
    path = server / "server.properties"
    path.write_text("a=1\n", encoding="utf-8")
    os.chown(path, 4321, 4322)
    update_properties(str(path), {"a": "2"})
    write_eula(str(server), True)  # a new file takes the directory's owner
    for written in (path, server / "eula.txt"):
        info = os.stat(written)
        assert (info.st_uid, info.st_gid) == (4321, 4322)
