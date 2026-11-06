"""Reusable in-memory fakes for lifecycle, controller and web tests.

They implement the documented APIs (docs/ARCHITECTURE.md), record their
calls, and can block to simulate slow I/O. No processes, sockets or files.

- ``FakeClock`` / ``FakeWallClock``: callables returning a settable time;
  ``advance(seconds)``. Inject as the supervisor's ``clock`` (monotonic)
  and ``wall_clock`` (epoch seconds).
- ``OffsetClock``: real monotonic time plus ``advance(seconds)``, for tests
  against real servers (a frozen clock would stop the polls).
- ``FakeProcessManager``: ``alive`` (live PIDs), ``found`` (what discovery
  reports), ``launch_identity`` (what the next launch returns), plus
  blocking/result knobs for launch, terminate and observe. ``observe_error``
  makes ``observe()`` raise (e.g. ``ProcessStateUnknown``); ``created_at``
  maps PIDs to the wall-clock creation time ``started_at()`` reports.
- ``FakeClient``: ``roster`` returned by ``get_roster()`` (``None`` or
  ``available = False`` means RCON and ping both failed), ``block`` to hold a
  poll, configurable moderation/console results. With ``banlist`` set, ban and
  unban change that FakeBanlistManager's "file" like the real server does.
- ``FakePlayerManager`` / ``FakeBanlistManager``: the app-shell managers'
  APIs over dicts, publishing their events when given a bus.
- ``make_roster(*names, online=None, ...)`` and ``fake_uuid(name)`` helpers.
- ``HandledSpy(supervisor)``: counts messages once the lifecycle thread has
  handled them, so tests wait for consumption instead of sleeping.
"""

import hashlib
import threading
import time
import uuid as uuid_module

from src.events import Event
from src.lifecycle.messages import ActionResult, OnlinePlayer, ProcessSnapshot, Roster
from src.minecraft.client import RconUnavailable


def fake_uuid(name: str) -> str:
    """Deterministic dashed UUID for a name (offline-mode style, v3)."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    return str(uuid_module.UUID(bytes=bytes(digest), version=3))


def make_roster(
    *names,
    online=None,
    max_players=20,
    source="rcon",
    version="1.20.1",
    motd="A Minecraft Server",
    with_uuids=True,
) -> Roster:
    """A successful roster; ``online`` defaults to the number of names."""
    players = tuple(
        OnlinePlayer(name, fake_uuid(name) if with_uuids else None) for name in names
    )
    return Roster(
        online=len(players) if online is None else online,
        players=players,
        max_players=max_players,
        source=source,
        version=version,
        motd=motd,
    )


class FakeClock:
    """A settable clock: ``clock()`` returns ``clock.now``."""

    def __init__(self, now=1000.0):
        self._lock = threading.Lock()
        self.now = float(now)

    def __call__(self):
        with self._lock:
            return self.now

    def advance(self, seconds):
        with self._lock:
            self.now += seconds
            return self.now


class FakeWallClock(FakeClock):
    """Epoch-seconds clock for snapshot timestamps."""

    def __init__(self, now=1_762_300_000.0):
        super().__init__(now)


class OffsetClock:
    """``time.monotonic()`` plus an offset that ``advance()`` pushes forward.

    For tests with real servers: time keeps flowing (polls stay due), and the
    test can still jump over the auto-stop startup guard and delay.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.offset = 0.0

    def __call__(self):
        with self._lock:
            return time.monotonic() + self.offset

    def advance(self, seconds):
        with self._lock:
            self.offset += seconds
        return self()


