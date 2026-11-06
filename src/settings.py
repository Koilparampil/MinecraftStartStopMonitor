# Copyright (c) 2024 Nomomo
# Copyright (c) 2026 Kevin Perez - Modified work
# Copyright (c) 2026 Koilparampil - Modified work (Minecraft port)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

"""Application settings.

CONTRACT FILE: the attribute names and YAML mapping below are shared by every
module. Owner of further logic (validation details, server.properties
resolution): the app-shell agent. Do not rename attributes without the lead.

Modules read settings through the module-level singleton
(``from src.settings import settings``) at call time, never by copying values
at import time, so tests can monkeypatch attributes on the singleton.
"""

import difflib
import ipaddress
import logging
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SECURITY_SETTINGS = {
    "sessionSecretKey": None,
    "sessionTimeout": 3600,
    "maxLoginAttempts": 5,
    "lockoutDuration": 300,
    "rateLimitEnabled": True,
    "rateLimitRequests": 300,
    "rateLimitWindow": 60,
}

DEFAULT_SLEEPING_MOTD = "§7Server is sleeping §8- §aJoin to wake it up!"
DEFAULT_STARTING_MESSAGE = (
    "§eThe server is starting up!\n§7Modded servers can take a few minutes. "
    "Please reconnect shortly."
)

# Defaults for every flat setting attribute.
DEFAULTS = {
    # --- minecraft server ---------------------------------------------
    "serverDir": None,  # server root: server.properties, mods/, world/
    "startCommand": None,  # e.g. "./run.sh nogui" or "java -Xmx6G -jar fabric-server-launch.jar nogui"
    "serverHost": "127.0.0.1",  # where the monitor reaches the server (RCON/ping)
    "serverPort": None,  # None -> server.properties server-port -> 25565
    "rconPort": None,  # None -> server.properties rcon.port -> 25575
    "rconPassword": None,  # None -> server.properties rcon.password (when enable-rcon=true)
    "pollingRate": 5,  # seconds between status/roster polls
    "stopTimeout": 120,  # seconds to wait for a graceful save+stop before force-killing
    "processName": "java",  # discovery: process name/cmdline must contain this
    # --- web ----------------------------------------------------------
    "useWebServer": True,
    "webBindHost": "0.0.0.0",
    "webServerPort": 25580,
    "webUsername": "admin",
    "webPassword": None,
    "controlServerThroughWeb": True,
    "showServerIPAddress": False,
    "webConsole": True,  # log tail + RCON console in the dashboard
    "playerAvatars": True,  # player heads from an external avatar service
    "trustedProxies": [],  # reverse proxies whose X-Forwarded-For is trusted (IPs/CIDRs)
    "secureCookies": False,  # session cookies only over HTTPS
    # --- features -----------------------------------------------------
    "enablePlayerTracking": True,
    "autoStart": True,
    "autoStop": True,
    # --- auto-start (sleeping server) ---------------------------------
    "listenerBindHost": None,  # None -> server.properties server-ip -> all interfaces
    "sleepingMotd": DEFAULT_SLEEPING_MOTD,
    "startingMessage": DEFAULT_STARTING_MESSAGE,
    "sleepingVersionName": "Sleeping",
    "respectWhitelist": True,  # only wake for players allowed by whitelist/ban lists
    # --- auto-stop ----------------------------------------------------
    "autoStopDelay": 600,  # seconds with zero players before stopping
    # --- monitor state ------------------------------------------------
    "dataDir": "data",  # players.json, server.pid, server-console.log (relative to the settings file)
    # --- security -----------------------------------------------------
    **DEFAULT_SECURITY_SETTINGS,
}

