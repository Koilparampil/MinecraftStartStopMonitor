"""Readers/writers for files in the Minecraft server directory.

``.properties`` files (server.properties, eula.txt) follow the rules of
``java.util.Properties.load``: ``#``/``!`` comments, ``=``/``:``/whitespace
separators, backslash escapes (including ``\\uXXXX``) and line continuations.
Updates rewrite only the lines of the keys being changed, so comments, order
and unknown keys survive. Every write is atomic (temp file + ``os.replace``).
"""

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import struct
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

EULA_URL = "https://aka.ms/MinecraftEULA"
# The vanilla client rejects status responses longer than 32767 characters, and
# the favicon is sent base64-encoded inside that JSON: keep icons comfortably below.
MAX_ICON_BYTES = 20 * 1024

_WHITESPACE = " \t\f"
_NEWLINE_SPLIT = re.compile(r"(\r\n|\r|\n)")
_TERMINATOR = re.compile(r"(\r\n|\r|\n)\Z")
_UNESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)", re.DOTALL)
_SIMPLE_UNESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
_WRITE_ESCAPES = {
    "\t": "\\t",
    "\n": "\\n",
    "\r": "\\r",
    "\f": "\\f",
    "\\": "\\\\",
    "=": "\\=",
    ":": "\\:",
    "#": "\\#",
    "!": "\\!",
}
_UTF8_BOM = b"\xef\xbb\xbf"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ----------------------------------------------------------------------
# .properties
# ----------------------------------------------------------------------


@dataclass
class _Entry:
    """One logical line of a .properties file, with its exact source text."""

    raw: str  # source text, continuation lines and terminators included
    key: str | None = None  # None for comments and blank lines
    value: str | None = None


@dataclass(frozen=True)
class _Source:
    text: str
    encoding: str
    bom: bytes


def read_properties(path: str) -> dict[str, str]:
    """Parse a Java .properties file (server.properties). {} if missing.

    Duplicate keys: the last one wins, as in Java. Other read errors
    (permissions...) propagate as OSError.
    """
    source = _read_source(path)
    if source is None:
        return {}
    return {
        entry.key: entry.value
        for entry in _parse_entries(source.text)
        if entry.key is not None
    }


def update_properties(path: str, updates: dict[str, Any]) -> bool:
    """Set keys, preserving comments/order/unknown keys; create the file if
    missing; atomic write. Returns True if the file content changed.

    Values are converted with ``str()`` (booleans become ``true``/``false``)
    and escaped like ``Properties.store``; non-ASCII characters are written as
    ``\\uXXXX`` so every server version reads them back correctly. Existing
    lines for an updated key are rewritten in place; new keys are appended.
    """
    source = _read_source(path)
    text = source.text if source else ""
    entries = _parse_entries(text)
    newline = _detect_newline(text)
    pending = {str(key): _property_string(value) for key, value in updates.items()}

    found = set()
    for entry in entries:
        if entry.key in pending:
            line = _format_line(entry.key, pending[entry.key])
            entry.raw = line + _line_terminator(entry.raw)
            found.add(entry.key)

    missing = [key for key in pending if key not in found]
    if missing:
        _close_dangling_continuation(entries)
    new_text = "".join(entry.raw for entry in entries)
    if missing:
        if new_text and not new_text.endswith(("\n", "\r")):
            new_text += newline
        new_text += "".join(
            _format_line(key, pending[key]) + newline for key in missing
        )

    if source is not None and new_text == text:
        return False
    encoding = source.encoding if source else "utf-8"
    bom = source.bom if source else b""
    _atomic_write(path, bom + new_text.encode(encoding))
    return True


def _close_dangling_continuation(entries: list[_Entry]) -> None:
    """Drop the backslash ending the file's last line when it is a
    continuation: Java ignores it at end of file, but it would join whatever is
    appended after it into that value (``level-name=C:\\worlds\\``)."""
    if not entries or entries[-1].key is None:
        return
    raw = entries[-1].raw
    while True:
        terminator = _line_terminator(raw)
        content = raw[: len(raw) - len(terminator)]
        # Dropping it can expose an earlier continuation ("a=x\\\n\\"): repeat.
        if not _continues(_NEWLINE_SPLIT.split(content)[-1]):
            break
        raw = content[:-1] + terminator
    entries[-1].raw = raw


def _read_source(path: str) -> _Source | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None
    bom = b""
    if data.startswith(_UTF8_BOM):
        bom, data = _UTF8_BOM, data[len(_UTF8_BOM) :]
    # Modern servers read UTF-8 and fall back to ISO-8859-1 (older versions
    # always used ISO-8859-1 plus \\u escapes); do the same.
    try:
        return _Source(data.decode("utf-8"), "utf-8", bom)
    except UnicodeDecodeError:
        return _Source(data.decode("latin-1"), "latin-1", bom)