class FakeProcessManager:
    """In-memory process backend with the OSProcessManager API."""

    def __init__(self):
        self._lock = threading.RLock()
        self.identity = None  # tracked identity
        self.alive = set()  # PIDs that are running
        self.found = None  # what find_process_identity() reports
        self.launch_identity = None  # what the next launch returns (None = fails)
        self.launch_block = None  # threading.Event: launch waits on it
        self.launch_entered = threading.Event()
        self.terminate_block = None
        self.terminate_entered = threading.Event()
        self.terminate_result = True  # True gone / False alive / None pending
        self.terminate_mutates = True  # a True result also kills the process
        self.observe_gate = None  # threading.Event: the next observe() waits
        self.observe_entered = threading.Event()
        self.gated_snapshot = None  # returned by the gated observe()
        self.find_error = None  # exception raised by find_process_identity()
        self.observe_error = None  # exception raised by observe()
        self.created_at = {}  # pid -> wall-clock creation time (started_at)
        self.launch_calls = []
        self.terminate_calls = []
        self.terminate_forced = []  # the force flag of each terminate call
        self.find_calls = 0
        self.closed = False

    @property
    def tracked_identity(self):
        return self.identity

    def set_known_identity(self, identity):
        with self._lock:
            self.identity = identity

    def clear_identity(self, expected):
        with self._lock:
            if self.identity is None or expected is None:
                return False
            if not self.identity.matches(expected):
                return False
            self.identity = None
            return True

    def identity_is_running(self, identity):
        return identity is not None and identity.pid in self.alive

    def find_process_identity(self):
        self.find_calls += 1
        if self.find_error is not None:
            raise self.find_error
        return self.found

    def started_at(self, identity):
        return self.created_at.get(identity.pid)

    def observe(self):
        if self.observe_error is not None:
            raise self.observe_error
        if self.observe_gate is not None:
            gate = self.observe_gate
            self.observe_gate = None
            self.observe_entered.set()
            gate.wait(5)
            return self.gated_snapshot
        return ProcessSnapshot(
            tracked=self.identity,
            tracked_running=self.identity_is_running(self.identity),
            found=self.found,
        )

    def launch_process(self):
        self.launch_calls.append(self.launch_identity)
        self.launch_entered.set()
        if self.launch_block is not None:
            self.launch_block.wait(5)
        with self._lock:
            if self.launch_identity is None:
                return None
            self.identity = self.launch_identity
            self.alive.add(self.identity.pid)
            self.found = self.identity
            return self.identity

    def terminate_process(self, expected=None, force=True):
        self.terminate_calls.append(expected)
        self.terminate_forced.append(force)
        if self.terminate_block is not None:
            self.terminate_entered.set()
            self.terminate_block.wait(5)
        if self.terminate_result is not True:
            return self.terminate_result
        with self._lock:
            if self.terminate_mutates:
                if self.identity is None or not self.identity.matches(expected):
                    return False
                self.alive.discard(self.identity.pid)
                self.identity = None
                self.found = None
        return True

    def exit(self, identity):
        """The process exits on its own (crash, in-game /stop)."""
        with self._lock:
            self.alive.discard(identity.pid)
            if self.found is not None and self.found.matches(identity):
                self.found = None

    def close(self):
        self.closed = True


