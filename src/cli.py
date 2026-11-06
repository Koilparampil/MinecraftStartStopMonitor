"""CLI argument parsing, settings-file location and application to settings."""

import argparse
import ipaddress
import logging
import math
import os
import sys

from src.settings import mask_setting
from src.settings import settings as _settings

logger = logging.getLogger(__name__)

SETTINGS_ENV_VAR = "MINECRAFT_MONITOR_SETTINGS"
SETTINGS_FILE_NAME = "settings.yaml"

# argparse destinations that are not settings attributes.
_NOT_SETTINGS = {"settings"}


def _port(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port: {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between 1 and 65535: {port}")
    return port


def _positive_number(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number: {value}")
    return int(number) if number.is_integer() else number


def _address_list(value):
    """Comma-separated IP addresses / networks (CIDR)."""
    entries = [part.strip() for part in value.split(",") if part.strip()]
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"not an IP address or network: {entry!r}"
            ) from None
    return entries


def _non_empty(value):
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _add_server_arguments(parser):
    group = parser.add_argument_group("Minecraft server")
    group.add_argument(
        "--server-dir",
        dest="serverDir",
        metavar="PATH",
        type=_non_empty,
        help="Server folder (server.properties, mods/, world/)",
    )
    group.add_argument(
        "--start-command",
        dest="startCommand",
        metavar="CMD",
        type=_non_empty,
        help="Launch command run inside the server folder, "
        'e.g. "./run.sh nogui" or "java -Xmx6G -jar fabric-server-launch.jar nogui"',
    )
    group.add_argument(
        "--host",
        dest="serverHost",
        metavar="HOST",
        type=_non_empty,
        help="Host the monitor uses for RCON and the status ping (default 127.0.0.1)",
    )
    group.add_argument(
        "--server-port",
        dest="serverPort",
        metavar="PORT",
        type=_port,
        help="Game port (default: server-port from server.properties, else 25565)",
    )
    group.add_argument(
        "--rcon-port",
        dest="rconPort",
        metavar="PORT",
        type=_port,
        help="RCON port (default: rcon.port from server.properties, else 25575)",
    )
    group.add_argument(
        "--rcon-password",
        dest="rconPassword",
        metavar="PASS",
        help="RCON password (default: rcon.password from server.properties)",
    )
    group.add_argument(
        "--polling-rate",
        dest="pollingRate",
        metavar="SEC",
        type=_positive_number,
        help="Seconds between status polls",
    )
    group.add_argument(
        "--stop-timeout",
        dest="stopTimeout",
        metavar="SEC",
        type=_positive_number,
        help="Seconds to wait for a graceful save+stop before terminating the server",
    )


def _add_web_arguments(parser):
    group = parser.add_argument_group("Web dashboard")
    group.add_argument(
        "--no-web",
        dest="useWebServer",
        action="store_false",
        default=None,
        help="Disable the web dashboard",
    )
    group.add_argument(
        "--web-port",
        dest="webServerPort",
        metavar="PORT",
        type=_port,
        help="Web dashboard port (default 25580)",
    )
    group.add_argument(
        "--web-username",
        dest="webUsername",
        metavar="USER",
        type=_non_empty,
        help="Web dashboard username",
    )
    group.add_argument(
        "--web-password",
        dest="webPassword",
        metavar="PASS",
        type=_non_empty,
        help="Web dashboard password",
    )
    group.add_argument(
        "--trusted-proxies",
        dest="trustedProxies",
        metavar="LIST",
        type=_address_list,
        help="Comma-separated reverse-proxy addresses/networks whose "
        "X-Forwarded-For header is trusted",
    )
    group.add_argument(
        "--secure-cookies",
        dest="secureCookies",
        action="store_true",
        default=None,
        help="Send the dashboard's session cookies over HTTPS only",
    )


def _add_feature_arguments(parser):
    group = parser.add_argument_group("Features")
    group.add_argument(
        "--no-auto-start",
        dest="autoStart",
        action="store_false",
        default=None,
        help="Do not listen on the game port to start the server on a join attempt",
    )
    group.add_argument(
        "--no-auto-stop",
        dest="autoStop",
        action="store_false",
        default=None,
        help="Do not stop the server when it is empty",
    )
    group.add_argument(
        "--auto-stop-delay",
        dest="autoStopDelay",
        metavar="SEC",
        type=_positive_number,
        help="Seconds the server must be empty before it is stopped (default 600)",
    )


def build_parser():
    """Return an ArgumentParser for minecraft-monitor CLI flags."""
    parser = argparse.ArgumentParser(
        prog="minecraft-monitor",
        description="Modded Minecraft (Java) server monitor: starts the server when a "
        "player tries to join, stops it when empty, and serves a web admin dashboard.",
    )
    parser.add_argument(
        "--settings",
        metavar="PATH",
        help=f"Path to settings.yaml (default: ${SETTINGS_ENV_VAR}, else "
        "settings.yaml next to the program)",
    )
    _add_server_arguments(parser)
    _add_web_arguments(parser)
    _add_feature_arguments(parser)
    return parser


def parse_args(argv=None):
    """Parse CLI arguments.  *argv* overrides sys.argv for testing."""
    return build_parser().parse_args(argv)


def apply_overrides(args, settings=None):
    """Apply non-None CLI args as settings overrides (sensitive values masked in logs).

    *settings* defaults to the global settings singleton. Returns the applied
    {attribute: value} mapping.
    """
    if settings is None:
        settings = _settings

    applied = {}
    for key, value in vars(args).items():
        if value is None or key in _NOT_SETTINGS:
            continue
        settings[key] = value
        applied[key] = value
        logger.info(f"CLI override: {key}={mask_setting(key, value)}")
    return applied


def default_settings_path(frozen=None, executable=None):
    """settings.yaml next to the executable (frozen build) or inside src/."""
    if frozen is None:
        frozen = getattr(sys, "frozen", False)
    if frozen:
        return os.path.join(
            os.path.dirname(executable or sys.executable), SETTINGS_FILE_NAME
        )
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_FILE_NAME)


def determine_settings_path(args, environ=None, frozen=None, executable=None):
    """--settings, then $MINECRAFT_MONITOR_SETTINGS, then the default location."""
    environ = os.environ if environ is None else environ
    path = getattr(args, "settings", None) or environ.get(SETTINGS_ENV_VAR)
    if path:
        return os.path.abspath(os.path.expanduser(path))
    return default_settings_path(frozen=frozen, executable=executable)
