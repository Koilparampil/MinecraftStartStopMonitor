"""Wire-format primitives: VarInt, String, numbers, framing, text components."""

import socket
import threading
import time

import pytest

from src.minecraft.protocol import (
    ConnectionClosed,
    PacketReader,
    ProtocolError,
    SocketReader,
    decode_long,
    decode_string,
    decode_ushort,
    decode_varint,
    encode_legacy_kick,
    encode_long,
    encode_string,
    encode_ushort,
    encode_varint,
    flatten_text,
    frame_packet,
    plain_text,
    strip_formatting,
    utf16_length,
    write_packet,
)

# Reference encodings from the protocol documentation.
VARINTS = [
    (0, "00"),
    (1, "01"),
    (2, "02"),
    (127, "7f"),
    (128, "8001"),
    (255, "ff01"),
    (25565, "ddc701"),
    (2097151, "ffff7f"),
    (2147483647, "ffffffff07"),
    (-1, "ffffffff0f"),
    (-2147483648, "8080808008"),
]


@pytest.mark.parametrize(("value", "encoded"), VARINTS)
def test_varint_reference_encodings(value, encoded):
    data = bytes.fromhex(encoded)
    assert encode_varint(value) == data
    assert decode_varint(data) == (value, len(data))


def test_varint_decode_at_offset_returns_next_offset():
    data = b"\xaa" + encode_varint(300) + b"\x05"
    assert decode_varint(data, 1) == (300, 3)


@pytest.mark.parametrize("value", [2**31, -(2**31) - 1, 2**40])
def test_varint_encode_rejects_out_of_range(value):
    with pytest.raises(ProtocolError):
        encode_varint(value)


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        ("ffffffffff01", "longer than 5"),  # 6 bytes
        ("8080808080", "longer than 5"),  # 5th byte still has the continuation bit
        ("ffffffff1f", "32 bits"),  # 35-bit value
        ("ffffffff7f", "32 bits"),
        ("80", "truncated"),
        ("", "truncated"),
    ],
)
def test_varint_decode_rejects_overlong_and_truncated(encoded, message):
    with pytest.raises(ProtocolError, match=message):
        decode_varint(bytes.fromhex(encoded))


def test_varint_accepts_non_minimal_encoding_like_vanilla():
    assert decode_varint(bytes.fromhex("8000")) == (0, 2)


@pytest.mark.parametrize(
    "text", ["", "localhost", "§aGreen Wörld", "emoji 😀 ok", "\0FML2\0"]
)
def test_string_round_trip(text):
    data = encode_string(text)
    assert decode_string(data) == (text, len(data))


def test_string_length_limits_count_utf16_units():
    assert utf16_length("😀") == 2
    encode_string("😀" * 8, 16)
    with pytest.raises(ProtocolError):
        encode_string("😀" * 9, 16)
    with pytest.raises(ProtocolError):
        decode_string(encode_string("😀" * 9), 0, 16)
    with pytest.raises(ProtocolError):
        decode_string(encode_string("a" * 17), 0, 16)


def test_string_byte_length_limit_checked_before_reading():
    # 49 bytes declared for a String(16): rejected (> 3 * 16) even if present.
    data = encode_varint(49) + b"a" * 49
    with pytest.raises(ProtocolError):
        decode_string(data, 0, 16)
    assert decode_string(data, 0, None)[0] == "a" * 49


def test_string_rejects_negative_and_truncated_lengths():
    with pytest.raises(ProtocolError, match="negative"):
        decode_string(encode_varint(-1))
    with pytest.raises(ProtocolError, match="truncated"):
        decode_string(encode_varint(10) + b"abc")


def test_string_invalid_utf8_is_replaced():
    assert decode_string(encode_varint(2) + b"\xff\xfe")[0] == "��"


def test_fixed_size_numbers():
    assert encode_ushort(25565) == b"\x63\xdd"
    assert decode_ushort(b"\x63\xdd") == (25565, 2)
    assert decode_long(encode_long(-2)) == (-2, 8)
    with pytest.raises(ProtocolError):
        encode_ushort(70000)
    with pytest.raises(ProtocolError):
        encode_long(2**63)
    with pytest.raises(ProtocolError):
        decode_long(b"\x00" * 7)


def test_packet_reader_reads_fields_in_order():
    payload = (
        encode_varint(763)
        + encode_string("host")
        + encode_ushort(25565)
        + encode_long(7)
    )
    reader = PacketReader(payload)
    assert reader.read_varint() == 763
    assert reader.read_string(255) == "host"
    assert reader.read_ushort() == 25565
    assert reader.read_long() == 7
    assert reader.remaining == 0
    with pytest.raises(ProtocolError):
        reader.read_bytes(1)


# ----------------------------------------------------------------------
# Framing over sockets
# ----------------------------------------------------------------------


@pytest.fixture
def pair():
    left, right = socket.socketpair()
    yield left, right
    left.close()
    right.close()


