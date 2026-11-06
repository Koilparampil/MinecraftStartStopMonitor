# Copyright (c) 2024 Nomomo
# Copyright (c) 2024 Kevin Perez - Modified work
# Copyright (c) 2026 Koilparampil - Modified work (Minecraft port)

"""Entry point and composition root (``python -m src.main``).

Order: banner -> logging -> settings (first-run wizard if missing) -> CLI
overrides -> server.properties resolution -> validation -> client -> bus ->
process manager -> managers -> controller -> auto-start -> web dashboard
(blocks) or, with --no-web, wait for SIGINT/SIGTERM.

Shutting the monitor down never stops the Minecraft server: a running server
is adopted again on the next start.
"""

import logging
import logging.handlers
import os
import signal
import sys
import threading

from src.auto_start import AutoStartManager
from src.banlist_manager import BanlistManager
from src.cli import apply_overrides, determine_settings_path, parse_args
from src.events import EventBus
from src.minecraft.client import MinecraftClient
from src.minecraft_control import MinecraftController
from src.player_manager import PlayerManager
from src.process_manager import create_process_manager
from src.settings import SENSITIVE_SETTINGS, settings
from src.setup_wizard import (
    detect_server_dir,
    interactive_setup,
    no_terminal_help,
    print_web_link,
)

logger = logging.getLogger("src.main")

LOG_FILE = "monitor.log"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3

BANNER = r"""
 __  __ ___ _   _ _____ ____ ____      _    _____ _____
|  \/  |_ _| \ | | ____/ ___|  _ \    / \  |  ___|_   _|
| |\/| || ||  \| |  _|| |   | |_) |  / _ \ | |_    | |
| |  | || || |\  | |__| |___|  _ <  / ___ \|  _|   | |
|_|  |_|___|_| \_|_____\____|_| \_\/_/   \_\_|     |_|

 __  __  ___  _   _ ___ _____ ___  ____
|  \/  |/ _ \| \ | |_ _|_   _/ _ \|  _ \
| |\/| | | | |  \| || |  | || | | | |_) |
| |  | | |_| | |\  || |  | || |_| |  _ <
|_|  |_|\___/|_| \_|___| |_| \___/|_| \_\
"""


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------


class RedactingFormatter(logging.Formatter):
    """Masks configured secrets (passwords, session key) in formatted records,
    tracebacks included. A safety net: modules must not log secrets anyway."""

    def format(self, record):
        text = super().format(record)
        for key in SENSITIVE_SETTINGS:
            secret = settings.get(key)
            if isinstance(secret, str) and len(secret) >= 4 and secret in text:
                text = text.replace(secret, "***")
        return text


def log_file_path(settings_path):
    """monitor.log lives next to the settings file, wherever we are started."""
    return os.path.join(os.path.dirname(os.path.abspath(settings_path)), LOG_FILE)


