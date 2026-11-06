"""AutoStartManager: listen cycles, port release, exactly-once wake, re-arm,
lifecycle subscriptions and the access policy.

Real sockets on free ports, a line-based fake of sleeping.handle_connection
(the real protocol is covered in test/integration/test_auto_start_listener.py)
and a fake controller.
"""

import contextlib
import errno
import json
import socket
import threading
import time

import pytest

from src.auto_start import (
    AccessPolicy,
    _ListenCycle,
    BUSY_MESSAGE,
    AutoStartManager,
    ConnectionSlots,
    ListenerOptions,
    accept_error_action,
    bind_address,
    client_key,
    normalize_client_ip,
)
from src.events import Event, EventBus
from src.lifecycle.messages import LifecycleState
from src.minecraft.sleeping import ConnectionKind, ConnectionOutcome
from test.support import free_tcp_port, wait_until

FAST = ListenerOptions(
    bind_attempts=2,
    retry_delay=0.05,
    slow_retry_delay=0.1,
    accept_timeout=0.05,
    connection_timeout=2.0,
    release_timeout=3.0,
)


def can_bind(port, host="127.0.0.1"):
    """Bind like the Java server does (SO_REUSEADDR on POSIX)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(1)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def fake_handler(conn, ip, config, is_allowed, timeout=5.0):
    """Line protocol: 'STATUS' or 'LOGIN <name>'; replies with one line."""
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(64)
        if not chunk:
            return ConnectionOutcome(ConnectionKind.INVALID)
        data += chunk
    kind, _, name = data.decode().strip().partition(" ")
    if kind != "LOGIN":
        conn.sendall(f"MOTD {config.motd}\n".encode())
        conn.settimeout(timeout)  # like the real exchange: wait for the PING
        with contextlib.suppress(OSError):
            conn.recv(64)
        return ConnectionOutcome(ConnectionKind.STATUS)
    allowed, message = is_allowed(name, ip)
    reply = config.kick_message if allowed else message
    conn.sendall(f"{'KICK' if allowed else 'DENY'} {reply}\n".encode())
    return ConnectionOutcome(ConnectionKind.LOGIN, player_name=name, wake=allowed)


class LineRequests:
    """The fake protocol's request is one line."""

    @staticmethod
    def classify(data):
        if not data.endswith(b"\n"):
            return None
        return "login" if data.startswith(b"LOGIN") else "status"

    @staticmethod
    def busy_reply(kind, data, config):
        if kind == "login":
            return f"DENY {BUSY_MESSAGE}\n".encode()
        return f"MOTD {config.motd}\n".encode()


def talk(port, line, host="127.0.0.1"):
    with socket.create_connection((host, port), timeout=3) as sock:
        sock.sendall(line.encode() + b"\n")
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = sock.recv(256)
            if not chunk:
                break
            reply += chunk
    return reply.decode().strip()


class FakeController:
    def __init__(self, port, state=LifecycleState.STOPPED, accept=True):
        self.port = port
        self.state = state
        self.accept = accept
        self.start_calls = 0
        self.port_free_at_start = []
        self.started = threading.Event()

    def start_server(self):
        self.start_calls += 1
        self.port_free_at_start.append(can_bind(self.port))
        self.started.set()
        return self.accept


class AllowAll:
    def check(self, name, ip):
        return True, None


class DenyAll:
    def check(self, name, ip):
        return False, "You are not white-listed on this server!"


@pytest.fixture
def port(mock_settings):
    port = free_tcp_port()
    mock_settings.serverPort = port
    mock_settings.listenerBindHost = "127.0.0.1"
    return port


@pytest.fixture
def make_manager(port):
    managers = []

    def factory(controller=None, policy=None, bus=None, options=FAST):
        controller = controller or FakeController(port)
        manager = AutoStartManager(
            controller,
            bus or EventBus(),
            options=options,
            connection_handler=fake_handler,
            access_policy=policy or AllowAll(),
            requests=LineRequests(),
        )
        managers.append(manager)
        return manager, controller

    yield factory
    for manager in managers:
        manager.close()


class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.subscriptions = []

    def subscribe(self, event_type, callback):
        self.subscriptions.append(event_type)
        super().subscribe(event_type, callback)