# Nested YAML key -> flat attribute.
YAML_MAPPING = {
    "minecraft": {
        "serverDir": "serverDir",
        "startCommand": "startCommand",
        "host": "serverHost",
        "port": "serverPort",
        "rconPort": "rconPort",
        "rconPassword": "rconPassword",
        "pollingRate": "pollingRate",
        "stopTimeout": "stopTimeout",
        "processName": "processName",
    },
    "web": {
        "enabled": "useWebServer",
        "host": "webBindHost",
        "port": "webServerPort",
        "username": "webUsername",
        "password": "webPassword",
        "controlServer": "controlServerThroughWeb",
        "showServerIP": "showServerIPAddress",
        "console": "webConsole",
        "playerAvatars": "playerAvatars",
        "trustedProxies": "trustedProxies",
        "secureCookies": "secureCookies",
    },
    "features": {
        "playerTracking": "enablePlayerTracking",
        "autoStart": "autoStart",
        "autoStop": "autoStop",
    },
    "autoStart": {
        "bindHost": "listenerBindHost",
        "motd": "sleepingMotd",
        "kickMessage": "startingMessage",
        "versionName": "sleepingVersionName",
        "respectWhitelist": "respectWhitelist",
    },
    "autoStop": {
        "stopDelay": "autoStopDelay",
    },
    "security": {key: key for key in DEFAULT_SECURITY_SETTINGS},
    "monitor": {
        "dataDir": "dataDir",
    },
}

SENSITIVE_SETTINGS = {"webPassword", "rconPassword", "sessionSecretKey"}
EXAMPLE_WEB_PASSWORD = "changeme"  # the placeholder in settings.yaml.example
SESSION_KEY_FILE = "session_secret.key"

DEFAULT_GAME_PORT = 25565
DEFAULT_RCON_PORT = 25575

# Typed settings: YAML strings such as "25565" or "false" are coerced on load,
# anything still of the wrong type is reported by validate_settings().
BOOLEAN_SETTINGS = (
    "useWebServer",
    "controlServerThroughWeb",
    "showServerIPAddress",
    "webConsole",
    "playerAvatars",
    "enablePlayerTracking",
    "autoStart",
    "autoStop",
    "respectWhitelist",
    "rateLimitEnabled",
    "secureCookies",
)
PORT_SETTINGS = ("serverPort", "rconPort", "webServerPort")
POSITIVE_NUMBER_SETTINGS = ("pollingRate", "stopTimeout", "autoStopDelay")
POSITIVE_INT_SETTINGS = (
    "sessionTimeout",
    "maxLoginAttempts",
    "lockoutDuration",
    "rateLimitRequests",
    "rateLimitWindow",
)
_TRUE_WORDS = {"true", "yes", "on", "1"}
_FALSE_WORDS = {"false", "no", "off", "0"}


def yaml_name(flat_key):
    """Nested YAML name (``section.key``) of a flat attribute, for messages."""
    for section, mapping in YAML_MAPPING.items():
        for nested_key, flat in mapping.items():
            if flat == flat_key:
                return f"{section}.{nested_key}"
    return flat_key


def _suggestion(name, candidates):
    by_case = {candidate.lower(): candidate for candidate in candidates}
    if name.lower() in by_case:
        return by_case[name.lower()]
    matches = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _unknown_key_warning(path, candidates):
    hint = _suggestion(path.rpartition(".")[2], candidates)
    text = f"Unknown setting '{path}' in the settings file was ignored"
    return f"{text}; did you mean '{hint}'?" if hint else f"{text}."


def unknown_setting_warnings(data):
    """Warnings for YAML keys that no setting uses (a typo would otherwise
    silently leave the default in place), with a "did you mean" hint."""
    nested = {
        key: f"{section}.{key}"
        for section, mapping in YAML_MAPPING.items()
        for key in mapping
    }
    warnings = []
    for key, value in data.items():
        if key in YAML_MAPPING:
            if isinstance(value, dict):
                known = list(YAML_MAPPING[key])
                warnings += [
                    _unknown_key_warning(f"{key}.{name}", known)
                    for name in value
                    if name not in YAML_MAPPING[key]
                ]
            elif value is not None:
                warnings.append(
                    f"Settings section '{key}' must be a mapping; it was ignored."
                )
        elif key not in DEFAULTS:
            moved = {name.lower(): path for name, path in nested.items()}.get(
                str(key).lower()
            )
            candidates = [*YAML_MAPPING, *DEFAULTS]
            warnings.append(
                f"Unknown setting '{key}' in the settings file was ignored; did you mean "
                f"'{moved}' (indented under its section)?"
                if moved
                else _unknown_key_warning(str(key), candidates)
            )
    return warnings