class FakeClient:
    """MinecraftClient stand-in (RCON + status ping)."""

    def __init__(self, roster=None, rcon_enabled=True):
        self._lock = threading.Lock()
        self.roster = roster if roster is not None else make_roster()
        self.available = True
        self.block = None  # threading.Event: get_roster() waits on it
        self.entered = threading.Event()
        self.calls = 0
        self.rcon_enabled = rcon_enabled
        self.banlist = None  # FakeBanlistManager the "server" writes on /ban
        self.ban_writes_file = True
        self.kick_result = None  # None -> success
        self.ban_result = None
        self.unban_result = None
        self.command_output = ""
        self.command_error = None  # exception raised by run_command()
        self.stop_result = True
        self.kick_calls = []
        self.ban_calls = []
        self.unban_calls = []
        self.commands = []
        self.said = []
        self.stop_requests = 0
        self.closed = False

    def hold(self, roster=None):
        """Block the next polls until the returned event is set.

        Atomic: every poll that blocks answers ``roster`` (default: the
        current one), and ``entered`` is set only by such a poll.
        """
        gate = threading.Event()
        with self._lock:
            if roster is not None:
                self.roster = roster
            self.block = gate
            self.entered.clear()
        return gate

    def get_roster(self):
        with self._lock:
            self.calls += 1
            roster = self.roster  # what the server answered when asked
            block = self.block
            self.entered.set()
        if block is not None:
            block.wait(5)
        if not self.available:
            return None
        return roster

    def run_command(self, command):
        self.commands.append(command)
        if self.command_error is not None:
            raise self.command_error
        if not self.rcon_enabled:
            raise RconUnavailable("RCON is not configured")
        return self.command_output

    def kick_player(self, name, reason=None):
        self.kick_calls.append((name, reason))
        if self.kick_result is not None:
            return self.kick_result
        return ActionResult(True, f"Kicked {name}")

    def ban_player(self, name, reason=None):
        self.ban_calls.append((name, reason))
        if self.banlist is not None and self.ban_writes_file:
            self.banlist.server_ban(name, fake_uuid(name), reason)
        if self.ban_result is not None:
            return self.ban_result
        return ActionResult(True, f"Banned {name}")

    def unban_player(self, name):
        self.unban_calls.append(name)
        if self.banlist is not None and self.ban_writes_file:
            self.banlist.server_pardon(name)
        if self.unban_result is not None:
            return self.unban_result
        return ActionResult(True, f"Unbanned {name}")

    def say(self, message):
        self.said.append(message)
        return True

    def request_stop(self):
        self.stop_requests += 1
        return self.stop_result

    def close(self):
        self.closed = True


class FakePlayerManager:
    """PlayerManager API over a dict keyed by lowercase name."""

    def __init__(self, bus=None, clock=time.time):
        self.bus = bus
        self._clock = clock
        self._lock = threading.RLock()
        self.players = {}
        self.roster_updates = []
        self.mark_offline_calls = 0

    def seed(self, name, uuid=None, online=False, last_online=None):
        now = self._clock()
        self.players[name.lower()] = {
            "name": name,
            "uuid": uuid,
            "currently_online": online,
            "last_online": last_online if last_online is not None else now,
            "first_seen": now,
        }

    def update_from_roster(self, roster):
        events = []
        with self._lock:
            self.roster_updates.append(roster)
            now = self._clock()
            listed = {
                self._mark_online(player, now, events) for player in roster.players
            }
            # Like the real manager: a partial roster (hidden names) only adds.
            if len(roster.players) >= roster.online:
                for key, record in self.players.items():
                    if key not in listed and record["currently_online"]:
                        record["currently_online"] = False
                        events.append((Event.PLAYER_LEFT, self._payload(record)))
        self._publish(events)

    def _mark_online(self, player, now, events):
        key = player.name.lower()
        record = self.players.setdefault(
            key,
            {
                "name": player.name,
                "uuid": player.uuid,
                "currently_online": False,
                "last_online": now,
                "first_seen": now,
            },
        )
        record["name"] = player.name
        record["uuid"] = player.uuid or record["uuid"]
        record["last_online"] = now
        if not record["currently_online"]:
            record["currently_online"] = True
            events.append((Event.PLAYER_JOINED, self._payload(record)))
        return key

    def mark_all_offline(self):
        events = []
        with self._lock:
            self.mark_offline_calls += 1
            for record in self.players.values():
                if record["currently_online"]:
                    record["currently_online"] = False
                    events.append((Event.PLAYER_LEFT, self._payload(record)))
        self._publish(events)

    def get_all_players(self):
        with self._lock:
            return [dict(record) for record in self.players.values()]

    def get_online_players(self):
        with self._lock:
            return [dict(r) for r in self.players.values() if r["currently_online"]]

    def get_player_count(self):
        return len(self.get_online_players())

    def find_uuid(self, name):
        with self._lock:
            record = self.players.get((name or "").strip().lower())
            return record["uuid"] if record else None

    @staticmethod
    def _payload(record):
        return {"name": record["name"], "uuid": record["uuid"]}

    def _publish(self, events):
        if self.bus is not None:
            for event_type, payload in events:
                self.bus.publish(event_type, payload)