# ----------------------------------------------------------------------
# Subscriptions
# ----------------------------------------------------------------------


class TestSubscriptions:
    def test_subscribes_to_lifecycle_events_and_close_unsubscribes(self, make_manager):
        bus = RecordingBus()
        manager, _ = make_manager(bus=bus)
        assert sorted(bus.subscriptions) == sorted(
            [Event.SERVER_STARTED, Event.SERVER_STOPPED, Event.LIFECYCLE_CHANGED]
        )
        manager.close()
        manager.close()  # idempotent
        assert bus.subscriber_count() == 0

    def test_server_stopped_event_arms_the_listener(self, make_manager, port):
        bus = EventBus()
        manager, _ = make_manager(bus=bus)
        bus.publish(Event.SERVER_STOPPED, {"state": LifecycleState.STOPPED})
        wait_until(lambda: manager.is_listening, message="listener did not bind")
        assert not can_bind(port)

    @pytest.mark.parametrize(
        ("event", "payload"),
        [
            (Event.SERVER_STARTED, {"state": "RUNNING"}),
            (Event.LIFECYCLE_CHANGED, {"state": LifecycleState.STARTING}),
        ],
    )
    def test_start_events_release_the_port_synchronously(
        self, make_manager, port, event, payload
    ):
        bus = EventBus()
        manager, _ = make_manager(bus=bus)
        assert manager.listen()
        wait_until(lambda: manager.is_listening)
        bus.publish(event, payload)  # returns only once the port is free
        assert not manager.is_listening
        assert can_bind(port)

    @pytest.mark.parametrize("state", [LifecycleState.STOPPING, LifecycleState.RUNNING])
    def test_other_lifecycle_changes_keep_listening(self, make_manager, state):
        bus = EventBus()
        manager, _ = make_manager(bus=bus)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        bus.publish(Event.LIFECYCLE_CHANGED, {"state": state})
        assert manager.is_listening


# ----------------------------------------------------------------------
# Arming
# ----------------------------------------------------------------------


class TestListen:
    def test_listen_is_idempotent(self, make_manager, port):
        manager, _ = make_manager()
        assert manager.listen()
        wait_until(lambda: manager.is_listening)
        first = manager.bound_address
        assert manager.listen()
        assert manager.bound_address == first
        assert talk(port, "STATUS").startswith("MOTD ")

    @pytest.mark.parametrize(
        "state",
        [LifecycleState.RUNNING, LifecycleState.STARTING, LifecycleState.STOPPING],
    )
    def test_does_not_listen_unless_stopped(self, make_manager, port, state):
        manager, _ = make_manager(controller=FakeController(port, state=state))
        assert manager.listen() is False
        assert not manager.is_armed  # no cycle was created at all
        assert not manager.is_listening
        assert can_bind(port)

    def test_without_controller_never_listens(self, make_manager, port):
        manager = AutoStartManager(None, EventBus(), options=FAST)
        try:
            assert manager.listen() is False
        finally:
            manager.close()

    def test_listen_never_blocks_the_caller_even_when_the_port_is_busy(
        self, make_manager, port
    ):
        blocker = socket.create_server(("127.0.0.1", port))
        try:
            manager, _ = make_manager()
            started = time.monotonic()
            assert manager.listen()
            assert time.monotonic() - started < 0.2
            wait_until(lambda: manager._cycle.bind_failures >= 1)
            assert not manager.is_listening
        finally:
            blocker.close()
        # The cycle keeps retrying and grabs the port once it is free.
        wait_until(lambda: manager.is_listening, timeout=3, message="no re-bind")

    def test_stop_listening_cancels_a_retrying_bind_quickly(self, make_manager, port):
        options = ListenerOptions(
            bind_attempts=1, retry_delay=5, slow_retry_delay=5, accept_timeout=0.05
        )
        blocker = socket.create_server(("127.0.0.1", port))
        try:
            manager, _ = make_manager(options=options)
            manager.listen()
            wait_until(
                lambda: manager._cycle.bind_failures >= 1
            )  # now waiting to retry
            started = time.monotonic()
            assert manager.stop_listening() is True
            assert time.monotonic() - started < 1.0
        finally:
            blocker.close()
        assert can_bind(port)  # stop_listening() returned: nothing holds the port

    def test_a_disarm_racing_with_listen_wins(self, make_manager, port):
        """STARTING committed (and handled) between listen()'s state check and
        the arming must not leave a listener bound during the launch."""
        holder = {}

        class RacingController(FakeController):
            @property
            def state(self):
                holder["manager"].stop_listening()  # the STARTING handler runs now
                return LifecycleState.STOPPED

            @state.setter
            def state(self, value):
                pass

        manager, _ = make_manager(controller=RacingController(port))
        holder["manager"] = manager
        assert manager.listen() is False
        assert not manager.is_armed
        assert not manager.is_listening
        assert can_bind(port)