def test_frame_layout():
    assert frame_packet(0x01, b"\x00" * 8) == b"\x09\x01" + b"\x00" * 8


def test_read_packets_from_one_segment(pair):
    left, right = pair
    left.sendall(frame_packet(0x00, b"abc") + frame_packet(0x01, b"z"))
    reader = SocketReader(right, time.monotonic() + 2)
    assert reader.read_packet() == (0x00, b"abc")
    assert reader.read_packet() == (0x01, b"z")


def test_read_packet_delivered_byte_by_byte(pair):
    left, right = pair
    data = frame_packet(0x00, encode_string("hello"))

    def trickle():
        for byte in data:
            left.sendall(bytes([byte]))
            time.sleep(0.002)

    thread = threading.Thread(target=trickle)
    thread.start()
    packet_id, payload = SocketReader(right, time.monotonic() + 5).read_packet()
    thread.join()
    assert packet_id == 0 and PacketReader(payload).read_string() == "hello"


@pytest.mark.parametrize("length", [0, 1025])
def test_read_packet_enforces_size_limit(pair, length):
    left, right = pair
    left.sendall(encode_varint(length) + b"\x00" * 8)
    with pytest.raises(ProtocolError):
        SocketReader(right, time.monotonic() + 2).read_packet(max_size=1024)


def test_read_packet_eof_mid_frame(pair):
    left, right = pair
    left.sendall(encode_varint(10) + b"\x00\x01")
    left.shutdown(socket.SHUT_WR)
    with pytest.raises(ConnectionClosed):
        SocketReader(right, time.monotonic() + 2).read_packet()


def test_deadline_bounds_a_trickling_peer(pair):
    left, right = pair
    stop = threading.Event()

    def trickle():  # a slow-loris peer: one byte of a huge frame every 50 ms
        left.sendall(encode_varint(1000))
        while not stop.wait(0.05):
            left.sendall(b"\x00")

    thread = threading.Thread(target=trickle)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            SocketReader(right, time.monotonic() + 0.3).read_packet()
    finally:
        stop.set()
        thread.join()
    assert time.monotonic() - started < 1.0


def test_timed_out_read_consumes_nothing(pair):
    left, right = pair
    reader = SocketReader(right)
    left.sendall(b"\x05\x00ab")  # 3 of 6 frame bytes
    with pytest.raises(TimeoutError):
        reader.read_packet(deadline=time.monotonic() + 0.1)
    left.sendall(b"cd")
    assert reader.read_packet(deadline=time.monotonic() + 1) == (0, b"abcd")


def test_write_packet_sends_a_frame(pair):
    left, right = pair
    write_packet(left, 0x00, b"xyz", time.monotonic() + 1)
    assert SocketReader(right, time.monotonic() + 1).read_packet() == (0, b"xyz")


def test_legacy_kick_encoding():
    text = "§1\x00127\x00Sleeping 😀"
    data = encode_legacy_kick(text)
    assert data[0] == 0xFF
    assert int.from_bytes(data[1:3], "big") == utf16_length(
        text
    )  # length in UTF-16 units
    assert data[3:].decode("utf-16-be") == text


# ----------------------------------------------------------------------
# Text components
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("component", "expected"),
    [
        ("plain", "plain"),
        ({"text": "A"}, "A"),
        (
            {"text": "A", "extra": ["B", {"text": "C", "extra": [{"text": "D"}]}]},
            "ABCD",
        ),
        ([{"text": "A"}, "B", {"text": "C"}], "ABC"),
        (
            {"translate": "multiplayer.disconnect.not_whitelisted"},
            "multiplayer.disconnect.not_whitelisted",
        ),
        ({"translate": "x.y", "fallback": "Fallback"}, "Fallback"),
        ({"keybind": "key.jump"}, "key.jump"),
        ({"score": {"name": "a", "objective": "b", "value": "12"}}, "12"),
        ({"text": 5}, "5"),
        ({"text": True}, "true"),
        ({"color": "red"}, ""),
        (None, ""),
        ({"text": "A", "extra": "not a list"}, "A"),
    ],
)
def test_flatten_text(component, expected):
    assert flatten_text(component) == expected


def test_flatten_text_bounds_nesting_depth():
    component = {"text": "x"}
    for _ in range(500):
        component = {"text": "", "extra": [component]}
    assert flatten_text(component) == ""  # nothing below the depth limit, no crash


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("§aGreen §lBold§r", "Green Bold"),
        ("§x§f§f§0§0§0§0Hex", "Hex"),
        ("no codes", "no codes"),
        ("trailing §", "trailing "),
        ("§§a", "a"),
    ],
)
def test_strip_formatting(text, expected):
    assert strip_formatting(text) == expected


def test_plain_text_combines_both():
    assert plain_text({"text": "§7A ", "extra": [{"text": "§aB"}]}) == "A B"