def _natural_lines(text: str) -> list[tuple[str, str]]:
    """(content, terminator) pairs. Java only knows \\n, \\r and \\r\\n."""
    parts = _NEWLINE_SPLIT.split(text)
    lines = [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]
    if parts[-1]:
        lines.append((parts[-1], ""))
    return lines


def _parse_entries(text: str) -> list[_Entry]:
    lines = _natural_lines(text)
    entries = []
    index = 0
    while index < len(lines):
        consumed, logical = _logical_line(lines, index)
        raw = "".join(
            content + term for content, term in lines[index : index + consumed]
        )
        index += consumed
        if logical is None:
            entries.append(_Entry(raw))
        else:
            key, value = _split_key_value(logical)
            entries.append(_Entry(raw, key, value))
    return entries


def _logical_line(lines: list[tuple[str, str]], start: int) -> tuple[int, str | None]:
    """Natural lines consumed and the joined logical line (None: blank/comment)."""
    content = lines[start][0].lstrip(_WHITESPACE)
    if not content or content[0] in "#!":
        return 1, None
    parts = []
    index = start
    while _continues(content) and index + 1 < len(lines):
        parts.append(content[:-1])
        index += 1
        content = lines[index][0].lstrip(_WHITESPACE)
        if not content:  # an empty continuation line ends the logical line
            return index - start + 1, "".join(parts)
    if _continues(content):  # a backslash at end of file is dropped
        content = content[:-1]
    parts.append(content)
    return index - start + 1, "".join(parts)


def _continues(content: str) -> bool:
    """True when the line ends with an odd number of backslashes."""
    trailing = len(content) - len(content.rstrip("\\"))
    return trailing % 2 == 1


def _split_key_value(line: str) -> tuple[str, str]:
    key_end = value_start = len(line)
    has_separator = False
    escaped = False
    for index, char in enumerate(line):
        if not escaped and char in "=:":
            key_end, value_start, has_separator = index, index + 1, True
            break
        if not escaped and char in _WHITESPACE:
            key_end, value_start = index, index + 1
            break
        escaped = char == "\\" and not escaped
    while value_start < len(line):
        char = line[value_start]
        if char not in _WHITESPACE:
            if has_separator or char not in "=:":
                break
            has_separator = True
        value_start += 1
    return _unescape(line[:key_end]), _unescape(line[value_start:])


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text
    result = _UNESCAPE.sub(_unescape_match, text)
    if any("\ud800" <= char <= "\udfff" for char in result):
        # \\uXXXX escapes are UTF-16 code units: merge surrogate pairs.
        result = result.encode("utf-16-le", "surrogatepass").decode(
            "utf-16-le", "replace"
        )
    return result


def _unescape_match(match: re.Match) -> str:
    escape = match.group(1)
    if len(escape) == 5:  # uXXXX
        return chr(int(escape[1:], 16))
    return _SIMPLE_UNESCAPES.get(escape, escape)


def _escape(text: str, *, is_key: bool) -> str:
    out = []
    for index, char in enumerate(text):
        if char == " ":
            out.append("\\ " if is_key or index == 0 else " ")
        elif char in _WRITE_ESCAPES:
            out.append(_WRITE_ESCAPES[char])
        elif " " < char <= "~":
            out.append(char)
        else:
            units = char.encode("utf-16-be", "surrogatepass")
            out.extend(
                f"\\u{int.from_bytes(units[i : i + 2], 'big'):04X}"
                for i in range(0, len(units), 2)
            )
    return "".join(out)


def _format_line(key: str, value: str) -> str:
    return f"{_escape(key, is_key=True)}={_escape(value, is_key=False)}"


def _property_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _line_terminator(raw: str) -> str:
    match = _TERMINATOR.search(raw)
    return match.group(1) if match else ""


def _detect_newline(text: str) -> str:
    for newline in ("\r\n", "\n", "\r"):
        if newline in text:
            return newline
    return "\n"


# ----------------------------------------------------------------------
# Atomic writes
# ----------------------------------------------------------------------


def _atomic_write(path: str, data: bytes) -> None:
    """Write via a temp file in the same directory, then os.replace().

    Symlinks are followed (the link survives). The existing file's mode and,
    on POSIX, its owner are kept (a wizard run with sudo must not leave
    root-owned files the server can no longer save); new files get 0644 and
    the directory's owner.
    """
    target = os.path.realpath(os.fspath(path))
    directory = os.path.dirname(target)
    try:
        existing = os.stat(target)
        mode, owner = existing.st_mode & 0o7777, (existing.st_uid, existing.st_gid)
    except OSError:
        mode, owner = 0o644, _owner_of(directory)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _set_owner(tmp_path, owner)  # before chmod: chown clears setuid/setgid
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, mode)
        _replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _owner_of(path: str) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_uid, info.st_gid


def _set_owner(path: str, owner: tuple[int, int] | None) -> None:
    """chown to `owner` when it differs (POSIX; only root can change the uid,
    so a failure just keeps the writer as owner, like a plain write would)."""
    if owner is None or not hasattr(os, "chown"):
        return
    with contextlib.suppress(OSError):
        if _owner_of(path) != owner:
            os.chown(path, *owner)