# ----------------------------------------------------------------------
# Waking
# ----------------------------------------------------------------------


class TestWake:
    def test_status_pings_do_not_wake(self, make_manager, port):
        manager, controller = make_manager()
        manager.listen()
        wait_until(lambda: manager.is_listening)
        for _ in range(3):
            assert talk(port, "STATUS").startswith("MOTD ")
        wait_until(lambda: manager.active_connections == 0)  # handlers finished
        assert controller.start_calls == 0
        assert manager.is_listening

    def test_login_wakes_once_after_releasing_the_port(
        self, make_manager, port, mock_settings
    ):
        manager, controller = make_manager()
        manager.listen()
        wait_until(lambda: manager.is_listening)
        reply = talk(port, "LOGIN Steve")
        assert reply.startswith("KICK ")
        assert controller.started.wait(3)
        assert controller.start_calls == 1
        assert controller.port_free_at_start == [True]
        assert not manager.is_listening
        manager._last_cycle.thread.join(3)  # the cycle is over: no second start
        assert not manager._last_cycle.thread.is_alive()
        assert controller.start_calls == 1

    def test_concurrent_logins_start_the_server_exactly_once(self, make_manager, port):
        manager, controller = make_manager()
        manager.listen()
        wait_until(lambda: manager.is_listening)
        # All connected (queued or accepted) before any login is sent: the ones
        # still in the backlog at wake time must be served, not reset.
        clients = [
            socket.create_connection(("127.0.0.1", port), timeout=3) for _ in range(3)
        ]
        for sock, name in zip(clients, ("Alex", "Steve", "Notch"), strict=True):
            sock.sendall(f"LOGIN {name}\n".encode())
        replies = [sock.recv(256) for sock in clients]
        for sock in clients:
            sock.close()
        assert all(r.startswith(b"KICK ") for r in replies)
        assert controller.started.wait(3)
        wait_until(lambda: manager.active_connections == 0)
        manager._last_cycle.thread.join(3)
        assert controller.start_calls == 1

    def test_denied_login_does_not_wake(self, make_manager, port):
        manager, controller = make_manager(policy=DenyAll())
        manager.listen()
        wait_until(lambda: manager.is_listening)
        assert (
            talk(port, "LOGIN Griefer")
            == "DENY You are not white-listed on this server!"
        )
        wait_until(lambda: manager.active_connections == 0)
        assert controller.start_calls == 0
        assert manager.is_listening

    def test_failing_access_check_fails_open(self, make_manager, port):
        class Broken:
            def check(self, name, ip):
                raise RuntimeError("boom")

        manager, _ = make_manager(policy=Broken())
        assert manager.is_allowed("Steve", "127.0.0.1") == (True, None)

    def test_rejected_start_sleeps_on_the_port_again(self, make_manager, port):
        manager, controller = make_manager(
            controller=FakeController(port, accept=False)
        )
        manager.listen()
        wait_until(lambda: manager.is_listening)
        talk(port, "LOGIN Steve")
        assert controller.started.wait(3)
        wait_until(lambda: manager.is_listening, timeout=3, message="not re-armed")
        controller.started.clear()
        talk(port, "LOGIN Steve")
        assert controller.started.wait(3)
        assert controller.start_calls == 2  # once per listen cycle

    def test_server_stopped_rearms_after_a_wake(self, make_manager, port):
        bus = EventBus()
        manager, controller = make_manager(bus=bus)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        talk(port, "LOGIN Steve")
        assert controller.started.wait(3)
        bus.publish(Event.LIFECYCLE_CHANGED, {"state": LifecycleState.STARTING})
        controller.state = LifecycleState.RUNNING
        bus.publish(Event.SERVER_STARTED, {"state": "RUNNING"})
        assert not manager.is_listening

        controller.state = LifecycleState.STOPPED
        bus.publish(Event.SERVER_STOPPED, {"state": "STOPPED"})
        wait_until(lambda: manager.is_listening, message="not re-armed after STOPPED")
        assert talk(port, "STATUS").startswith("MOTD ")

    def test_close_releases_the_port_and_ignores_later_events(self, make_manager, port):
        bus = EventBus()
        manager, _ = make_manager(bus=bus)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        manager.close()
        assert can_bind(port)
        bus.publish(Event.SERVER_STOPPED, {})
        assert manager.listen() is False
        assert not manager.is_armed
        assert can_bind(port)

    def test_sleeping_config_comes_from_settings_and_properties(
        self, make_manager, server_dir, mock_settings
    ):
        (server_dir / "server.properties").write_text(
            "max-players=42\n", encoding="utf-8"
        )
        mock_settings.sleepingMotd = "zzz"
        mock_settings.startingMessage = "wait"
        manager, _ = make_manager()
        config = manager.sleeping_config()
        assert (config.motd, config.kick_message, config.max_players) == (
            "zzz",
            "wait",
            42,
        )
        assert config.protocol_version is None
        assert config.favicon is None
        assert config.version_name == "Sleeping"


