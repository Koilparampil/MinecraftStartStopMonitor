"""Dummy Minecraft Java server for tests. Stdlib only, independent of src/.

Run it with the working directory set to a server directory. It reads
./server.properties and speaks enough of the real protocols for the monitor:

- game port: server-list ping (status + ping/pong, legacy 0xFE ping) and login
  attempts, which are rejected with a Login Disconnect;
- RCON with vanilla's quirks: each packet must arrive in one read of at most
  1460 bytes (a coalesced or split packet closes the connection), auth failure
  answers request id -1, replies are split every 4096 characters and unknown
  packet types get "Unknown request <hex>";
- the same console commands on stdin. Vanilla-style lines go to
  logs/latest.log and stdout.

Environment:
  DUMMY_PLAYERS             comma list of online players
                            (default TestPlayer1,TestPlayer2,TestPlayer3; empty = nobody)
  DUMMY_ANONYMOUS           comma list of online players shown as "Anonymous Player"
                            in the status sample (the client's "Allow Server Listings" off)
  DUMMY_STARTUP_DELAY       seconds before the ports open and "Done" is logged (default 0)
  DUMMY_STOP_DELAY          seconds spent "saving" during a graceful stop (default 0.3)
  DUMMY_IGNORE_STOP=1       ignore stop commands, SIGTERM and SIGINT (a hung server)
  DUMMY_LIST_FORMAT         modern (1.13+, default) | legacy (1.7-1.12 command output)
  DUMMY_VERSION             version name in the status ping (default 1.20.1)
  DUMMY_PROTOCOL            protocol number (default: derived from DUMMY_VERSION)
  DUMMY_NO_UUIDS=1          "list uuids" is an unknown command, like old servers
  DUMMY_RCON_AUTH_PREAMBLE=1  send an empty type-0 packet before the auth reply

Commands: list [uuids], kick, ban, pardon, banlist [players|ips], say, save-all,
stop, help, plus test hooks "dummy players a,b,c" (replace the online roster),
"dummy crash" (exit 1 at once) and "dummy long <chars> [unicode]" (a long reply).
Exit code 0 after a graceful stop (stdin/RCON "stop", SIGTERM, SIGINT).
"""

import base64
import datetime
import hashlib
import json
import os
import signal
import socket
import struct
import sys
import threading
import time

PROTOCOLS = {
    "1.7.10": 5,
    "1.8.9": 47,
    "1.12.2": 340,
    "1.16.5": 754,
    "1.18.2": 758,
    "1.19.2": 760,
    "1.19.4": 762,
    "1.20.1": 763,
    "1.20.4": 765,
    "1.20.6": 766,
    "1.21.1": 767,
}
DEFAULT_PLAYERS = "TestPlayer1,TestPlayer2,TestPlayer3"
ANONYMOUS_NAME = "Anonymous Player"
NIL_UUID = "00000000-0000-0000-0000-000000000000"
RCON_READ_SIZE = 1460
RCON_SPLIT = 4096
BANNED_PLAYERS = "banned-players.json"
BANNED_IPS = "banned-ips.json"
USERCACHE = "usercache.json"
WHITELIST = "whitelist.json"


# ----------------------------------------------------------------------
# Small helpers (deliberately independent of src/minecraft)
# ----------------------------------------------------------------------


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def env_names(name, default=""):
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return [part.strip() for part in raw.split(",") if part.strip()]


def offline_uuid(name):
    """Java's UUID.nameUUIDFromBytes(("OfflinePlayer:" + name).getBytes(UTF_8))."""
    digest = bytearray(
        hashlib.md5(
            ("OfflinePlayer:" + name).encode("utf-8"), usedforsecurity=False
        ).digest()
    )
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    text = digest.hex()
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def long_text(count, unicode=False):
    """Deterministic test text of exactly `count` characters."""
    pieces = []
    total = 0
    index = 0
    while total < count:
        piece = f"é{index:06d}✓" if unicode else f"<{index:06d}>"
        pieces.append(piece)
        total += len(piece)
        index += 1
    return "".join(pieces)[:count]


