"""Shared lifecycle message, roster and process model types.

CONTRACT FILE: every module codes against these types. Data only: no
application logic and no imports from the rest of the application.
Change only with the lead's agreement (see docs/ARCHITECTURE.md).
"""

import threading
from dataclasses import dataclass, field
from typing import Optional


class LifecycleState:
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"


# ----------------------------------------------------------------------
# Process models
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessIdentity:
    """A process we own, identified by PID and (when available) creation time."""

    pid: int
    create_time: Optional[float] = None

    def matches(self, other: Optional["ProcessIdentity"]) -> bool:
        if other is None or self.pid != other.pid:
            return False
        if self.create_time is not None and other.create_time is not None:
            return abs(self.create_time - other.create_time) < 0.01
        return True


@dataclass(frozen=True)
class ProcessSnapshot:
    """One I/O observation of the tracked and any discovered server process."""

    tracked: Optional[ProcessIdentity]
    tracked_running: bool
    found: Optional[ProcessIdentity]


@dataclass(frozen=True)
class ProcessObservation:
    """A process scan tagged with the generation it started under."""

    generation: int
    snapshot: ProcessSnapshot


# ----------------------------------------------------------------------
# Roster models (what one successful poll of the Minecraft server returns)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class OnlinePlayer:
    """A player reported online by the server.

    ``uuid`` is the dashed lowercase UUID string when the server reported it
    (RCON ``list uuids`` or the status-ping sample), otherwise None.
    """

    name: str
    uuid: Optional[str] = None


@dataclass(frozen=True)
class Roster:
    """One successful roster sample.

    ``online`` is the authoritative online count and is what auto-stop uses.
    ``players`` holds the players whose names are known; it can be shorter
    than ``online`` (status-ping samples are capped at 12 and servers can hide
    names). Anonymous ping-sample entries are never included in ``players``.
    """

    online: int
    players: tuple[OnlinePlayer, ...] = ()
    max_players: Optional[int] = None
    source: str = "rcon"  # "rcon" | "ping"
    version: Optional[str] = None  # e.g. "1.20.1" (from the status ping)
    motd: Optional[str] = None  # plain text, formatting codes stripped


# ----------------------------------------------------------------------
# Supervisor <-> runtime requests and results
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchRequest:
    generation: int


@dataclass(frozen=True)
class LaunchResult:
    generation: int
    identity: Optional[ProcessIdentity]


@dataclass(frozen=True)
class PollRequest:
    generation: int
    identity: ProcessIdentity
    seq: int


@dataclass(frozen=True)
class RosterSample:
    generation: int
    identity: ProcessIdentity
    seq: int
    roster: Optional[Roster]  # None means RCON and the status ping both failed.


@dataclass(frozen=True)
class StopRequest:
    generation: int
    identity: ProcessIdentity


@dataclass(frozen=True)
class StopResult:
    generation: int
    identity: Optional[ProcessIdentity]
    stopped: Optional[bool]  # True=gone, False=failed, None=pending shutdown


# ----------------------------------------------------------------------
# Commands submitted by the facade (web UI, auto-start) to the supervisor
# ----------------------------------------------------------------------


class Command:
    def __init__(self):
        self.done = threading.Event()
        self.result = False


class StartCommand(Command):
    pass


class StopCommand(Command):
    pass


class RestartCommand(Command):
    """RUNNING: stop, then start as soon as the exit is committed.
    STOPPED: same as StartCommand. STARTING/STOPPING: rejected."""


# ----------------------------------------------------------------------
# Moderation / console results returned by the controller facade
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ActionResult:
    """Outcome of a kick/ban/unban/console action, shown to the admin."""

    success: bool
    message: str = ""
    output: Optional[str] = None  # raw RCON response text, when there was one
    details: dict = field(default_factory=dict)