# ----------------------------------------------------------------------
# Socket helpers
# ----------------------------------------------------------------------


class TestSocketHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("::ffff:192.168.1.5", "192.168.1.5"),
            ("10.0.0.1", "10.0.0.1"),
            ("::1", "::1"),
            ("fe80::1%eth0", "fe80::1"),
            ("not-an-ip", "not-an-ip"),
        ],
    )
    def test_normalize_client_ip(self, raw, expected):
        assert normalize_client_ip(raw) == expected

    def test_all_interfaces_binds_dual_stack_when_available(self):
        family, sockaddr, dualstack = bind_address("", 25565)
        if socket.has_dualstack_ipv6():
            assert (family, sockaddr[0], dualstack) == (socket.AF_INET6, "::", True)
        else:
            assert (family, sockaddr, dualstack) == (
                socket.AF_INET,
                ("0.0.0.0", 25565),
                False,
            )

    def test_specific_host_binds_that_address(self):
        family, sockaddr, dualstack = bind_address("127.0.0.1", 25565)
        assert (family, sockaddr, dualstack) == (
            socket.AF_INET,
            ("127.0.0.1", 25565),
            False,
        )

    def test_all_interfaces_listener_accepts_ipv4(
        self, make_manager, port, mock_settings
    ):
        mock_settings.listenerBindHost = ""
        manager, _ = make_manager()
        manager.listen()
        wait_until(lambda: manager.is_listening)
        assert talk(port, "STATUS").startswith("MOTD ")


# ----------------------------------------------------------------------
# Access policy
# ----------------------------------------------------------------------


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