def mask_setting(key, value):
    """Printable form of a setting value; sensitive values are masked."""
    if key in SENSITIVE_SETTINGS:
        return "***" if value else repr(value)
    return repr(value)


class Settings:
    """Application settings held as attributes; item access mirrors attribute access."""

    def __init__(self):
        vars(self).update(
            {k: list(v) if isinstance(v, list) else v for k, v in DEFAULTS.items()}
        )

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _ensure_session_secret_key(self, settings_file_path, configured=None):
        """A non-empty ``security.sessionSecretKey`` from the YAML wins (so it
        can be rotated); otherwise the key in session_secret.key next to the
        settings file is used, created (owner-only, 0600) on the first run."""
        if configured is not None and str(configured).strip():
            self.sessionSecretKey = str(configured)
            return
        key_file = os.path.join(
            os.path.dirname(os.path.abspath(settings_file_path)), SESSION_KEY_FILE
        )
        saved = _read_secret_file(key_file)
        if saved:
            self.sessionSecretKey = saved
            return
        if self.sessionSecretKey:
            return
        self.sessionSecretKey = secrets.token_hex(32)
        try:
            _write_private_file(key_file, self.sessionSecretKey)
            logger.info(f"Generated a session secret key in {key_file}")
        except OSError as e:
            logger.warning(
                f"Could not save {key_file} ({e}): dashboard logins end when the "
                "monitor restarts"
            )

    @staticmethod
    def _load_nested_settings(data):
        """Map the nested YAML structure onto flat attribute names."""
        flat_settings = {}
        for domain, domain_mapping in YAML_MAPPING.items():
            section = data.get(domain)
            if isinstance(section, dict):
                for nested_key, flat_key in domain_mapping.items():
                    if nested_key in section:
                        flat_settings[flat_key] = section[nested_key]

        # Flat top-level keys are accepted for known attributes only (an
        # arbitrary key could shadow a method such as ``get``).
        for key, value in data.items():
            if key not in YAML_MAPPING and key in DEFAULTS:
                flat_settings[key] = value
        return flat_settings

    def readSettings(self, file_path):
        configured_secret = None
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                file_data = yaml.safe_load(file) or {}
            if not isinstance(file_data, dict):
                raise yaml.YAMLError("the top level must be a mapping")
            for warning in unknown_setting_warnings(file_data):
                logger.warning(warning)
            loaded = self._load_nested_settings(file_data)
            for key, value in loaded.items():
                self[key] = value
            configured_secret = loaded.get("sessionSecretKey")
            self.coerce_types()
            print("Settings loaded successfully.")
        except FileNotFoundError:
            logger.warning(f"Settings file not found: {file_path}")
        except yaml.YAMLError as e:
            logger.warning(f"Error: Invalid YAML format in {file_path}: {e}")
        except Exception:
            logger.exception("Error from readSettings")

        self._ensure_session_secret_key(file_path, configured_secret)
        self.resolve_paths(os.path.dirname(os.path.abspath(file_path)))

    def resolve_paths(self, base_dir):
        """Relative ``serverDir`` / ``dataDir`` from the settings file are
        relative to the folder holding that file (not to the working
        directory), so the monitor finds the same data wherever it is
        started from. Absolute paths and ``~`` are kept as given (expanded).
        """
        for key in ("serverDir", "dataDir"):
            value = self.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            path = os.path.expanduser(value.strip())
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(base_dir, path))
            self[key] = path

    def coerce_types(self):
        """Turn YAML strings like ``"25565"`` / ``"false"`` into int/bool.

        Values that cannot be coerced are left alone; validate_settings()
        reports them.
        """
        for key in BOOLEAN_SETTINGS:
            self[key] = _coerce_bool(self.get(key))
        for key in PORT_SETTINGS + POSITIVE_INT_SETTINGS:
            self[key] = _coerce_number(self.get(key), integer=True)
        for key in POSITIVE_NUMBER_SETTINGS:
            self[key] = _coerce_number(self.get(key), integer=False)
        self.trustedProxies = _coerce_list(self.get("trustedProxies"))

    def data_path(self, *parts):
        """Path inside the monitor's data directory (created on demand)."""
        base = self.dataDir or "data"
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, *parts)

    # ------------------------------------------------------------------
    # server.properties resolution
    # ------------------------------------------------------------------

    def server_properties_path(self):
        if not self.serverDir:
            return None
        return os.path.join(self.serverDir, "server.properties")

    def read_server_properties(self):
        """server.properties as a dict ({} if missing or unreadable)."""
        from src.minecraft.server_files import read_properties

        path = self.server_properties_path()
        if not path or not os.path.isfile(path):
            return {}
        try:
            return read_properties(path)
        except Exception:
            logger.exception(f"Could not read {path}")
            return {}

    def resolve_server_properties(self):
        """Fill unset ports/password/bind host from the server's server.properties.

        Explicit settings (YAML or CLI) always win; an empty string counts as
        unset for the ports and the RCON password (an empty RCON password
        cannot work anyway). Also expands ``serverDir`` to an absolute path.
        Called by main after CLI overrides and before validation. Safe to call
        when the file is missing. Returns the parsed properties.
        """
        if isinstance(self.serverDir, str) and self.serverDir.strip():
            self.serverDir = os.path.abspath(os.path.expanduser(self.serverDir.strip()))
        props = self.read_server_properties()
        explicit_password = _blank_to_none(self.rconPassword)
        explicit_port = _blank_to_none(self.serverPort)
        explicit_rcon_port = _blank_to_none(self.rconPort)

        self.serverPort = _resolved_port(
            self.serverPort, props.get("server-port"), DEFAULT_GAME_PORT
        )
        self.rconPort = _resolved_port(
            self.rconPort, props.get("rcon.port"), DEFAULT_RCON_PORT
        )
        self.rconPassword = explicit_password
        if self.rconPassword is None and _truthy(props.get("enable-rcon")):
            self.rconPassword = props.get("rcon.password") or None
        if self.listenerBindHost is None:
            self.listenerBindHost = props.get("server-ip", "") or ""

        self._warn_about_properties(props, explicit_password, explicit_port)
        self._warn_about_rcon(props, explicit_password, explicit_rcon_port)
        return props

    def _warn_about_rcon(self, props, explicit_password, explicit_rcon_port):
        """Explicit RCON settings that contradict server.properties (values of
        passwords are never logged)."""
        file_port = _int_or(props.get("rcon.port"), None)
        if explicit_rcon_port is not None and file_port not in (None, self.rconPort):
            logger.warning(
                f"minecraft.rconPort is {self.rconPort} but server.properties has "
                f"rcon.port={file_port}: RCON will not connect. Make them match "
                "(or set minecraft.rconPort to null)."
            )
        file_password = props.get("rcon.password") or None
        if (
            explicit_password is not None
            and file_password is not None
            and _truthy(props.get("enable-rcon"))
            and explicit_password != file_password
        ):
            logger.warning(
                "minecraft.rconPassword differs from rcon.password in server.properties:"
                " RCON logins will fail. Set minecraft.rconPassword to null to use the"
                " server's own password."
            )

    def _warn_about_properties(self, props, explicit_password, explicit_port):
        if not props:
            return
        file_port = _int_or(props.get("server-port"), None)
        if (
            explicit_port is not None
            and file_port is not None
            and file_port != self.serverPort
        ):
            logger.warning(
                f"minecraft.port is {self.serverPort} but server.properties has "
                f"server-port={file_port}: the monitor sleeps on and pings port "
                f"{self.serverPort} while the server listens on {file_port}. "
                "Make them match (or remove minecraft.port)."
            )
        if explicit_password is not None and not _truthy(props.get("enable-rcon")):
            logger.warning(
                "minecraft.rconPassword is set but server.properties does not have "
                "enable-rcon=true: RCON will not answer until it is enabled there."
            )
        server_ip = (props.get("server-ip") or "").strip()
        if server_ip and _is_loopback(self.serverHost) and not _is_loopback(server_ip):
            logger.warning(
                f"server.properties binds server-ip={server_ip}, but minecraft.host is "
                f"{self.serverHost}: RCON and the status ping may not reach the server. "
                f"Set minecraft.host to {server_ip}."
            )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_settings(self):
        """Validate mandatory settings.

        Returns:
            list[str]: non-fatal warnings (already logged).

        Raises:
            ValueError: listing every problem found.
        """
        errors = []
        errors += self._required_errors()
        errors += start_command_errors(self.get("startCommand"))
        errors += self._port_errors()
        errors += self._number_errors()
        errors += self._type_errors()
        errors += self._web_login_errors()
        errors += self._proxy_errors()

        if errors:
            raise ValueError(
                "Settings validation failed:\n"
                + "\n".join(f"  - {error}" for error in errors)
            )

        warnings = self.validation_warnings()
        for warning in warnings:
            logger.warning(warning)
        return warnings

    def _required_errors(self):
        errors = []
        if not self.get("serverDir"):
            errors.append("minecraft.serverDir is required")
        elif not os.path.isdir(self.serverDir):
            errors.append(f"minecraft.serverDir does not exist: {self.serverDir}")

        process_name = self.get("processName")
        if not isinstance(process_name, str) or not process_name.strip():
            errors.append("minecraft.processName must be a non-empty string")
        host = self.get("serverHost")
        if not isinstance(host, str) or not host.strip():
            errors.append("minecraft.host must be a non-empty host name or address")
        bind_host = self.get("listenerBindHost")
        if bind_host is not None and not isinstance(bind_host, str):
            errors.append("autoStart.bindHost must be a host name, an address or empty")
        return errors

    def _port_errors(self):
        errors = []
        ports = {}
        for key in PORT_SETTINGS:
            value = self.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"{yaml_name(key)} is not a valid integer: {value!r}")
            elif not 1 <= value <= 65535:
                errors.append(
                    f"{yaml_name(key)} must be between 1 and 65535 (got {value})"
                )
            else:
                ports[key] = value

        game = ports.get("serverPort")
        for other in ("rconPort", "webServerPort"):
            if game is not None and ports.get(other) == game:
                errors.append(
                    f"{yaml_name(other)} must differ from the game port ({game})"
                )
        rcon, web = ports.get("rconPort"), ports.get("webServerPort")
        if rcon is not None and rcon == web and rcon != game:
            errors.append(f"web.port must differ from the RCON port ({rcon})")
        return errors

    def _number_errors(self):
        errors = []
        for key in POSITIVE_NUMBER_SETTINGS + POSITIVE_INT_SETTINGS:
            value = self.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{yaml_name(key)} is not a number: {value!r}")
            elif not math.isfinite(value):
                errors.append(f"{yaml_name(key)} must be a finite number (got {value})")
            elif value <= 0:
                errors.append(f"{yaml_name(key)} must be positive (got {value})")
        return errors

    def _type_errors(self):
        errors = []
        for key in BOOLEAN_SETTINGS:
            if not isinstance(self.get(key), bool):
                errors.append(
                    f"{yaml_name(key)} must be true or false (got {self.get(key)!r})"
                )
        for key in (
            "sleepingMotd",
            "startingMessage",
            "sleepingVersionName",
        ):
            if not isinstance(self.get(key), str):
                errors.append(f"{yaml_name(key)} must be a string")
        for key in ("rconPassword", "webPassword", "webUsername"):
            value = self.get(key)
            if value is not None and not isinstance(value, str):
                errors.append(
                    f"{yaml_name(key)} must be text, but YAML read it as "
                    f"{_yaml_kind(value)}: put it in quotes in the settings file "
                    '(e.g. password: "0123")'
                )
        return errors

    def _web_login_errors(self):
        if not self.get("useWebServer", True):
            return []
        errors = []
        username, password = self.get("webUsername"), self.get("webPassword")
        if isinstance(username, str) and not username.strip():
            errors.append("web.username must not be empty")
        if password is None or password == "":
            errors.append("web.password is required when the web server is enabled")
        elif password == EXAMPLE_WEB_PASSWORD:
            errors.append(
                "web.password is still the example password from "
                "settings.yaml.example: choose your own"
            )
        return errors

    def _proxy_errors(self):
        proxies = self.get("trustedProxies")
        if not isinstance(proxies, list):
            return ["web.trustedProxies must be a list of IP addresses or networks"]
        errors = []
        for entry in proxies:
            try:
                ipaddress.ip_network(str(entry).strip(), strict=False)
            except ValueError:
                errors.append(
                    f"web.trustedProxies: {entry!r} is not an IP address or network "
                    "(e.g. 127.0.0.1 or 10.0.0.0/8)"
                )
        return errors

    def validation_warnings(self):
        """Non-fatal configuration problems worth telling the admin about."""
        warnings = []
        if not self.get("rconPassword"):
            warnings.append(
                "RCON is not configured (no minecraft.rconPassword and no enable-rcon=true "
                "in server.properties): the monitor falls back to the status ping. "
                "Server state and auto-stop still work, but kick and console commands "
                "are unavailable, bans can only be changed while the server is stopped, "
                "and a server the monitor did not launch is stopped with a termination "
                "signal instead of `stop`."
            )
        warnings += self._server_dir_warnings()
        warnings += start_command_warnings(
            self.get("startCommand"), self.get("serverDir")
        )
        warnings += restart_loop_warnings(
            self.get("startCommand"), self.get("serverDir")
        )
        return warnings

    def _server_dir_warnings(self):
        server_dir = self.get("serverDir")
        if not server_dir or not os.path.isdir(server_dir):
            return []
        warnings = []
        if not os.path.isfile(os.path.join(server_dir, "server.properties")):
            warnings.append(
                f"No server.properties in {server_dir} yet: ports and RCON use "
                "defaults until the server has been started once."
            )
        try:
            from src.minecraft.server_files import read_eula

            eula = read_eula(server_dir)
        except Exception:
            logger.debug("Could not read eula.txt", exc_info=True)
            eula = None
        if eula is not True:
            warnings.append(
                f"The Minecraft EULA is not accepted in {os.path.join(server_dir, 'eula.txt')}:"
                " the server will exit right after starting. Read https://aka.ms/MinecraftEULA"
                " and set eula=true."
            )
        return warnings

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def get_git_hash(self):
        """Return the current git commit hash (cached), or None."""
        if not hasattr(self, "_git_hash"):
            self._git_hash = self._read_git_hash()
        return self._git_hash

    @staticmethod
    def _read_git_hash():
        if not shutil.which("git"):
            return None
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=os.path.dirname(__file__),
                check=True,
                timeout=5,
            )
            return result.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None