class FakeBanlistManager:
    """BanlistManager API over an in-memory banned-players.json.

    ``server_ban``/``server_pardon`` change the "file" like the server does
    on /ban and /pardon; reads see the file at once (the real manager
    re-reads on mtime change); ``refresh()`` publishes BAN_ADDED/BAN_REMOVED.
    """

    def __init__(self, bus=None, usercache=None):
        self.bus = bus
        self._lock = threading.RLock()
        self.entries = []
        self.usercache = {
            name.lower(): uuid for name, uuid in (usercache or {}).items()
        }
        self.require_uuid = True  # offline bans need a known UUID
        self.offline_result = None  # forced ActionResult for offline edits
        self.refresh_calls = 0
        self.offline_add_calls = []
        self.offline_remove_calls = []
        self._known = {}

    def server_ban(self, name, uuid=None, reason=None):
        with self._lock:
            if self.is_banned(name):
                return
            self.entries.append(
                {
                    "name": name,
                    "uuid": uuid,
                    "reason": reason or "Banned by an operator.",
                    "source": "Server",
                    "created": "2026-11-05 10:00:00 +0000",
                    "expires": "forever",
                }
            )

    def server_pardon(self, name):
        with self._lock:
            lowered = name.lower()
            self.entries = [e for e in self.entries if e["name"].lower() != lowered]

    def get_banned_players(self):
        with self._lock:
            return [dict(entry) for entry in self.entries]

    def is_banned(self, name):
        lowered = (name or "").strip().lower()
        with self._lock:
            return any(entry["name"].lower() == lowered for entry in self.entries)

    def refresh(self):
        with self._lock:
            self.refresh_calls += 1
            current = {
                e["name"].lower(): {"name": e["name"], "uuid": e["uuid"]}
                for e in self.entries
            }
            added = [current[key] for key in current if key not in self._known]
            removed = [self._known[key] for key in self._known if key not in current]
            self._known = current
        if self.bus is not None:
            for payload in added:
                self.bus.publish(Event.BAN_ADDED, dict(payload))
            for payload in removed:
                self.bus.publish(Event.BAN_REMOVED, dict(payload))

    def lookup_uuid(self, name):
        lowered = (name or "").strip().lower()
        if lowered in self.usercache:
            return self.usercache[lowered]
        with self._lock:
            for entry in self.entries:
                if entry["name"].lower() == lowered and entry["uuid"]:
                    return entry["uuid"]
        return None

    def add_ban_offline(self, name, uuid=None, reason=None):
        self.offline_add_calls.append((name, uuid, reason))
        if self.offline_result is not None:
            return self.offline_result
        if uuid is None and self.require_uuid:
            return ActionResult(
                False, f"Unknown UUID for {name}; start the server to ban this player."
            )
        if self.is_banned(name):
            return ActionResult(True, f"{name} is already banned.")
        self.server_ban(name, uuid, reason)
        self.refresh()
        return ActionResult(True, f"Banned {name}.")

    def remove_ban_offline(self, name):
        self.offline_remove_calls.append(name)
        if self.offline_result is not None:
            return self.offline_result
        if not self.is_banned(name):
            return ActionResult(True, f"{name} is not banned.")
        self.server_pardon(name)
        self.refresh()
        return ActionResult(True, f"Unbanned {name}.")


class HandledSpy:
    """Wrap ``supervisor.handle`` to count messages after they were handled.

    The runtime looks ``supervisor.handle`` up per message, so the spy sees
    everything the lifecycle thread processes. ``count(kind)`` is the number
    of handled messages of that type; wait for it to grow instead of sleeping.
    """

    def __init__(self, supervisor):
        self._lock = threading.Lock()
        self._handled = []
        self._handle = supervisor.handle
        supervisor.handle = self._spy

    def _spy(self, item):
        try:
            return self._handle(item)
        finally:
            with self._lock:
                self._handled.append(item)

    def count(self, kind=object, predicate=None):
        with self._lock:
            return sum(
                1
                for item in self._handled
                if isinstance(item, kind) and (predicate is None or predicate(item))
            )