class TestAccessPolicy:
    def test_everyone_allowed_by_default(self, server_dir):
        assert AccessPolicy().check("Steve", "1.2.3.4") == (True, None)

    def test_banned_name_is_refused_case_insensitively(self, server_dir):
        _write_json(
            server_dir / "banned-players.json",
            [{"uuid": "u", "name": "Griefer", "expires": "forever", "reason": "Grief"}],
        )
        allowed, message = AccessPolicy().check("griefer", "1.2.3.4")
        assert not allowed
        assert message == "You are banned from this server.\nReason: Grief"

    def test_expired_ban_does_not_refuse(self, server_dir):
        _write_json(
            server_dir / "banned-players.json",
            [{"name": "Griefer", "expires": "2001-01-01 00:00:00 +0000"}],
        )
        assert AccessPolicy().check("Griefer", "1.2.3.4") == (True, None)

    def test_future_temporary_ban_refuses(self, server_dir):
        _write_json(
            server_dir / "banned-players.json",
            [{"name": "Griefer", "expires": "2999-01-01 00:00:00 +0000"}],
        )
        assert AccessPolicy().check("Griefer", "1.2.3.4")[0] is False

    def test_banned_ip_matches_ipv4_mapped_clients(self, server_dir):
        _write_json(
            server_dir / "banned-ips.json", [{"ip": "10.0.0.7", "expires": "forever"}]
        )
        allowed, message = AccessPolicy().check("Steve", "::ffff:10.0.0.7")
        assert not allowed
        assert message.startswith("Your IP address is banned")

    def test_whitelist_allows_listed_names_and_ops_only(self, server_dir):
        (server_dir / "server.properties").write_text(
            "white-list=true\n", encoding="utf-8"
        )
        _write_json(server_dir / "whitelist.json", [{"uuid": "a", "name": "Alex"}])
        _write_json(
            server_dir / "ops.json", [{"uuid": "b", "name": "Admin", "level": 4}]
        )
        policy = AccessPolicy()
        assert policy.check("alex", "1.2.3.4") == (True, None)
        assert policy.check("ADMIN", "1.2.3.4") == (True, None)
        assert policy.check("Stranger", "1.2.3.4") == (
            False,
            "You are not white-listed on this server!",
        )

    def test_whitelist_file_ignored_when_whitelist_is_off(self, server_dir):
        (server_dir / "server.properties").write_text(
            "white-list=false\n", encoding="utf-8"
        )
        _write_json(server_dir / "whitelist.json", [{"name": "Alex"}])
        assert AccessPolicy().check("Stranger", "1.2.3.4") == (True, None)

    def test_respect_whitelist_off_allows_everyone(self, server_dir, mock_settings):
        mock_settings.respectWhitelist = False
        _write_json(server_dir / "banned-players.json", [{"name": "Griefer"}])
        assert AccessPolicy().check("Griefer", "1.2.3.4") == (True, None)


# ----------------------------------------------------------------------
# Admission control: idle sockets must not lock legitimate players out
# ----------------------------------------------------------------------