def configure_logging(log_file=LOG_FILE):
    """Log to stdout and a rotating ``monitor.log`` (level from $LOG_LEVEL)."""
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), None)
    if not isinstance(level, int):
        level = logging.INFO
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUPS,
                encoding="utf-8",
            )
        )
    except OSError as e:
        print(f"warning: cannot write {log_file}: {e}", file=sys.stderr)
    formatter = RedactingFormatter(LOG_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


# ----------------------------------------------------------------------
# Signals
# ----------------------------------------------------------------------


class ShutdownSignals:
    """SIGINT/SIGTERM (SIGBREAK on Windows) unwind the main thread once.

    The handler raises KeyboardInterrupt, exactly like Ctrl+C, so a blocking
    web server or wait returns and main's cleanup runs (systemd stops are
    clean). Signals that arrive while shutting down are ignored, so cleanup
    is never interrupted halfway.
    """

    def __init__(self):
        self.received = None
        self._shutting_down = False

    def install(self):
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                signal.signal(signum, self._handle)
            except (ValueError, OSError):  # not the main thread / unsupported
                logger.debug(f"Cannot handle {name}")

    def shutting_down(self):
        self._shutting_down = True

    def _handle(self, signum, _frame):
        if self._shutting_down or self.received is not None:
            return
        self.received = signum
        raise KeyboardInterrupt


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


WIZARD_FLAGS = ("serverDir", "startCommand", "webPassword")


def cli_settings_complete(args):
    """The command line names the server folder, its launch command and
    (unless --no-web) a dashboard password: no settings file is needed."""
    web_ok = getattr(args, "useWebServer", None) is False or bool(
        getattr(args, "webPassword", None)
    )
    return bool(getattr(args, "serverDir", None) and args.startCommand and web_ok)


def _stdin_is_a_terminal():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def run_first_time_setup(settings_path, args):
    """No settings file: the interactive wizard (pre-filled from the command
    line), or just the command line when it is complete and there is no
    terminal to ask on. False = exit 1."""
    if cli_settings_complete(args) and not _stdin_is_a_terminal():
        logger.info(f"No settings file at {settings_path}: using the command line")
        return True
    given = {key: getattr(args, key, None) for key in WIZARD_FLAGS}
    try:
        interactive_setup(settings_path, detect_server_dir(), given=given)
        return True
    except EOFError:
        print()  # end the unanswered prompt's line
        for line in no_terminal_help(settings_path):
            print(line)
    except KeyboardInterrupt:
        print("\nSetup cancelled; nothing was saved.")
    except OSError as e:
        print(f"error: cannot write the settings file {settings_path}: {e}")
        print("Choose a writable location with --settings PATH.")
    return False


def load_settings(settings_path, args):
    """settings file -> CLI overrides -> server.properties -> validation."""
    logger.info(f"Loading settings from {settings_path}")
    settings.readSettings(settings_path)
    apply_overrides(args)
    settings.resolve_server_properties()
    try:
        settings.validate_settings()
    except ValueError as e:
        logger.error(e)
        logger.error("Please fix the settings and try again.")
        return False
    return True


def log_configuration():
    rcon = f"port {settings.rconPort}" if settings.rconPassword else "not configured"
    logger.info(f"Server folder: {settings.serverDir}")
    logger.info(f"Start command: {settings.startCommand}")
    logger.info(f"Game port: {settings.serverPort} | RCON: {rcon}")
    logger.info(
        f"Auto-start: {'on' if settings.autoStart else 'off'} | "
        f"Auto-stop: {f'after {settings.autoStopDelay}s empty' if settings.autoStop else 'off'}"
    )


# ----------------------------------------------------------------------
# Composition
# ----------------------------------------------------------------------


class MonitorApp:
    """Builds and owns the long-lived objects; close() releases them."""

    def __init__(self):
        self.client = None
        self.bus = None
        self.process_manager = None
        self.controller = None
        self.auto_start = None
        self.web_server = None
        self._stop = threading.Event()

    def build(self):
        self.client = MinecraftClient(
            settings.serverHost,
            settings.serverPort,
            settings.rconPort,
            settings.rconPassword,
        )
        self.bus = EventBus()
        self.process_manager = create_process_manager(
            graceful_stopper=self.client.request_stop
        )
        player_manager = PlayerManager(bus=self.bus)
        self.controller = MinecraftController(
            client=self.client,
            process_manager=self.process_manager,
            player_manager=player_manager,
            banlist_manager=BanlistManager(bus=self.bus, known_players=player_manager),
            bus=self.bus,
        )
        if settings.autoStart:
            # Subscribe before the supervisor's first scan can commit STOPPED.
            self.auto_start = AutoStartManager(self.controller, self.bus)
        self.controller.start()
        self._arm_auto_start()

    def _arm_auto_start(self):
        if self.auto_start is None:
            logger.info("Auto-start is disabled")
            return
        # The supervisor reports STOPPED until its first scan: never bind the
        # game port while an undiscovered server may already hold it.
        found = self.process_manager.find_process_identity()
        if found is None:
            self.auto_start.listen()
        else:
            logger.info(
                f"Found a running Minecraft server (PID {found.pid}); adopting it"
            )

    def serve(self, use_web):
        """Block until shutdown: the web server, or a signal with --no-web."""
        if use_web:
            from src.web_server import WebServer

            status = self.auto_start.status if self.auto_start is not None else None
            self.web_server = WebServer(
                self.controller, self.bus, auto_start_status=status
            )
            print_web_link(settings.webServerPort, settings.webBindHost)
            self.web_server.run()
            return
        logger.info("Web dashboard disabled; press Ctrl+C to stop the monitor")
        while not self._stop.wait(1.0):
            pass

    def request_stop(self):
        """Make serve() return (used by tests; signals raise KeyboardInterrupt)."""
        self._stop.set()
        if self.web_server is not None:
            self.web_server.shutdown()

    def close(self):
        """web -> auto-start -> controller -> client. Never stops the server."""
        for name, target in (
            ("web server", self.web_server),
            ("auto-start", self.auto_start),
            ("controller", self.controller),
            ("client", self.client),
        ):
            if target is None:
                continue
            try:
                target.close()
            except Exception:
                logger.exception(f"Error while closing the {name}")


def main(argv=None):
    args = parse_args(argv)
    signals = ShutdownSignals()
    signals.install()
    print(BANNER, flush=True)
    settings_path = determine_settings_path(args)
    configure_logging(log_file_path(settings_path))

    if not os.path.exists(settings_path) and not run_first_time_setup(
        settings_path, args
    ):
        return 1
    if not load_settings(settings_path, args):
        return 1
    log_configuration()

    app = MonitorApp()
    try:
        app.build()
        logger.info("Minecraft Monitor is running")
        app.serve(settings.useWebServer)
    except KeyboardInterrupt:
        pass
    finally:
        signals.shutting_down()
        logger.info(
            "Shutting down the monitor (a running Minecraft server keeps running)"
        )
        app.close()
    logger.info("Minecraft Monitor stopped")
    return 0


def run():
    try:
        return main()
    except KeyboardInterrupt:
        return 0
    except Exception:
        logger.exception("Error from src.main routine")
        return 1


if __name__ == "__main__":
    sys.exit(run())