# ----------------------------------------------------------------------
# startCommand helpers
# ----------------------------------------------------------------------


def start_command_argv(command, os_name=None):
    """The startCommand as an argv list (POSIX strings are split with shlex).

    On Windows a string is returned as a one-element list: it is handed to
    CreateProcess unsplit. Raises ValueError for unusable values.
    """
    os_name = os_name or os.name
    if isinstance(command, (list, tuple)):
        if not command or not all(isinstance(arg, str) and arg for arg in command):
            raise ValueError("a list startCommand must contain non-empty strings only")
        return list(command)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("startCommand must be a non-empty string or list")
    if os_name == "nt":
        return [command.strip()]
    argv = shlex.split(command)  # ValueError on unbalanced quotes
    if not argv:
        raise ValueError("startCommand must not be empty")
    return argv


def start_command_errors(command, os_name=None):
    if command is None or command in ("", []):
        return ["minecraft.startCommand is required"]
    try:
        start_command_argv(command, os_name)
    except ValueError as e:
        return [f"minecraft.startCommand is invalid: {e}"]
    return []


def start_command_warnings(command, server_dir, os_name=None):
    """Warn when the launch executable cannot be found (PATH may differ at runtime)."""
    os_name = os_name or os.name
    try:
        argv = start_command_argv(command, os_name)
    except ValueError:
        return []
    if os_name == "nt" and isinstance(command, str):
        return []  # CreateProcess parses the string; do not guess
    executable = argv[0]
    if os.sep in executable or (os.altsep and os.altsep in executable):
        candidate = os.path.join(server_dir or "", executable)
        if not os.path.exists(candidate):
            return [
                f"minecraft.startCommand: {executable} does not exist in {server_dir}"
            ]
        return []
    if shutil.which(executable) is None:
        return [
            (
                f"minecraft.startCommand: '{executable}' was not found on PATH "
                "(install Java or use the full path to the java executable)"
            )
        ]
    return []


