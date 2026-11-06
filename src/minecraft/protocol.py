"""Minecraft Java Edition wire-format primitives.

VarInt / String / UShort / Long codecs, uncompressed packet framing
(``VarInt length + VarInt packet id + payload``) over sockets with size limits
and an overall deadline, text-component flattening and ``§`` code stripping,
and ``safe_for_log()`` for peer text that goes into the log.

Everything that parses peer data raises ProtocolError (a ValueError) for
malformed input, ``TimeoutError`` when the deadline passes and
ConnectionClosed (an OSError) on EOF, so callers can catch
``(OSError, ProtocolError)`` and never see anything else.
"""

import re
import socket
import struct
import time
from collections.abc import Callable
from typing import Any

MAX_STRING_LENGTH = 32767  # vanilla default for String(n) fields
DEFAULT_MAX_PACKET = 2097151  # largest length a 3-byte VarInt frame header allows
_MAX_COMPONENT_DEPTH = 64
_FORMATTING_CODE = re.compile("§.?", re.DOTALL)


class ProtocolError(ValueError):
    """Malformed, truncated or oversized protocol data."""


class ConnectionClosed(ConnectionError):
    """The peer closed the connection before the expected data arrived."""


# ----------------------------------------------------------------------
# VarInt
# ----------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    """Encode a 32-bit signed int (negative values take 5 bytes)."""
    if not -(2**31) <= value < 2**31:
        raise ProtocolError(f"VarInt out of range: {value}")
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if not value:
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _varint_from(next_byte: Callable[[], int]) -> int:
    """Decode a VarInt from a byte source: at most 5 bytes, 32-bit two's
    complement. Non-minimal encodings are accepted (as vanilla does)."""
    result = 0
    for index in range(5):
        byte = next_byte()
        result |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            if index == 4 and byte > 0x0F:
                raise ProtocolError("VarInt does not fit in 32 bits")
            return result - (1 << 32) if result & 0x80000000 else result
    raise ProtocolError("VarInt is longer than 5 bytes")


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a VarInt at `offset`; returns (value, offset after it)."""
    position = offset

    def next_byte() -> int:
        nonlocal position
        if position >= len(data):
            raise ProtocolError("truncated VarInt")
        position += 1
        return data[position - 1]

    value = _varint_from(next_byte)
    return value, position


# ----------------------------------------------------------------------
# Strings and fixed-size numbers
# ----------------------------------------------------------------------


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units, which is what Java's String.length() counts."""
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


def encode_string(text: str, max_length: int | None = MAX_STRING_LENGTH) -> bytes:
    """VarInt byte length + UTF-8. Enforces the String(n) limits of the reader."""
    data = text.encode("utf-8", errors="replace")
    if max_length is not None and (
        utf16_length(text) > max_length or len(data) > max_length * 3
    ):
        raise ProtocolError(f"string longer than {max_length} characters")
    return encode_varint(len(data)) + data


def decode_string(
    data: bytes, offset: int = 0, max_length: int | None = MAX_STRING_LENGTH
) -> tuple[str, int]:
    """Decode a String(max_length) at `offset`; returns (text, new offset).

    Like vanilla: the byte length may not exceed 3 * max_length, the decoded
    text may not exceed max_length UTF-16 units; invalid UTF-8 is replaced.
    `max_length=None` only bounds the string by the data available.
    """
    length, offset = decode_varint(data, offset)
    if length < 0:
        raise ProtocolError("negative string length")
    if max_length is not None and length > max_length * 3:
        raise ProtocolError(f"string of {length} bytes exceeds String({max_length})")
    end = offset + length
    if end > len(data):
        raise ProtocolError("truncated string")
    text = bytes(data[offset:end]).decode("utf-8", errors="replace")
    if max_length is not None and utf16_length(text) > max_length:
        raise ProtocolError(f"string exceeds String({max_length})")
    return text, end


def encode_ushort(value: int) -> bytes:
    try:
        return struct.pack(">H", value)
    except struct.error as exc:
        raise ProtocolError(f"unsigned short out of range: {value}") from exc


