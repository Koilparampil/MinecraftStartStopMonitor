"""First-run interactive setup and Minecraft server auto-detection.

Pure helpers (``looks_like_server_dir``, ``detect_start_command``,
``configure_rcon``, ``default_config``...) are kept separate from the prompts so
they can be tested with temporary directories.
"""

import contextlib
import getpass
import os
import re
import secrets
import shlex
import shutil
import socket
import string
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import yaml

from src.minecraft import server_files
from src.settings import (
    DEFAULT_GAME_PORT,
    DEFAULT_RCON_PORT,
    DEFAULTS,
    EXAMPLE_WEB_PASSWORD,
    YAML_MAPPING,
    start_command_errors,
)

EULA_URL = "https://aka.ms/MinecraftEULA"
RCON_PASSWORD_LENGTH = 24
WEB_PASSWORD_LENGTH = 16

# Server jars in detection order: (kind, file-name pattern).
SERVER_JAR_RULES = (
    ("fabric", re.compile(r"^fabric-server-launch\.jar$", re.IGNORECASE)),
    ("fabric", re.compile(r"^fabric-server-mc\..+-launcher\..+\.jar$", re.IGNORECASE)),
    ("quilt", re.compile(r"^quilt-server-launch\.jar$", re.IGNORECASE)),
    ("forge", re.compile(r"^forge-.+\.jar$", re.IGNORECASE)),
    ("vanilla", re.compile(r"^minecraft_server.*\.jar$", re.IGNORECASE)),
    ("vanilla", re.compile(r"^server\.jar$", re.IGNORECASE)),
)
_NOT_SERVER_JAR = re.compile(r"installer|-sources|-javadoc", re.IGNORECASE)
_SERVER_MARKERS = (
    "server.properties",
    "eula.txt",
    "run.sh",
    "run.bat",
    "user_jvm_args.txt",
)
_SCRIPT_SUFFIXES = (".sh", ".bat", ".cmd", ".command", ".ps1")
# Endless loops in shell / PowerShell start scripts, anywhere on a line
# (``cd .. && while true; do``): while true / : / [ 1 ] / [ true ] / ((1)),
# until false, for (( ; ; )), PowerShell while ($true) and for (;;).
_LOOP = re.compile(
    r"(?<![\w$.-])(?:"
    r"while\s+(?:true|:|\[\[?\s*(?:1|true)\s*\]\]?|\(\(\s*1\s*\)\))(?=[\s;]|$)"
    r"|until\s+(?:false|\[\[?\s*0\s*\]\]?|\(\(\s*0\s*\)\))(?=[\s;]|$)"
    r"|for\s*\(\(\s*;\s*;\s*\)\)"
    r"|for\s*\(\s*;\s*;\s*\)"
    r"|while\s*\(\s*(?:\$true|1)\s*\)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_COMMENT_LINE = re.compile(r"^\s*(?:#|::|rem\b).*$", re.IGNORECASE | re.MULTILINE)
_BAT_LABEL = re.compile(r"^\s*:(\w+)", re.MULTILINE)
_BAT_GOTO = re.compile(r"(?<![\w-])goto\s*:?\s*(\w+)", re.IGNORECASE)


@dataclass(frozen=True)
class ServerJar:
    kind: str  # fabric | quilt | forge | vanilla | jar
    name: str


@dataclass(frozen=True)
class RconSetup:
    port: int
    password: str
    changed: bool
    already_enabled: bool


# ----------------------------------------------------------------------
# Detection (pure)
# ----------------------------------------------------------------------


def _files(directory):
    try:
        return sorted(e.name for e in os.scandir(directory) if e.is_file())
    except OSError:
        return []


def find_server_jar(server_dir):
    """The server jar to launch, by the rules above; a lone other jar as a fallback."""
    candidates = [
        name
        for name in _files(server_dir)
        if name.lower().endswith(".jar") and not _NOT_SERVER_JAR.search(name)
    ]
    for kind, pattern in SERVER_JAR_RULES:
        matches = sorted((n for n in candidates if pattern.match(n)), reverse=True)
        if matches:
            return ServerJar(kind, matches[0])
    if len(candidates) == 1:
        return ServerJar("jar", candidates[0])
    return None


def looks_like_server_dir(path):
    """True for a folder with server files, a launch script or a server jar."""
    if not path or not os.path.isdir(path):
        return False
    names = set(_files(path))
    if any(marker in names for marker in _SERVER_MARKERS):
        return True
    if find_server_jar(path) is not None:
        return True
    return all(os.path.isdir(os.path.join(path, d)) for d in ("mods", "libraries"))


def detect_server_dir(cwd=None):
    """The current folder (or ./server) if it looks like a Minecraft server."""
    cwd = os.path.abspath(cwd or os.getcwd())
    for candidate in (cwd, os.path.join(cwd, "server")):
        if looks_like_server_dir(candidate):
            return candidate
    return None


def java_command(jar, memory, os_name=None):
    """``java -Xms1G -Xmx<memory> -jar <jar> nogui`` quoted for the OS."""
    argv = ["java", "-Xms1G", f"-Xmx{memory}", "-jar", jar, "nogui"]
    if (os_name or os.name) == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def detect_start_command(server_dir, os_name=None, memory="4G"):
    """Launch command for *server_dir*, or None if nothing launchable was found.

    Forge/NeoForge >= 1.17 install ``run.sh``/``run.bat`` (JVM flags such as
    -Xmx live in user_jvm_args.txt); other loaders and vanilla get a plain
    ``java -jar`` command.
    """
    os_name = os_name or os.name
    script = "run.bat" if os_name == "nt" else "run.sh"
    script_path = os.path.join(server_dir, script)
    if os.path.isfile(script_path):
        if os_name == "nt":
            return "run.bat nogui"
        if os.access(script_path, os.X_OK):
            return "./run.sh nogui"
        return "sh ./run.sh nogui"
    jar = find_server_jar(server_dir)
    if jar is None:
        return None
    return java_command(jar.name, memory, os_name)


def uses_run_script(command):
    return bool(re.search(r"(^|[\s/\\])run\.(sh|bat)\b", command or ""))


def suggest_max_memory(total_bytes):
    """-Xmx suggestion: half the RAM, between 1G and 16G (modded servers
    rarely benefit from a bigger heap)."""
    if not total_bytes:
        return "4G"
    total_gib = int(total_bytes // (1 << 30))
    return f"{max(1, min(16, total_gib // 2))}G"


def detect_total_memory():
    """Total RAM in bytes, or None if it cannot be determined."""
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except (ImportError, OSError, AttributeError):
        return None


def _has_backward_goto(text):
    labels = {m.group(1).lower(): m.start() for m in _BAT_LABEL.finditer(text)}
    for match in _BAT_GOTO.finditer(text):
        target = match.group(1).lower()
        if target != "eof" and labels.get(target, len(text)) < match.start():
            return True
    return False


def restart_loop_scripts(server_dir):
    """Start scripts in *server_dir* that look like they restart the server in a
    loop (common in modpack server packs). Such a loop fights auto-stop: the
    monitor stops the server and the script starts it again."""
    found = []
    for name in _files(server_dir):
        if not name.lower().endswith(_SCRIPT_SUFFIXES):
            continue
        try:
            with open(
                os.path.join(server_dir, name), encoding="utf-8", errors="replace"
            ) as f:
                text = f.read(65536)
        except OSError:
            continue
        text = _COMMENT_LINE.sub("", text)
        if _LOOP.search(text) or _has_backward_goto(text):
            found.append(name)
    return found


def clean_path_answer(answer, os_name=None):
    """A pasted folder path without surrounding quotes (Windows "Copy as
    path") and, on POSIX, without backslash escapes (drag and drop)."""
    text = answer.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if (os_name or os.name) != "nt" and "\\" in text:
        try:
            parts = shlex.split(text)
        except ValueError:
            return text
        if len(parts) == 1:
            return parts[0]
    return text


def generate_password(length=RCON_PASSWORD_LENGTH):
    """Random alphanumeric password (safe in server.properties and YAML)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ----------------------------------------------------------------------
# server.properties / eula.txt edits
# ----------------------------------------------------------------------


def _properties_path(server_dir):
    return os.path.join(server_dir, "server.properties")


def _port_or(value, default):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def rcon_property_updates(port, password):
    return {
        "enable-rcon": "true",
        "rcon.port": str(port),
        "rcon.password": password,
        # The monitor polls `list` every few seconds; don't echo it to ops.
        "broadcast-rcon-to-ops": "false",
    }


@dataclass(frozen=True)
class RconStatus:
    enabled: bool  # enable-rcon=true with a non-empty rcon.password
    port: int
    broadcasts_to_ops: bool


def rcon_status(server_dir):
    """Current RCON configuration from server.properties."""
    props = server_files.read_properties(_properties_path(server_dir))
    enabled = str(props.get("enable-rcon", "")).strip().lower() == "true"
    return RconStatus(
        enabled=enabled and bool(props.get("rcon.password")),
        port=_port_or(props.get("rcon.port"), DEFAULT_RCON_PORT),
        # vanilla default is true
        broadcasts_to_ops=str(props.get("broadcast-rcon-to-ops", "true"))
        .strip()
        .lower()
        != "false",
    )


def disable_rcon_broadcast(server_dir):
    """broadcast-rcon-to-ops=false; returns True if the file changed."""
    return server_files.update_properties(
        _properties_path(server_dir), {"broadcast-rcon-to-ops": "false"}
    )


def configure_rcon(server_dir, port=None, password=None):
    """Enable RCON in server.properties (creating the file if needed).

    An existing enabled RCON password is kept; only broadcast-rcon-to-ops is
    forced to false then.
    """
    path = _properties_path(server_dir)
    props = server_files.read_properties(path)
    existing = props.get("rcon.password") or ""
    if str(props.get("enable-rcon", "")).strip().lower() == "true" and existing:
        updates = {"broadcast-rcon-to-ops": "false"}
        changed = server_files.update_properties(path, updates)
        return RconSetup(
            _port_or(props.get("rcon.port"), DEFAULT_RCON_PORT), existing, changed, True
        )

    game_port = _port_or(props.get("server-port"), DEFAULT_GAME_PORT)
    port = port or _port_or(props.get("rcon.port"), DEFAULT_RCON_PORT)
    if port == game_port:
        port = DEFAULT_RCON_PORT if game_port != DEFAULT_RCON_PORT else game_port + 10
    password = password or generate_password()
    changed = server_files.update_properties(
        path, rcon_property_updates(port, password)
    )
    return RconSetup(port, password, changed, False)


def accept_eula(server_dir):
    server_files.write_eula(server_dir, True)


def reserved_ports(server_dir):
    """Ports the server uses or may use after the RCON setup: game, RCON
    (configured, default, fallback) and query."""
    props = server_files.read_properties(_properties_path(server_dir))
    game = _port_or(props.get("server-port"), DEFAULT_GAME_PORT)
    return {
        game,
        _port_or(props.get("rcon.port"), DEFAULT_RCON_PORT),
        _port_or(props.get("query.port"), game),
        DEFAULT_RCON_PORT,
        game + 10,  # configure_rcon's choice when rcon.port equals the game port
    }


def port_is_free(port):
    """True if nothing listens on *port* right now (all interfaces)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def choose_dashboard_port(server_dir, is_free=None, start=None):
    """The first port from 25580 up that the server does not use and is free."""
    is_free = is_free or port_is_free
    start = start or DEFAULTS["webServerPort"]
    taken = reserved_ports(server_dir)
    for port in range(start, min(start + 200, 65536)):
        if port not in taken and is_free(port):
            return port
    return start


# ----------------------------------------------------------------------
# settings.yaml
# ----------------------------------------------------------------------


def default_config(server_dir, start_command, web_password, **overrides):
    """Nested settings dict (YAML layout) from the defaults.

    *overrides* use flat attribute names, e.g. ``webServerPort=8080``.
    """
    flat = dict(DEFAULTS)
    flat.update(
        serverDir=server_dir, startCommand=start_command, webPassword=web_password
    )
    flat.update(overrides)
    return {
        section: {nested: flat[attribute] for nested, attribute in mapping.items()}
        for section, mapping in YAML_MAPPING.items()
    }


SETTINGS_HEADER = """\
# minecraft-monitor settings (written by the first-run setup).
# Every key is documented in settings.yaml.example. null ports and RCON
# password are read from the server's server.properties at startup.
"""


class SettingsWriteError(OSError):
    """settings.yaml itself could not be written (not a server file)."""


def check_settings_writable(path):
    """Raise SettingsWriteError now if *path* cannot be written."""
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
        os.close(fd)
        os.unlink(probe)
    except OSError as e:
        raise SettingsWriteError(e.errno, e.strerror, e.filename) from e


def write_settings_file(path, config):
    """Write *config* as YAML atomically; owner-only permissions on POSIX.
    Raises SettingsWriteError."""
    try:
        _write_settings_file(path, config)
    except OSError as e:
        raise SettingsWriteError(e.errno, e.strerror, e.filename) from e


def command_line_config(values):
    """The settings file the wizard would write, from a complete command line
    (flat attribute names: serverDir, startCommand, webPassword and any other
    given flags). Raises ValueError when a value cannot work."""
    server_dir = os.path.abspath(os.path.expanduser(values["serverDir"]))
    if not os.path.isdir(server_dir):
        raise ValueError(f"--server-dir: {server_dir} is not a folder")
    errors = start_command_errors(values["startCommand"])
    if errors:
        raise ValueError(errors[0])
    password = values.get("webPassword")
    if password == EXAMPLE_WEB_PASSWORD:
        raise ValueError(
            "--web-password is the example password from settings.yaml.example; "
            "choose your own"
        )
    asked = ("serverDir", "startCommand", "webPassword")
    overrides = {k: v for k, v in values.items() if k not in asked and v is not None}
    if "webServerPort" not in overrides:
        overrides["webServerPort"] = choose_dashboard_port(server_dir)
    return default_config(server_dir, values["startCommand"], password, **overrides)


def _write_settings_file(path, config):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(SETTINGS_HEADER)
            yaml.safe_dump(
                config,
                file,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


def example_settings_path():
    """settings.yaml.example shipped with the program (bundle dir when frozen)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "settings.yaml.example")


def no_terminal_help(settings_path):
    """Lines printed when setup cannot prompt (no TTY, e.g. under systemd)."""
    example = example_settings_path()
    if getattr(sys, "frozen", False):
        target = os.path.join(
            os.path.dirname(os.path.abspath(settings_path)), "settings.yaml.example"
        )
        with contextlib.suppress(OSError):
            shutil.copyfile(example, target)
            example = target
    return [
        "error: no terminal available for interactive setup.",
        f"copy {example} -> {settings_path} and edit it (minecraft.serverDir, "
        "minecraft.startCommand and your own web.password), then restart.",
        "(or run minecraft-monitor once in a terminal to answer the setup questions)",
    ]


# ----------------------------------------------------------------------
# Interactive flow
# ----------------------------------------------------------------------


class _Prompter:
    """input()/getpass() wrappers, injectable for tests. EOFError propagates."""

    def __init__(self, ask=None, ask_secret=None, out=None):
        self._ask = ask
        self._ask_secret = ask_secret
        self.out = out or print

    def ask(self, prompt):
        return (self._ask or input)(prompt).strip()

    def secret(self, prompt):
        return (self._ask_secret or getpass.getpass)(prompt).strip()

    def confirm(self, prompt, default):
        hint = "[Y/n]" if default else "[y/N]"
        answer = self.ask(f"{prompt} {hint} ").lower()
        if not answer:
            return default
        return answer in ("y", "yes")


def _usable_server_dir(io, answer):
    path = os.path.abspath(os.path.expanduser(clean_path_answer(answer)))
    if not os.path.isdir(path):
        io.out(f"  {path} is not a folder.")
        return None
    if looks_like_server_dir(path) or io.confirm(
        "  That folder has no server.properties, launch script or server jar. Use it anyway?",
        default=False,
    ):
        return path
    return None


def _ask_server_dir(io, detected, given=None):
    if given:
        path = _usable_server_dir(io, given)
        if path:
            io.out(f"  Server folder (from --server-dir): {path}")
            return path
    answer = ""
    if detected:
        io.out(f"  Found a Minecraft server in: {detected}")
        answer = io.ask("  Press Enter to use it, or type another folder: ")
        if not answer:
            return detected
    while True:
        if answer:
            path = _usable_server_dir(io, answer)
            if path:
                return path
        answer = io.ask(
            "  Path to your Minecraft server folder (with server.properties, mods/...): "
        )


def _explain_detected_command(io, command, memory):
    io.out(f"  Launch command: {command}")
    if uses_run_script(command):
        io.out(
            "  Forge/NeoForge read JVM memory flags from user_jvm_args.txt; "
            f"for this machine something like -Xmx{memory} is a good start."
        )
    else:
        io.out(
            f"  (-Xmx{memory} is half of this machine's RAM; adjust it to your modpack.)"
        )


def _ask_start_command(io, server_dir, given=None):
    memory = suggest_max_memory(detect_total_memory())
    loops = restart_loop_scripts(server_dir)
    for name in loops:
        io.out(
            f"  Warning: {name} looks like it restarts the server in a loop. Do not use it as "
            "the launch command: the monitor stops the server and the loop would start it again."
        )
    if given:
        io.out(f"  Launch command (from --start-command): {given}")
        detected = given
    else:
        detected = detect_start_command(server_dir, memory=memory)
        if detected:
            _explain_detected_command(io, detected, memory)
    if detected:
        answer = io.ask("  Press Enter to accept, or type a different command: ")
        command = answer or detected
    else:
        io.out("  No run.sh/run.bat or server jar found in that folder.")
        command = ""
    command = _valid_command(io, command)
    if any(name in command for name in loops):
        io.out(
            "  Warning: that script restarts the server in a loop; auto-stop will not work."
        )
    return command


def _valid_command(io, command):
    while True:
        while not command:
            command = io.ask(
                "  Command that starts your server (run inside the server folder): "
            )
        errors = start_command_errors(command)
        if not errors:
            return command
        io.out(f"  {errors[0]}")
        command = ""


def _offer_rcon(io, server_dir):
    """Ask about RCON. Returns "enable", "quiet" (only broadcast-rcon-to-ops)
    or None; nothing is written yet."""
    status = rcon_status(server_dir)
    if status.enabled:
        io.out(f"  RCON is already enabled in server.properties (port {status.port}).")
        if status.broadcasts_to_ops and io.confirm(
            "  Set broadcast-rcon-to-ops=false so the monitor's status polls are not "
            "echoed to operators in chat?",
            default=True,
        ):
            return "quiet"
        return None
    io.out(
        "  RCON lets the monitor list players, kick/ban and run console commands. "
        "Without it only the status ping is used."
    )
    if io.confirm("  Enable RCON in server.properties?", default=True):
        return "enable"
    io.out(
        "  Skipped. You can set enable-rcon/rcon.password in server.properties later."
    )
    return None


def _ask_eula(io, server_dir):
    """True when the user accepts the EULA now; nothing is written yet."""
    if server_files.read_eula(server_dir) is True:
        return False
    io.out("  The Minecraft server only starts after you accept the Minecraft EULA:")
    io.out(f"    {EULA_URL}")
    answer = io.ask("  Do you accept the Minecraft EULA? Type 'yes' to accept: ")
    if answer.lower() == "yes":
        return True
    io.out("  Not accepted. The server will not start until eula.txt says eula=true.")
    return False


def _apply_server_edits(io, server_dir, rcon_action, accept):
    """Write server.properties / eula.txt once every question is answered."""
    if rcon_action == "quiet":
        disable_rcon_broadcast(server_dir)
        io.out("  Set broadcast-rcon-to-ops=false in server.properties.")
    elif rcon_action == "enable":
        setup = configure_rcon(server_dir)
        io.out(
            f"  RCON enabled on port {setup.port} with a random password "
            "(kept in server.properties)."
        )
        io.out("  If the server is running right now, restart it for RCON to start.")
    if accept:
        accept_eula(server_dir)
        io.out("  EULA accepted (eula=true written to eula.txt).")


def _ask_web_password(io, given=None):
    if given and given != EXAMPLE_WEB_PASSWORD:
        io.out("  Web dashboard password: taken from --web-password.")
        return given
    if given:
        io.out("  --web-password is the example password; choose your own.")
    while True:
        password = io.secret("  Web dashboard password (leave empty to generate one): ")
        if password == EXAMPLE_WEB_PASSWORD:
            io.out(
                "  That is the example password from settings.yaml.example; choose another."
            )
            continue
        if not password:
            password = generate_password(WEB_PASSWORD_LENGTH)
            io.out(f"  Generated web dashboard password: {password}")
            io.out("  (it is saved in settings.yaml; change it there any time)")
            return password
        if io.secret("  Repeat the password: ") == password:
            return password
        io.out("  The passwords did not match, try again.")


def interactive_setup(
    settings_path, detected_dir=None, *, given=None, ask=None, ask_secret=None, out=None
):
    """First-run wizard: prompt for required settings and save settings.yaml.

    *given* holds values from the command line (``serverDir``,
    ``startCommand``, ``webPassword``) used instead of asking. Raises
    OSError before the first question if settings.yaml cannot be written.
    Nothing is written until the last question is answered; settings.yaml
    is saved first, then server.properties / eula.txt are changed. Raises
    EOFError when there is no terminal to read answers from (the caller
    prints help and exits). Returns the saved nested config.
    """
    given = given or {}
    check_settings_writable(settings_path)
    io = _Prompter(ask, ask_secret, out)
    io.out("")
    io.out("  Welcome to Minecraft Monitor! A few questions and you are done.")
    io.out("")
    server_dir = _ask_server_dir(io, detected_dir, given.get("serverDir"))
    start_command = _ask_start_command(io, server_dir, given.get("startCommand"))
    rcon_action = _offer_rcon(io, server_dir)
    accept = _ask_eula(io, server_dir)
    web_password = _ask_web_password(io, given.get("webPassword"))

    port = choose_dashboard_port(server_dir)
    config = default_config(server_dir, start_command, web_password, webServerPort=port)
    write_settings_file(settings_path, config)
    io.out("")
    try:
        _apply_server_edits(io, server_dir, rcon_action, accept)
    except OSError as e:
        io.out(f"  Warning: could not update the server's files: {e}")
    io.out(f"  Dashboard port: {port}")
    io.out(f"  Configuration saved to {settings_path}")
    io.out("")
    return config


def print_web_link(port, host="0.0.0.0"):
    """Print localhost and LAN links to the web dashboard."""
    print(f"\n  > Web dashboard: http://localhost:{port}")
    if host in ("0.0.0.0", "", "::"):
        lan_ip = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("8.8.8.8", 80))  # no packet is sent for UDP connect
                lan_ip = probe.getsockname()[0]
        except OSError:
            lan_ip = None
        if lan_ip and lan_ip not in ("127.0.0.1", "0.0.0.0"):
            print(f"  > LAN:           http://{lan_ip}:{port}")
    print()