def restart_loop_warnings(command, server_dir):
    """Warn when startCommand runs a start script that restarts the server in
    a loop (checked at every startup, not only by the setup wizard)."""
    if not server_dir or not os.path.isdir(server_dir) or not command:
        return []
    from src.setup_wizard import restart_loop_scripts

    loops = set(restart_loop_scripts(server_dir))
    if not loops:
        return []
    tokens = command if isinstance(command, (list, tuple)) else str(command).split()
    names = {
        os.path.basename(str(token).strip("\"'").replace("\\", "/")) for token in tokens
    }
    return [
        f"minecraft.startCommand runs {name}, which looks like it restarts the "
        "server in a loop: auto-stop cannot work (the script starts the server "
        "again). Launch the server directly instead (e.g. ./run.sh nogui or "
        "java -jar ...)."
        for name in sorted(loops & names)
    ]


# ----------------------------------------------------------------------
# Value helpers
# ----------------------------------------------------------------------


def _int_or(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _truthy(value):
    return str(value).strip().lower() == "true"


def _blank_to_none(value):
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _resolved_port(explicit, from_properties, default):
    explicit = _blank_to_none(explicit)
    if explicit is None:
        return _int_or(from_properties, default)
    return _coerce_number(explicit, integer=True)


def _coerce_list(value):
    """A list from YAML/CLI: None -> [], "a, b" -> ["a", "b"]."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in re.split(r"[\s,]+", value.strip()) if part]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value]
    return value


def _yaml_kind(value):
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "a number"
    return f"a {type(value).__name__}"


def _read_secret_file(path):
    """The 64-character key stored in *path* (tightened to 0600), or None."""
    try:
        with open(path, encoding="utf-8") as file:
            saved = file.read().strip()
    except OSError:
        return None
    if len(saved) != 64:
        return None
    _make_private(path)
    return saved


def _make_private(path):
    if os.name == "nt":
        return
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError as e:
        logger.warning(f"Could not restrict the permissions of {path}: {e}")


def _write_private_file(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
    _make_private(path)  # an existing file keeps its mode despite O_CREAT


def _coerce_bool(value):
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return value


def _coerce_number(value, integer):
    if not isinstance(value, str):
        return value
    text = value.strip()
    try:
        return int(text)
    except ValueError:
        pass
    if integer:
        return value
    try:
        return float(text)
    except ValueError:
        return value


def _is_loopback(host):
    if not isinstance(host, str):
        return False
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


settings = Settings()