def idle_socket(port, source="127.0.0.1"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((source, 0))
    except OSError:
        sock.close()
        pytest.skip(f"cannot use {source} as a source address here")
    sock.settimeout(3)
    sock.connect(("127.0.0.1", port))
    return sock


def talk_from(port, line, source):
    sock = idle_socket(port, source)
    with sock:
        sock.sendall(line.encode() + b"\n")
        return sock.recv(256).decode().strip()


SLOTS = ListenerOptions(
    accept_timeout=0.05,
    connection_timeout=2.0,
    request_timeout=5.0,  # longer than the tests: idle sockets stay held
    release_timeout=3.0,
)


class TestAdmission:
    def test_idle_sockets_from_one_host_do_not_block_a_join(self, make_manager, port):
        manager, controller = make_manager(options=SLOTS)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        idle = [idle_socket(port) for _ in range(20)]  # old global cap was 16
        try:
            wait_until(lambda: manager.active_connections == 4)  # per-client cap
            assert talk_from(port, "LOGIN Steve", "127.0.0.2").startswith("KICK ")
            assert controller.started.wait(3)
        finally:
            for sock in idle:
                sock.close()

    def test_idle_sockets_from_many_hosts_do_not_block_a_join(self, make_manager, port):
        manager, controller = make_manager(options=SLOTS)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        idle = [idle_socket(port, f"127.0.0.{10 + i}") for i in range(20)]
        try:
            wait_until(lambda: manager.active_connections == 20)
            assert talk(port, "LOGIN Steve").startswith("KICK ")
            assert controller.started.wait(3)
        finally:
            for sock in idle:
                sock.close()

    def test_silent_sockets_are_dropped_after_the_request_timeout(
        self, make_manager, port
    ):
        options = ListenerOptions(accept_timeout=0.05, request_timeout=0.2)
        manager, controller = make_manager(options=options)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        idle = [idle_socket(port) for _ in range(4)]  # fills this host's slots
        try:
            wait_until(lambda: manager.active_connections == 4)
            for sock in idle:
                assert sock.recv(16) == b""  # closed by the listener
            wait_until(lambda: manager.active_connections == 0)
            assert talk(port, "LOGIN Steve").startswith("KICK ")
            assert controller.started.wait(3)
        finally:
            for sock in idle:
                sock.close()

    def test_a_trickled_request_is_dropped_at_the_request_deadline(
        self, make_manager, port
    ):
        options = ListenerOptions(accept_timeout=0.05, request_timeout=0.3)
        manager, controller = make_manager(options=options)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        with idle_socket(port) as sock:
            sock.sendall(b"LOGIN Ste")  # never finished
            started = time.monotonic()
            assert sock.recv(16) == b""
            assert time.monotonic() - started < 1.5
        assert not controller.started.is_set()

    def test_a_join_evicts_the_oldest_idle_socket_when_full(self, make_manager, port):
        options = ListenerOptions(
            accept_timeout=0.05,
            request_timeout=5.0,
            eviction_grace=0.05,
            max_connections=4,
            release_timeout=3.0,
        )
        manager, controller = make_manager(options=options)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        idle = [idle_socket(port, f"127.0.0.{10 + i}") for i in range(4)]
        try:
            wait_until(lambda: manager.active_connections == 4)
            time.sleep(0.1)  # older than the eviction grace
            assert talk_from(port, "LOGIN Steve", "127.0.0.2").startswith("KICK ")
            assert controller.started.wait(3)
            assert idle[0].recv(16) == b""  # the oldest idle socket was evicted
        finally:
            for sock in idle:
                sock.close()

    def test_a_join_evicts_an_idle_socket_of_its_own_address(self, make_manager, port):
        options = ListenerOptions(
            accept_timeout=0.05,
            request_timeout=5.0,
            eviction_grace=0.05,
            max_connections_per_client=2,
            release_timeout=3.0,
        )
        manager, controller = make_manager(options=options)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        idle = [idle_socket(port) for _ in range(2)]
        try:
            wait_until(lambda: manager.active_connections == 2)
            time.sleep(0.1)
            assert talk(port, "LOGIN Steve").startswith("KICK ")
            assert controller.started.wait(3)
        finally:
            for sock in idle:
                sock.close()

    def test_when_every_slot_holds_a_login_a_join_is_told_to_retry(self, port):
        gate = threading.Event()

        class DenySlow:
            def check(self, name, ip):
                return name != "Slow", "go away"

        def handler(conn, ip, config, is_allowed, timeout=5.0):
            outcome = fake_handler(conn, ip, config, is_allowed, timeout)
            if outcome.player_name == "Slow":
                gate.wait(3)  # a login holding the only slot
            return outcome

        controller = FakeController(port)
        manager = AutoStartManager(
            controller,
            EventBus(),
            options=ListenerOptions(accept_timeout=0.05, max_connections=1),
            connection_handler=handler,
            access_policy=DenySlow(),
            requests=LineRequests(),
        )
        try:
            manager.listen()
            wait_until(lambda: manager.is_listening)
            assert talk(port, "LOGIN Slow") == "DENY go away"
            wait_until(lambda: manager.active_connections == 1)
            assert talk(port, "LOGIN Steve") == f"DENY {BUSY_MESSAGE}"
            assert talk(port, "STATUS").startswith("MOTD ")  # pings still answered
            assert not controller.started.is_set()
        finally:
            gate.set()
            manager.close()

    def test_a_ping_without_the_ping_packet_is_dropped_quickly(
        self, make_manager, port
    ):
        options = ListenerOptions(accept_timeout=0.05, status_timeout=0.3)
        manager, _ = make_manager(options=options)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        with idle_socket(port) as sock:
            sock.sendall(b"STATUS\n")
            started = time.monotonic()
            reply = b""
            while not reply.endswith(b"\n"):
                reply += sock.recv(256)
            assert reply.startswith(b"MOTD ")
            assert sock.recv(16) == b""  # never sent the PING: dropped
            assert time.monotonic() - started < 1.5

    def test_stalled_pings_and_idle_sockets_cannot_block_a_wake(
        self, make_manager, port
    ):
        """The verifier's sustained attack, scaled down: lanes from 6
        addresses (4 each, so more than the slots) that send a ping and never
        the PING packet, or nothing at all, and reconnect when dropped."""
        options = ListenerOptions(
            accept_timeout=0.02,
            request_timeout=5.0,
            status_timeout=5.0,  # eviction alone has to make room
            eviction_grace=0.05,
            max_connections=8,
            release_timeout=3.0,
        )
        manager, controller = make_manager(options=options)
        manager.listen()
        wait_until(lambda: manager.is_listening)
        stop = threading.Event()

        def lane(source, stalled_ping):
            while not stop.is_set():
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.bind((source, 0))
                    sock.settimeout(0.2)
                    sock.connect(("127.0.0.1", port))
                    if stalled_ping:
                        sock.sendall(b"STATUS\n")
                    while not stop.is_set():
                        try:
                            if sock.recv(256) == b"":
                                break  # dropped by the listener: reconnect
                        except TimeoutError:
                            continue
                except OSError:
                    pass
                finally:
                    sock.close()

        lanes = [
            threading.Thread(
                target=lane, args=(f"127.0.3.{n}", n % 2 == 0), daemon=True
            )
            for n in range(1, 7)
            for _ in range(4)
        ]
        for thread in lanes:
            thread.start()
        try:
            wait_until(lambda: manager.active_connections == 8)
            time.sleep(0.2)
            for _ in range(3):
                if talk_from(port, "LOGIN Steve", "127.0.0.2").startswith("KICK "):
                    break
            assert controller.started.wait(3)
        finally:
            stop.set()
            for thread in lanes:
                thread.join(2)

    def test_connection_slots(self):
        class Conn:
            shut_down = closed = False

            def shutdown(self, _how):
                self.shut_down = True

            def close(self):
                self.closed = True

        now = [0.0]
        slots = ConnectionSlots(total=3, per_client=2, grace=1.0, clock=lambda: now[0])
        conns = [Conn() for _ in range(5)]
        a1, a2 = slots.admit(conns[0], "a"), slots.admit(conns[1], "a")
        assert a1 and a2
        assert slots.admit(conns[2], "a") is None  # per-client cap; too young to evict
        now[0] = 2.0
        a3 = slots.admit(conns[2], "a")  # evicts the oldest pending one of "a"
        assert a3 and a1.evicted and conns[0].shut_down
        assert not slots.promote(a1, "login")
        assert slots.promote(a2, "login") and slots.promote(a3, "login")
        b1 = slots.admit(conns[3], "b")
        assert slots.promote(b1, "status")
        assert slots.admit(conns[4], "c") is None  # the ping is too young
        now[0] = 4.0
        c1 = slots.admit(conns[4], "c")  # evicts the old ping, never a login
        assert c1 and b1.evicted and not a2.evicted and not a3.evicted
        assert slots.admit(conns[0], "d") is None  # pending c1 is too young
        d1 = slots.admit(conns[0], "d", phase="login")  # a login evicts it anyway
        assert d1 and c1.evicted
        assert slots.admit(conns[1], "e", phase="login") is None  # all logins
        assert slots.in_use == 3

    @pytest.mark.parametrize(
        ("ip", "key"),
        [
            ("10.0.0.7", "10.0.0.7"),
            ("2001:db8:1:2:3:4:5:6", "2001:db8:1::/56"),
            ("2001:db8:1:2::ff", "2001:db8:1::/56"),  # same home, other /64
            ("2001:db8:1:ff00::1", "2001:db8:1:ff00::/56"),
            ("garbage", "garbage"),
        ],
    )
    def test_client_key(self, ip, key):
        assert client_key(ip) == key


# ----------------------------------------------------------------------
# accept() errors keep the port bound
# ----------------------------------------------------------------------


class TestAcceptErrors:
    @pytest.mark.parametrize(
        ("error", "action"),
        [
            (ConnectionAbortedError(errno.ECONNABORTED, "aborted"), "retry"),
            (ConnectionResetError(errno.ECONNRESET, "reset"), "retry"),
            (OSError(errno.EPROTO, "protocol"), "retry"),
            (OSError(errno.EMFILE, "too many open files"), "backoff"),
            (OSError(errno.ENFILE, "file table overflow"), "backoff"),
            (OSError(errno.EBADF, "bad file descriptor"), "rebind"),
            (OSError(errno.EINVAL, "invalid"), "rebind"),
        ],
    )
    def test_classification(self, error, action):
        assert accept_error_action(error) == action

    @staticmethod
    def serve_with(manager, errors):
        """Run one cycle's accept loop over a fake listening socket that raises
        *errors* in turn, then cancels the cycle. Returns the errors left."""
        cycle = _ListenCycle(manager, 99)
        pending = list(errors)

        class FakeListener:
            def settimeout(self, _timeout):
                pass

            def accept(self):
                if pending:
                    raise pending.pop(0)
                cycle.cancel()
                raise TimeoutError

        cycle._serve(FakeListener())
        return pending

    def test_transient_and_resource_errors_keep_accepting(self, make_manager):
        manager, _ = make_manager()
        left = self.serve_with(
            manager,
            [
                ConnectionAbortedError(errno.ECONNABORTED, "aborted"),
                OSError(errno.EMFILE, "too many open files"),
                ConnectionResetError(errno.ECONNRESET, "reset"),
            ],
        )
        assert left == []  # survived all three, then the cancel ended it

    def test_a_broken_listening_socket_is_rebound(self, make_manager):
        manager, _ = make_manager()
        left = self.serve_with(
            manager, [OSError(errno.EBADF, "bad file descriptor"), OSError(1, "unused")]
        )
        assert len(left) == 1  # returned after the first error: _run re-binds


# ----------------------------------------------------------------------
# status() for the dashboard
# ----------------------------------------------------------------------


class TestStatus:
    def test_reports_a_busy_port_and_recovers(self, make_manager, port):
        manager, _ = make_manager()
        idle = {"armed": False, "listening": False, "error": None}
        assert manager.status() == idle
        hog = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            hog.bind(("127.0.0.1", port))
            hog.listen(1)
            manager.listen()
            wait_until(lambda: manager.status()["error"] is not None)
            status = manager.status()
            assert status["armed"] and not status["listening"]
            assert str(port) in status["error"]
        finally:
            hog.close()
        wait_until(
            lambda: (
                manager.status() == {"armed": True, "listening": True, "error": None}
            )
        )
        manager.stop_listening()
        assert manager.status() == idle


# ----------------------------------------------------------------------
# Minecraft request recognition (the real protocol)
# ----------------------------------------------------------------------


def handshake(next_state, protocol=763):
    from src.minecraft.protocol import encode_string, encode_varint, frame_packet

    body = (
        encode_varint(protocol)
        + encode_string("play.example\0FML2\0")
        + (25565).to_bytes(2, "big")
        + encode_varint(next_state)
    )
    return frame_packet(0x00, body)


class TestMinecraftRequests:
    def test_classify(self):
        from src.auto_start import MinecraftRequests
        from src.minecraft.protocol import encode_string, frame_packet

        classify = MinecraftRequests.classify
        status = handshake(1) + frame_packet(0x00)
        login = handshake(2) + frame_packet(0x00, encode_string("Steve"))
        assert classify(b"") is None
        assert classify(status[:-1]) is None  # incomplete
        assert classify(status) == "status"
        assert classify(login) == "login"
        assert classify(b"\xfe\x01") == "legacy"
        with pytest.raises(ValueError):
            classify(b"\xff\xff\xff\xff")

    def test_busy_replies(self, mock_settings):
        from src.auto_start import MinecraftRequests
        from src.minecraft.protocol import PacketReader, frame_packet
        from src.minecraft.sleeping import SleepingServerConfig

        config = SleepingServerConfig("zzz", "Sleeping", None, 20, "data:x", "starting")

        def payload(frame):
            reader = PacketReader(frame[1:] if frame[0] < 0x80 else frame[2:])
            assert reader.read_varint() == 0x00
            return json.loads(reader.read_string(None))

        login = MinecraftRequests.busy_reply("login", b"", config)
        assert payload(login) == {"text": BUSY_MESSAGE}
        status = MinecraftRequests.busy_reply(
            "status", handshake(1, protocol=47) + frame_packet(0x00), config
        )
        reply = payload(status)
        assert reply["version"]["protocol"] == 47
        assert reply["description"] == {"text": "zzz"} and "favicon" not in reply
        assert MinecraftRequests.busy_reply("legacy", b"\xfe", config) is None