def _replace(source: str, target: str) -> None:
    attempts = 5 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            # Windows: another process (an editor, the server, an antivirus)
            # briefly holds the file open.
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


# ----------------------------------------------------------------------
# JSON lists (banned-players.json, whitelist.json, ops.json, usercache.json...)
# ----------------------------------------------------------------------


def read_json_list(path: str) -> list[dict]:
    """banned-players.json / whitelist.json / usercache.json / ops.json /
    banned-ips.json. [] if missing or invalid (non-object entries are skipped)."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return []
    except (OSError, ValueError, RecursionError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.warning("Ignoring %s: expected a JSON list", path)
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def write_json_list(path: str, entries: list[dict]) -> None:
    """Atomic write, 2-space indent (vanilla style)."""
    text = json.dumps(list(entries), indent=2, ensure_ascii=False)
    _atomic_write(path, text.encode("utf-8"))


# ----------------------------------------------------------------------
# Misc server files
# ----------------------------------------------------------------------


def offline_uuid(name: str) -> str:
    """Offline-mode UUID (v3 of 'OfflinePlayer:<name>'), dashed lowercase.

    Same as Java's ``UUID.nameUUIDFromBytes(("OfflinePlayer:" + name).getBytes(UTF_8))``.
    """
    digest = hashlib.md5(f"OfflinePlayer:{name}".encode(), usedforsecurity=False)
    return str(uuid.UUID(bytes=digest.digest(), version=3))


def list_mods(server_dir: str) -> list[str]:
    """Sorted jar file names in <server_dir>/mods (ignores *.disabled)."""
    mods_dir = os.path.join(server_dir, "mods")
    try:
        with os.scandir(mods_dir) as scan:
            names = [
                entry.name
                for entry in scan
                if entry.name.lower().endswith(".jar") and entry.is_file()
            ]
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        logger.warning("Could not list %s: %s", mods_dir, exc)
        return []
    return sorted(names, key=lambda name: (name.lower(), name))


def read_server_icon(server_dir: str) -> str | None:
    """'data:image/png;base64,...' for <server_dir>/server-icon.png, else None.

    Like the vanilla server, only a 64x64 PNG is used; icons larger than
    MAX_ICON_BYTES are ignored because they would not fit in a status response.
    """
    path = os.path.join(server_dir, "server-icon.png")
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ICON_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_ICON_BYTES:
        logger.warning("Ignoring %s: larger than %d bytes", path, MAX_ICON_BYTES)
        return None
    if not _is_64px_png(data):
        logger.warning("Ignoring %s: not a 64x64 PNG image", path)
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _is_64px_png(data: bytes) -> bool:
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return False
    width, height = struct.unpack(">II", data[16:24])
    return width == 64 and height == 64


def read_eula(server_dir: str) -> bool | None:
    """True/False from eula.txt, None if the file is missing.

    Mirrors the server: only a value of exactly ``true`` (any case) accepts.
    """
    path = os.path.join(server_dir, "eula.txt")
    if not os.path.isfile(path):
        return None
    return read_properties(path).get("eula", "").lower() == "true"


def write_eula(server_dir: str, accepted: bool) -> None:
    path = os.path.join(server_dir, "eula.txt")
    value = "true" if accepted else "false"
    if os.path.exists(path):
        update_properties(path, {"eula": value})
        return
    stamp = time.strftime("%a %b %d %H:%M:%S %Z %Y")
    text = (
        "#By changing the setting below to TRUE you are indicating your agreement "
        f"to our EULA ({EULA_URL}).\n#{stamp}\neula={value}\n"
    )
    _atomic_write(path, text.encode("utf-8"))


def tail_file(path: str, lines: int = 200, max_bytes: int = 262144) -> list[str]:
    """Last `lines` lines of a text file (reads at most max_bytes from the end).
    [] if missing. Decodes UTF-8 with replacement."""
    if lines <= 0 or max_bytes <= 0:
        return []
    try:
        data, from_start = _read_tail_bytes(path, lines, max_bytes)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return []
    text_lines = data.decode("utf-8", errors="replace").split("\n")
    if not from_start:
        text_lines = text_lines[1:]  # the first line was cut by the byte limit
    if text_lines and text_lines[-1] == "":
        text_lines.pop()  # the file ends with a newline
    return [line.removesuffix("\r") for line in text_lines[-lines:]]


def _read_tail_bytes(path: str, lines: int, max_bytes: int) -> tuple[bytes, bool]:
    """Read blocks backwards until `lines` complete lines (or max_bytes) are
    covered. Returns the bytes and whether they start at the file start."""
    block_size = 8192
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        limit = max(0, position - max_bytes)
        chunks = []
        newlines = 0
        while position > limit and newlines <= lines:
            size = min(block_size, position - limit)
            position -= size
            handle.seek(position)
            chunk = handle.read(size)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
    return b"".join(reversed(chunks)), position == 0