def java_timestamp():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def unescape_property(text):
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        index += 1
        if char != "\\" or index >= len(text):
            out.append(char)
            continue
        char = text[index]
        index += 1
        if char == "u" and len(text) >= index + 4:
            out.append(chr(int(text[index : index + 4], 16)))
            index += 4
        else:
            out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(char, char))
    return "".join(out)


def split_property(line):
    escaped = False
    for index, char in enumerate(line):
        if not escaped and char in "=: \t":
            value = line[index + 1 :].lstrip(" \t")
            if char in " \t" and value[:1] in ("=", ":"):
                value = value[1:].lstrip(" \t")
            return line[:index], value
        escaped = char == "\\" and not escaped
    return line, ""


def load_properties(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return {}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    props = {}
    pending = ""
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.lstrip(" \t\f")
        if not pending and (not line or line[0] in "#!"):
            continue
        if (len(line) - len(line.rstrip("\\"))) % 2 == 1:
            pending += line[:-1]
            continue
        key, value = split_property(pending + line)
        pending = ""
        props[unescape_property(key)] = unescape_property(value)
    return props


def load_json_list(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return (
        [entry for entry in data if isinstance(entry, dict)]
        if isinstance(data, list)
        else []
    )


def save_json_list(path, entries):
    tmp_path = path + ".dummy-tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2)
    os.replace(tmp_path, path)


# ----------------------------------------------------------------------
# Wire formats
# ----------------------------------------------------------------------


def varint_bytes(value):
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        if value < 0x80:
            out.append(value)
            return bytes(out)
        out.append((value & 0x7F) | 0x80)
        value >>= 7


def parse_varint(data, position):
    value = 0
    for shift in range(0, 35, 7):
        if position >= len(data):
            raise ValueError("truncated VarInt")
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return (value - (1 << 32) if value & (1 << 31) else value), position
    raise ValueError("VarInt too big")


def parse_mc_string(data, position):
    length, position = parse_varint(data, position)
    if length < 0 or position + length > len(data):
        raise ValueError("bad string")
    return data[position : position + length].decode(
        "utf-8", "replace"
    ), position + length


def mc_string(text):
    data = text.encode("utf-8")
    return varint_bytes(len(data)) + data


def recv_exact(conn, size):
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def read_socket_varint(conn, first=None):
    data = bytearray()
    if first is not None:
        data.append(first)
    while not data or data[-1] & 0x80:
        if len(data) >= 5:
            raise ValueError("VarInt too big")
        data += recv_exact(conn, 1)
    return parse_varint(bytes(data), 0)[0]


def read_mc_packet(conn, limit, first=None):
    length = read_socket_varint(conn, first)
    if not 0 < length <= limit:
        raise ValueError("bad packet length")
    body = recv_exact(conn, length)
    packet_id, position = parse_varint(body, 0)
    return packet_id, body[position:]


def send_mc_packet(conn, packet_id, payload=b""):
    body = varint_bytes(packet_id) + payload
    conn.sendall(varint_bytes(len(body)) + body)


def rcon_send(conn, request_id, packet_type, text):
    payload = text.encode("utf-8", "replace")
    header = struct.pack("<iii", len(payload) + 10, request_id, packet_type)
    conn.sendall(header + payload + b"\0\0")


def rcon_send_response(conn, request_id, message):
    """Vanilla splits replies every 4096 UTF-16 code units (Java chars)."""
    units = message.encode("utf-16-le", "surrogatepass")
    step = RCON_SPLIT * 2
    chunks = [units[i : i + step] for i in range(0, len(units), step)] or [b""]
    for chunk in chunks:
        rcon_send(conn, request_id, 0, chunk.decode("utf-16-le", "surrogatepass"))


# ----------------------------------------------------------------------
# The server
# ----------------------------------------------------------------------


class ServerLog:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._file = open(path, "w", encoding="utf-8")
        self._lock = threading.Lock()

    def info(self, message, thread="Server thread", level="INFO"):
        line = f"[{time.strftime('%H:%M:%S')}] [{thread}/{level}]: {message}\n"
        with self._lock:
            try:
                self._file.write(line)
                self._file.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
            except (OSError, ValueError):
                pass

    def warning(self, message, thread="Server thread"):
        self.info(message, thread, "WARN")

    def close(self):
        with self._lock:
            try:
                self._file.close()
            except OSError:
                pass


class DummyServer:
    def __init__(self):
        self.props = load_properties("server.properties")
        self.log = ServerLog(os.path.join("logs", "latest.log"))
        self.lock = threading.RLock()
        self.version = os.environ.get("DUMMY_VERSION", "1.20.1")
        protocol = os.environ.get("DUMMY_PROTOCOL", "")
        self.protocol = int(protocol) if protocol else PROTOCOLS.get(self.version, 763)
        self.legacy = os.environ.get("DUMMY_LIST_FORMAT", "modern").lower() == "legacy"
        self.no_uuids = env_flag("DUMMY_NO_UUIDS")
        self.ignore_stop = env_flag("DUMMY_IGNORE_STOP")
        self.auth_preamble = env_flag("DUMMY_RCON_AUTH_PREAMBLE")
        self.startup_delay = env_float("DUMMY_STARTUP_DELAY", 0)
        self.stop_delay = env_float("DUMMY_STOP_DELAY", 0.3)
        self.anonymous = {name.lower() for name in env_names("DUMMY_ANONYMOUS")}
        self.players = []
        self.initial_players = env_names("DUMMY_PLAYERS", DEFAULT_PLAYERS)
        self.stop_requested = threading.Event()
        self.closing = threading.Event()
        self.stop_pending = False
        self.listeners = []
        self.rcon_clients = set()

    # --- properties ----------------------------------------------------

    def prop(self, key, default=""):
        return self.props.get(key, default)

    def prop_int(self, key, default):
        try:
            return int(self.props.get(key, default))
        except ValueError:
            return default

    def prop_bool(self, key, default=False):
        return self.props.get(key, str(default)).strip().lower() == "true"

    # --- lifecycle -----------------------------------------------------

    def run(self):
        started = time.monotonic()
        self.log.info(f"Starting minecraft server version {self.version}")
        self.log.info("Loading properties")
        self.log.info("Default game type: SURVIVAL")
        self.log.info("Generating keypair")
        self.install_signal_handlers()
        threading.Thread(
            target=self.console_loop, name="Server console handler", daemon=True
        ).start()
        if self.stop_requested.wait(self.startup_delay):
            return self.shutdown()
        host = self.prop("server-ip", "")
        port = self.prop_int("server-port", 25565)
        self.log.info(f"Starting Minecraft server on {host or '*'}:{port}")
        try:
            self.serve(
                self.listen(host, port), self.handle_game_client, "Server Connection"
            )
        except OSError as exc:
            self.log.warning("**** FAILED TO BIND TO PORT!")
            self.log.warning(f"The exception was: {exc}")
            self.log.warning("Perhaps a server is already running on that port?")
            self.log.close()
            return 1
        self.log.info('Preparing level "world"')
        self.log.info(
            f'Done ({time.monotonic() - started:.3f}s)! For help, type "help"'
        )
        self.start_rcon(host)
        with self.lock:
            self.set_players(self.initial_players)
        while not self.stop_requested.wait(0.25):
            pass
        return self.shutdown()

    def shutdown(self):
        self.log.info("Stopping server")
        self.closing.set()
        for listener in list(self.listeners):
            self.close_socket(listener)
        self.log.info("Saving players")
        self.log.info("Saving worlds")
        time.sleep(max(0.0, self.stop_delay))
        self.log.info(
            "Saving chunks for level 'ServerLevel[world]'/minecraft:overworld"
        )
        self.log.info("ThreadedAnvilChunkStorage (world): All chunks are saved")
        for conn in list(self.rcon_clients):
            self.close_socket(conn)
        self.log.close()
        return 0

    def request_stop(self, reason):
        if self.ignore_stop:
            self.log.warning(f"[dummy] DUMMY_IGNORE_STOP is set: ignoring {reason}")
            return
        self.stop_requested.set()

    def install_signal_handlers(self):
        def on_signal(signum, _frame):
            self.request_stop(f"signal {signum}")

        names = ["SIGTERM", "SIGINT", "SIGBREAK"]
        for name in names:
            if hasattr(signal, name):
                try:
                    signal.signal(getattr(signal, name), on_signal)
                except (OSError, ValueError):
                    pass

    @staticmethod
    def close_socket(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # --- networking ----------------------------------------------------

    def listen(self, host, port):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(
                (host or ("::" if family == socket.AF_INET6 else "0.0.0.0"), port)
            )
            sock.listen(50)
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.25)
        self.listeners.append(sock)
        return sock

    def serve(self, listener, handler, name):
        def accept_loop():
            while not self.closing.is_set():
                try:
                    conn, addr = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                threading.Thread(
                    target=self.guard,
                    args=(handler, conn, addr),
                    name=name,
                    daemon=True,
                ).start()

        threading.Thread(
            target=accept_loop, name=f"{name} listener", daemon=True
        ).start()

    @staticmethod
    def guard(handler, conn, addr):
        try:
            handler(conn, addr)
        except (OSError, ValueError, struct.error):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def start_rcon(self, host):
        if not self.prop_bool("enable-rcon"):
            return
        if not self.prop("rcon.password"):
            self.log.warning(
                "No rcon password set in server.properties, rcon disabled!"
            )
            return
        port = self.prop_int("rcon.port", 25575)
        self.log.info("Starting remote control listener")
        try:
            listener = self.listen(host, port)
        except OSError as exc:
            self.log.warning(
                f"Unable to initialise RCON on {host or '0.0.0.0'}:{port} : {exc}"
            )
            return
        self.serve(listener, self.handle_rcon_client, "RCON Client")
        self.log.info("Thread RCON Listener started")
        self.log.info(f"RCON running on {host or '0.0.0.0'}:{port}")

    # --- game port -----------------------------------------------------

    def handle_game_client(self, conn, addr):
        conn.settimeout(10)
        first = recv_exact(conn, 1)[0]
        if first == 0xFE:
            self.send_legacy_status(conn)
            return
        packet_id, payload = read_mc_packet(conn, 65536, first)
        if packet_id != 0:
            return
        _protocol, position = parse_varint(payload, 0)
        _host, position = parse_mc_string(payload, position)
        next_state, _ = parse_varint(payload, position + 2)
        if next_state == 1:
            self.status_exchange(conn)
        elif next_state in (2, 3):
            self.reject_login(conn, addr)

    def status_exchange(self, conn):
        packet_id, payload = read_mc_packet(conn, 1024)
        if packet_id == 0:
            send_mc_packet(conn, 0, mc_string(self.status_json()))
            packet_id, payload = read_mc_packet(conn, 1024)
        if packet_id == 1 and len(payload) == 8:
            send_mc_packet(conn, 1, payload)

    def status_json(self):
        with self.lock:
            players = list(self.players)
        sample = []
        if not self.prop_bool("hide-online-players"):
            for name in players[:12]:
                if name.lower() in self.anonymous:
                    sample.append({"name": ANONYMOUS_NAME, "id": NIL_UUID})
                else:
                    sample.append({"name": name, "id": offline_uuid(name)})
        status = {
            "version": {"name": self.version, "protocol": self.protocol},
            "players": {
                "max": self.prop_int("max-players", 20),
                "online": len(players),
                "sample": sample,
            },
            "description": {"text": self.prop("motd", "A Minecraft Server")},
            "enforcesSecureChat": True,
        }
        favicon = self.favicon()
        if favicon:
            status["favicon"] = favicon
        return json.dumps(status)

    @staticmethod
    def favicon():
        try:
            with open("server-icon.png", "rb") as handle:
                return (
                    "data:image/png;base64," + base64.b64encode(handle.read()).decode()
                )
        except OSError:
            return None

    def send_legacy_status(self, conn):
        with self.lock:
            online = len(self.players)
        fields = ["§1", "127", self.version, self.prop("motd", "A Minecraft Server")]
        fields += [str(online), str(self.prop_int("max-players", 20))]
        data = "\0".join(fields).encode("utf-16-be")
        conn.sendall(b"\xff" + struct.pack(">H", len(data) // 2) + data)

    def reject_login(self, conn, addr):
        _packet_id, payload = read_mc_packet(conn, 4096)
        name, _ = parse_mc_string(payload, 0)
        message = self.login_rejection(name)
        self.log.info(f"Disconnecting {name} (/{addr[0]}:{addr[1]}): {message}")
        send_mc_packet(conn, 0, mc_string(json.dumps({"text": message})))

    def login_rejection(self, name):
        for entry in load_json_list(BANNED_PLAYERS):
            if str(entry.get("name", "")).lower() == name.lower():
                return f"You are banned from this server.\nReason: {entry.get('reason', '')}"
        if self.prop_bool("white-list"):
            allowed = {
                str(e.get("name", "")).lower() for e in load_json_list(WHITELIST)
            }
            if name.lower() not in allowed:
                return "You are not white-listed on this server!"
        return "This is a dummy test server; joining is not supported."

    # --- RCON ----------------------------------------------------------

    def handle_rcon_client(self, conn, addr):
        conn.settimeout(None)
        thread = f"RCON Client /{addr[0]}"
        self.rcon_clients.add(conn)
        self.log.info(f"Thread {thread} started", thread="RCON Listener #1")
        authed = False
        try:
            while True:
                data = conn.recv(RCON_READ_SIZE)
                if len(data) < 10:
                    return
                (length,) = struct.unpack_from("<i", data, 0)
                if length != len(data) - 4:
                    self.log.warning(
                        f"[dummy] RCON read {len(data)} bytes for a {length}-byte packet: "
                        "vanilla closes the connection (packets must arrive one per read)",
                        thread=thread,
                    )
                    return
                request_id, packet_type = struct.unpack_from("<ii", data, 4)
                body = data[12:].split(b"\0", 1)[0].decode("utf-8", "replace")
                authed = self.rcon_dispatch(conn, request_id, packet_type, body, authed)
        finally:
            self.rcon_clients.discard(conn)
            self.log.info(f"Thread {thread} shutting down", thread=thread)

    def rcon_dispatch(self, conn, request_id, packet_type, body, authed):
        if packet_type == 3:
            password = self.prop("rcon.password")
            if password and body == password:
                if self.auth_preamble:
                    rcon_send(conn, request_id, 0, "")
                rcon_send(conn, request_id, 2, "")
                return True
            rcon_send(conn, -1, 2, "")
            return False
        if packet_type == 2:
            if not authed:
                rcon_send(conn, -1, 2, "")
                return False
            try:
                output = self.execute(body, source="Rcon")
            except Exception as exc:  # noqa: BLE001 - mirror vanilla's error reply
                output = f"Error executing: {body} ({exc})"
            rcon_send_response(conn, request_id, output)
            self.after_command()
            return True
        rcon_send_response(
            conn, request_id, f"Unknown request {packet_type & 0xFFFFFFFF:x}"
        )
        return authed

    # --- console -------------------------------------------------------

    def console_loop(self):
        try:
            for line in sys.stdin:
                output = self.execute(line, source="Server")
                for out_line in output.split("\n") if output else []:
                    self.log.info(out_line)
                self.after_command()
        except (OSError, ValueError):
            pass

    def after_command(self):
        with self.lock:
            pending, self.stop_pending = self.stop_pending, False
        if pending:
            self.request_stop("the stop command")

    # --- commands ------------------------------------------------------

    def execute(self, line, source):
        command = line.strip()
        command = command.removeprefix("/")
        if not command:
            return ""
        name, _, args = command.partition(" ")
        handler = COMMANDS.get(name.lower())
        if handler is None:
            return self.unknown_command(command)
        with self.lock:
            return handler(self, args.strip(), source)

    def unknown_command(self, command):
        if self.legacy:
            return "Unknown command. Try /help for a list of commands"
        return f"Unknown or incomplete command, see below for error\n{command}<--[HERE]"

    def feedback(self, source, text):
        if source == "Rcon":
            self.log.info(f"[Rcon: {text}]")
        return text

    def find_online(self, name):
        for player in self.players:
            if player.lower() == name.lower():
                return player
        return None

    def known_profile(self, name):
        """(canonical name, uuid) from the online list or usercache, else offline."""
        online = self.find_online(name)
        if online:
            return online, offline_uuid(online)
        for entry in load_json_list(USERCACHE):
            if str(entry.get("name", "")).lower() == name.lower() and entry.get("uuid"):
                return entry["name"], entry["uuid"]
        return name, offline_uuid(name)

    def set_players(self, names):
        old = {name.lower(): name for name in self.players}
        new = {name.lower(): name for name in names}
        for key, name in old.items():
            if key not in new:
                self.log.info(f"{name} left the game")
        for key, name in new.items():
            if key not in old:
                self.log.info(f"{name} joined the game")
        self.players = list(new.values())
        self.remember_profiles(self.players)

    def remove_player(self, name, reason):
        self.log.info(f"{name} lost connection: {reason}")
        self.set_players([p for p in self.players if p != name])

    @staticmethod
    def remember_profiles(names):
        cache = load_json_list(USERCACHE)
        known = {str(entry.get("name", "")).lower() for entry in cache}
        expires = (
            datetime.datetime.now().astimezone() + datetime.timedelta(days=30)
        ).strftime("%Y-%m-%d %H:%M:%S %z")
        added = [
            {"name": name, "uuid": offline_uuid(name), "expiresOn": expires}
            for name in names
            if name.lower() not in known
        ]
        if added:
            save_json_list(USERCACHE, cache + added)

    def cmd_list(self, args, _source):
        uuids = args.lower() == "uuids"
        if uuids and self.no_uuids:
            return self.unknown_command(f"list {args}")
        names = [f"{p} ({offline_uuid(p)})" if uuids else p for p in self.players]
        count, maximum = len(self.players), self.prop_int("max-players", 20)
        if self.legacy:
            return f"There are {count}/{maximum} players online:\n" + ", ".join(names)
        return f"There are {count} of a max of {maximum} players online: " + ", ".join(
            names
        )

    def cmd_kick(self, args, source):
        name, _, reason = args.partition(" ")
        target = self.find_online(name) if name else None
        if target is None:
            return (
                "That player cannot be found" if self.legacy else "No player was found"
            )
        reason = reason.strip() or "Kicked by an operator"
        self.remove_player(target, reason)
        if self.legacy:
            return self.feedback(source, f"Kicked {target} from the game")
        return self.feedback(source, f"Kicked {target}: {reason}")

    def cmd_ban(self, args, source):
        name, _, reason = args.partition(" ")
        if not name:
            return "Usage: /ban <name> [reason ...]"
        reason = reason.strip() or "Banned by an operator."
        bans = load_json_list(BANNED_PLAYERS)
        if any(str(e.get("name", "")).lower() == name.lower() for e in bans):
            if self.legacy:
                return f"Could not ban player {name}"
            return "Nothing changed. The player is already banned"
        canonical, player_uuid = self.known_profile(name)
        bans.append(
            {
                "uuid": player_uuid,
                "name": canonical,
                "created": java_timestamp(),
                "source": source,
                "expires": "forever",
                "reason": reason,
            }
        )
        save_json_list(BANNED_PLAYERS, bans)
        if self.find_online(canonical):
            self.remove_player(canonical, "You are banned from this server.")
        if self.legacy:
            return self.feedback(source, f"Banned player {canonical}")
        return self.feedback(source, f"Banned {canonical}: {reason}")

    def cmd_pardon(self, args, source):
        name = args.split(" ")[0]
        bans = load_json_list(BANNED_PLAYERS)
        kept = [e for e in bans if str(e.get("name", "")).lower() != name.lower()]
        if not name or len(kept) == len(bans):
            if self.legacy:
                return f"Could not unban player {name}"
            return "Nothing changed. The player isn't banned"
        canonical = next(e.get("name") for e in bans if e not in kept)
        save_json_list(BANNED_PLAYERS, kept)
        if self.legacy:
            return self.feedback(source, f"Unbanned player {canonical}")
        return self.feedback(source, f"Unbanned {canonical}")

    def cmd_banlist(self, args, _source):
        ips = args.lower() == "ips"
        bans = load_json_list(BANNED_IPS if ips else BANNED_PLAYERS)
        labels = [str(e.get("ip" if ips else "name", "?")) for e in bans]
        if self.legacy:
            kind = "IP addresses" if ips else "players"
            return f"There are {len(bans)} total banned {kind}:\n" + ", ".join(labels)
        if not bans:
            return "There are no bans"
        lines = [f"There are {len(bans)} ban(s):"]
        lines += [
            f"{label} was banned by {e.get('source', 'Server')}: {e.get('reason', '')}"
            for label, e in zip(labels, bans, strict=True)
        ]
        return "\n".join(lines)

    def cmd_say(self, args, source):
        self.log.info(f"[{source}] {args}")
        return ""

    def cmd_save_all(self, _args, source):
        self.feedback(source, "Saving the game (this may take a moment!)")
        self.feedback(source, "Saved the game")
        return "Saving the game (this may take a moment!)\nSaved the game"

    def cmd_stop(self, _args, source):
        self.stop_pending = True
        return self.feedback(source, "Stopping the server")

    def cmd_help(self, _args, _source):
        return "\n".join(f"/{name}" for name in sorted(COMMANDS))

    def cmd_dummy(self, args, _source):
        sub, _, rest = args.partition(" ")
        if sub == "players":
            names = [n.strip() for n in rest.split(",") if n.strip()]
            self.set_players(names)
            return "Dummy roster: " + (", ".join(self.players) or "(nobody)")
        if sub == "crash":
            self.log.info("[dummy] crashing on request", level="ERROR")
            os._exit(1)
        if sub == "long":
            count, *flags = rest.split() or ["0"]
            return long_text(int(count), unicode="unicode" in flags)
        return "Usage: dummy players a,b,c | dummy crash | dummy long <chars> [unicode]"


COMMANDS = {
    "list": DummyServer.cmd_list,
    "kick": DummyServer.cmd_kick,
    "ban": DummyServer.cmd_ban,
    "pardon": DummyServer.cmd_pardon,
    "banlist": DummyServer.cmd_banlist,
    "say": DummyServer.cmd_say,
    "save-all": DummyServer.cmd_save_all,
    "stop": DummyServer.cmd_stop,
    "help": DummyServer.cmd_help,
    "dummy": DummyServer.cmd_dummy,
}


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    code = DummyServer().run()
    sys.stdout.flush()
    # os._exit: a daemon thread may still be blocked reading stdin, which can
    # make normal interpreter shutdown abort; skip it after flushing.
    os._exit(code)


if __name__ == "__main__":
    main()
