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

import logging
import os
import secrets
import shutil
import subprocess

import yaml

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
    "dataDir": "data",  # players.json, server.pid, server-console.log (relative to cwd)
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

DEFAULT_GAME_PORT = 25565
DEFAULT_RCON_PORT = 25575


class Settings:
    """Application settings held as attributes; item access mirrors attribute access."""

    def __init__(self):
        vars(self).update(dict(DEFAULTS))

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _ensure_session_secret_key(self, settings_file_path):
        """Load or generate a session secret key next to the settings file."""
        key_file = os.path.join(
            os.path.dirname(os.path.abspath(settings_file_path)), "session_secret.key"
        )
        try:
            if os.path.exists(key_file):
                with open(key_file, "r") as f:
                    saved = f.read().strip()
                if saved and len(saved) == 64:
                    self.sessionSecretKey = saved
                    return
        except OSError:
            pass

        if self.sessionSecretKey:
            return

        self.sessionSecretKey = secrets.token_hex(32)
        try:
            with open(key_file, "w") as f:
                f.write(self.sessionSecretKey)
            logging.info("Auto-generated sessionSecretKey and saved to session_secret.key")
        except OSError:
            pass

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

        # Flat top-level keys (scalars) are accepted as-is.
        for key, value in data.items():
            if key not in YAML_MAPPING and isinstance(
                value, (str, int, float, bool, type(None))
            ):
                flat_settings[key] = value
        return flat_settings

    def readSettings(self, file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                file_data = yaml.safe_load(file) or {}
            for key, value in self._load_nested_settings(file_data).items():
                self[key] = value
            print("Settings loaded successfully.")
        except FileNotFoundError:
            logging.warning(f"Settings file not found: {file_path}")
        except yaml.YAMLError as e:
            logging.warning(f"Error: Invalid YAML format in {file_path}: {e}")
        except Exception:
            logging.exception("Error from readSettings")

        self._ensure_session_secret_key(file_path)

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

    def resolve_server_properties(self):
        """Fill unset ports/password/bind host from the server's server.properties.

        Explicit settings (YAML or CLI) always win. Called by main after CLI
        overrides and before validation. Safe to call when the file is missing.
        """
        from src.minecraft.server_files import read_properties

        props = {}
        path = self.server_properties_path()
        if path and os.path.isfile(path):
            try:
                props = read_properties(path)
            except Exception:
                logging.exception(f"Could not read {path}")

        if self.serverPort is None:
            self.serverPort = _int_or(props.get("server-port"), DEFAULT_GAME_PORT)
        if self.rconPort is None:
            self.rconPort = _int_or(props.get("rcon.port"), DEFAULT_RCON_PORT)
        if self.rconPassword is None and _truthy(props.get("enable-rcon")):
            self.rconPassword = props.get("rcon.password") or None
        if self.listenerBindHost is None:
            self.listenerBindHost = props.get("server-ip", "") or ""
        return props

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_settings(self):
        """Validate mandatory settings.

        Raises:
            ValueError: listing every problem found.
        """
        errors = []

        if not self.get("serverDir"):
            errors.append("minecraft.serverDir is required")
        elif not os.path.isdir(self.serverDir):
            errors.append(f"minecraft.serverDir does not exist: {self.serverDir}")

        if not self.get("startCommand"):
            errors.append("minecraft.startCommand is required")

        if self.get("useWebServer", True) and not self.get("webPassword"):
            errors.append("web.password is required when the web server is enabled")

        for name, value in [
            ("minecraft.port", self.serverPort),
            ("minecraft.rconPort", self.rconPort),
            ("web.port", self.webServerPort),
        ]:
            if value is None:
                continue
            try:
                port = int(value)
                if not 1 <= port <= 65535:
                    errors.append(f"{name} must be between 1 and 65535 (got {port})")
            except (TypeError, ValueError):
                errors.append(f"{name} is not a valid integer: {value}")

        for name in ("pollingRate", "stopTimeout", "autoStopDelay"):
            try:
                if float(self.get(name)) <= 0:
                    errors.append(f"{name} must be positive")
            except (TypeError, ValueError):
                errors.append(f"{name} is not a number: {self.get(name)}")

        if errors:
            raise ValueError(
                "Settings validation failed:\n"
                + "\n".join(f"  - {error}" for error in errors)
            )

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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=os.path.dirname(__file__),
                check=True,
                timeout=5,
            )
            return result.stdout.strip() or None
        except Exception:
            return None


def _int_or(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _truthy(value):
    return str(value).strip().lower() == "true"


settings = Settings()