def encode_long(value: int) -> bytes:
    try:
        return struct.pack(">q", value)
    except struct.error as exc:
        raise ProtocolError(f"long out of range: {value}") from exc


def _unpack(fmt: str, data: bytes, offset: int) -> tuple[int, int]:
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(data):
        raise ProtocolError("truncated packet")
    return struct.unpack_from(fmt, data, offset)[0], offset + size


def decode_ushort(data: bytes, offset: int = 0) -> tuple[int, int]:
    return _unpack(">H", data, offset)


def decode_long(data: bytes, offset: int = 0) -> tuple[int, int]:
    return _unpack(">q", data, offset)


class PacketReader:
    """Sequential decoder over one packet payload."""

    def __init__(self, data: bytes):
        self._data = bytes(data)
        self._offset = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._offset

    def read_varint(self) -> int:
        value, self._offset = decode_varint(self._data, self._offset)
        return value

    def read_string(self, max_length: int | None = MAX_STRING_LENGTH) -> str:
        text, self._offset = decode_string(self._data, self._offset, max_length)
        return text

    def read_ushort(self) -> int:
        value, self._offset = decode_ushort(self._data, self._offset)
        return value

    def read_long(self) -> int:
        value, self._offset = decode_long(self._data, self._offset)
        return value

    def read_bytes(self, size: int) -> bytes:
        if size < 0 or size > self.remaining:
            raise ProtocolError("truncated packet")
        data = self._data[self._offset : self._offset + size]
        self._offset += size
        return data


# ----------------------------------------------------------------------
# Framing over sockets
# ----------------------------------------------------------------------


def frame_packet(packet_id: int, payload: bytes = b"") -> bytes:
    """Uncompressed frame: VarInt length + VarInt packet id + payload."""
    body = encode_varint(packet_id) + payload
    return encode_varint(len(body)) + body


def deadline_after(seconds: float | None) -> float | None:
    return None if seconds is None else time.monotonic() + seconds


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timed out")
    return remaining


def send_all(sock: socket.socket, data: bytes, deadline: float | None = None) -> None:
    sock.settimeout(_remaining(deadline))
    sock.sendall(data)


def write_packet(
    sock: socket.socket,
    packet_id: int,
    payload: bytes = b"",
    deadline: float | None = None,
) -> None:
    send_all(sock, frame_packet(packet_id, payload), deadline)


class SocketReader:
    """Buffered reads from a socket, bounded by an overall deadline.

    The deadline (a ``time.monotonic()`` value, None = blocking) applies to
    the sum of all waits, so a client trickling one byte at a time cannot hold
    the connection beyond it. Reads consume the buffer only once complete,
    so a timeout never leaves a half-read value behind.
    """

    def __init__(self, sock: socket.socket, deadline: float | None = None):
        self.sock = sock
        self.deadline = deadline
        self._buffer = bytearray()

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def _fill(self, deadline: float | None) -> None:
        self.sock.settimeout(_remaining(deadline))
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionClosed("connection closed by peer")
        self._buffer += chunk

    def _deadline(self, deadline: float | None) -> float | None:
        return self.deadline if deadline is None else deadline

    def peek(self, size: int, deadline: float | None = None) -> bytes:
        deadline = self._deadline(deadline)
        while len(self._buffer) < size:
            self._fill(deadline)
        return bytes(self._buffer[:size])

    def read_exact(self, size: int, deadline: float | None = None) -> bytes:
        data = self.peek(size, deadline)
        del self._buffer[:size]
        return data

    def peek_byte(self, deadline: float | None = None) -> int:
        return self.peek(1, deadline)[0]

    def read_varint(self, deadline: float | None = None) -> int:
        value, size = self._peek_varint(deadline)
        del self._buffer[:size]
        return value

    def _peek_varint(self, deadline: float | None) -> tuple[int, int]:
        """(value, encoded size) of the VarInt at the start of the buffer."""
        deadline = self._deadline(deadline)
        size = 0

        def next_byte() -> int:
            nonlocal size
            size += 1
            return self.peek(size, deadline)[size - 1]

        value = _varint_from(next_byte)
        return value, size

    def read_packet(
        self, max_size: int = DEFAULT_MAX_PACKET, deadline: float | None = None
    ) -> tuple[int, bytes]:
        """Read one frame; returns (packet id, payload)."""
        length, header = self._peek_varint(deadline)
        if length <= 0 or length > max_size:
            raise ProtocolError(f"invalid packet length {length} (limit {max_size})")
        frame = self.read_exact(header + length, deadline)
        packet_id, offset = decode_varint(frame, header)
        return packet_id, frame[offset:]


# ----------------------------------------------------------------------
# Legacy (pre-1.7) server list ping
# ----------------------------------------------------------------------


def encode_legacy_kick(text: str) -> bytes:
    """0xFF kick packet: UShort length in UTF-16 units + UTF-16BE text."""
    data = text.encode("utf-16-be", errors="replace")[: 0xFFFF * 2]
    return b"\xff" + struct.pack(">H", len(data) // 2) + data


# ----------------------------------------------------------------------
# Text components
# ----------------------------------------------------------------------


def flatten_text(component: Any) -> str:
    """Plain concatenated text of a chat component (str, dict or list).

    Handles ``text``, ``translate`` (its ``fallback`` or the key itself),
    ``keybind``, ``selector``, ``score`` and nested ``extra`` children.
    Formatting codes inside the text are kept; see strip_formatting.
    """
    parts: list[str] = []
    _flatten(component, parts, 0)
    return "".join(parts)


def _flatten(component: Any, parts: list[str], depth: int) -> None:
    if component is None or depth > _MAX_COMPONENT_DEPTH:
        return
    if isinstance(component, str):
        parts.append(component)
    elif isinstance(component, bool):
        parts.append("true" if component else "false")
    elif isinstance(component, (int, float)):
        parts.append(str(component))
    elif isinstance(component, list):
        for child in component:
            _flatten(child, parts, depth + 1)
    elif isinstance(component, dict):
        _flatten(_own_text(component), parts, depth + 1)
        extra = component.get("extra")
        if isinstance(extra, list):
            for child in extra:
                _flatten(child, parts, depth + 1)


def _own_text(component: dict) -> Any:
    if "text" in component:
        return component["text"]
    if "translate" in component:
        fallback = component.get("fallback")
        return fallback if isinstance(fallback, str) else component["translate"]
    for key in ("keybind", "selector"):
        if key in component:
            return component[key]
    score = component.get("score")
    if isinstance(score, dict):
        return score.get("value")
    return None


def strip_formatting(text: str) -> str:
    """Remove ``§x`` formatting codes (the client consumes § plus one char)."""
    if "§" not in text:
        return text
    return _FORMATTING_CODE.sub("", text)


def plain_text(component: Any) -> str:
    """flatten_text + strip_formatting."""
    return strip_formatting(flatten_text(component))


# ----------------------------------------------------------------------
# Logging peer data
# ----------------------------------------------------------------------

LOG_TEXT_LIMIT = 200


def safe_for_log(text: Any, limit: int = LOG_TEXT_LIMIT) -> str:
    """*text* from a peer (a player name, a URL path), fit for one log line.

    Backslashes and every character ``str.isprintable()`` rejects (control
    characters such as newlines and ANSI escapes, line and paragraph
    separators, invisible formatting such as bidi overrides) are written as
    Python escapes (``\\n``, ``\\x1b``, ``\\u2028``). So a client can neither
    start a forged log line nor send escape sequences to the operator's
    terminal, and an escape in the output always means the client sent that
    character. Longer text is cut to *limit* characters, ending in "...".
    """
    text = str(text)
    cut = len(text) > limit
    if cut:
        text = text[:limit]
    if text.isprintable() and "\\" not in text:
        escaped = text
    else:
        escaped = "".join(
            char if char.isprintable() and char != "\\" else _escape_for_log(char)
            for char in text
        )
    return escaped + "..." if cut else escaped


def _escape_for_log(char: str) -> str:
    if char == "\\":
        return "\\\\"
    return char.encode("unicode_escape").decode("ascii")
