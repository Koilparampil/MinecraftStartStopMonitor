"""Tests for src.auth: user model, credential check, redirect safety, lockout."""

import threading

import os

import pytest

from src.auth import (
    LoginAttemptTracker,
    User,
    credential_epoch,
    is_safe_redirect,
    safe_next_url,
    split_user_id,
    verify_credentials,
)


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestUser:
    def test_identity(self):
        user = User("admin")
        assert user.id == "admin"
        assert user.username == "admin"
        assert user.get_id() == "admin"

    def test_flask_login_flags(self):
        user = User("admin")
        assert user.is_authenticated is True
        assert user.is_active is True
        assert user.is_anonymous is False


class TestCredentialEpoch:
    def test_user_id_carries_the_epoch(self):
        user = User("admin", "abc123")
        assert user.username == "admin"
        assert user.epoch == "abc123"
        assert user.get_id() == "admin|abc123"
        assert split_user_id(user.get_id()) == ("admin", "abc123")

    def test_username_with_separator_round_trips(self):
        user = User("we|ird", "ff00")
        assert split_user_id(user.get_id()) == ("we|ird", "ff00")

    def test_legacy_id_has_no_epoch(self):
        assert split_user_id("admin") == ("admin", "")

    def test_epoch_changes_with_any_credential_or_the_secret(self):
        base = credential_epoch("admin", "pw", "secret")
        assert base == credential_epoch("admin", "pw", "secret")
        assert len(base) == 32
        assert base != credential_epoch("admin", "pw2", "secret")
        assert base != credential_epoch("admin2", "pw", "secret")
        assert base != credential_epoch("admin", "pw", "other-secret")
        assert base == credential_epoch("admin", "pw", b"secret")

    def test_epoch_does_not_reveal_the_password(self):
        assert "pw-secret" not in credential_epoch("admin", "pw-secret", "k")


class TestVerifyCredentials:
    @pytest.mark.parametrize(
        "username,password,expected",
        [
            ("admin", "s3cret", True),
            ("admin", "wrong", False),
            ("Admin", "s3cret", False),
            ("other", "s3cret", False),
            ("", "", False),
            (None, None, False),
            ("admin", "s3cret ", False),
        ],
    )
    def test_matches_only_exact_credentials(self, username, password, expected):
        assert verify_credentials(username, password, "admin", "s3cret") is expected

    def test_non_ascii_credentials(self):
        assert verify_credentials("jörg", "pässwörd✓", "jörg", "pässwörd✓") is True
        assert verify_credentials("jörg", "passwörd✓", "jörg", "pässwörd✓") is False

    def test_numeric_yaml_password_is_compared_as_text(self):
        # `password: 12345` in YAML loads as an int.
        assert verify_credentials("admin", "12345", "admin", 12345) is True

    @pytest.mark.parametrize(
        "expected_user,expected_pass",
        [("admin", None), ("admin", ""), (None, "x"), ("", "x")],
    )
    def test_unconfigured_credentials_never_authenticate(
        self, expected_user, expected_pass
    ):
        assert verify_credentials("admin", "", expected_user, expected_pass) is False
        assert verify_credentials("", "x", expected_user, expected_pass) is False

    def test_uses_constant_time_comparison(self, monkeypatch):
        import src.auth as auth

        calls = []
        real = auth.hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(auth.hmac, "compare_digest", spy)
        assert verify_credentials("nope", "nope", "admin", "s3cret") is False
        # Both fields are always compared (no short-circuit on the username).
        assert len(calls) == 2


class TestSafeRedirect:
    @pytest.mark.parametrize(
        "target",
        ["/", "/dashboard", "/x?y=1&z=%2F", "/a/b#frag", "/%2F%2Fevil.com"],
    )
    def test_relative_paths_are_safe(self, target):
        assert is_safe_redirect(target) is True
        assert safe_next_url(target) == target

    @pytest.mark.parametrize(
        "target",
        [
            None,
            "",
            "http://evil.com",
            "https://evil.com/",
            "HTTPS://evil.com",
            "//evil.com",
            "///evil.com",
            "/\\evil.com",
            "\\\\evil.com",
            "\\/evil.com",
            "/\t/evil.com",
            "/\n/evil.com",
            "/\r\n/evil.com",
            "javascript:alert(1)",
            "evil.com",
            " /dashboard",
            "/x\x00",
            "/x\x7f",
            ["/"],
        ],
    )
    def test_everything_else_falls_back_to_the_default(self, target):
        assert is_safe_redirect(target) is False
        assert safe_next_url(target) == "/"
        assert safe_next_url(target, default="/home") == "/home"


class TestLoginAttemptTracker:
    def make(self, max_attempts=3, lockout=300):
        clock = FakeClock()
        return LoginAttemptTracker(max_attempts, lockout, clock=clock), clock

    def test_fresh_address_is_not_locked(self):
        tracker, _ = self.make()
        assert tracker.is_locked_out("10.0.0.1") is False
        assert tracker.get_remaining_attempts("10.0.0.1") == 3
        assert tracker.get_lockout_time_remaining("10.0.0.1") is None

    def test_locks_after_max_attempts(self):
        tracker, _ = self.make()
        tracker.record_failed_attempt("10.0.0.1")
        tracker.record_failed_attempt("10.0.0.1")
        assert tracker.is_locked_out("10.0.0.1") is False
        assert tracker.get_remaining_attempts("10.0.0.1") == 1

        tracker.record_failed_attempt("10.0.0.1")
        assert tracker.is_locked_out("10.0.0.1") is True
        assert tracker.get_remaining_attempts("10.0.0.1") == 0
        assert tracker.get_lockout_time_remaining("10.0.0.1") == 300

    def test_addresses_are_tracked_separately(self):
        tracker, _ = self.make(max_attempts=1)
        tracker.record_failed_attempt("10.0.0.1")
        assert tracker.is_locked_out("10.0.0.1") is True
        assert tracker.is_locked_out("10.0.0.2") is False

    def test_lockout_runs_from_the_moment_it_started(self):
        tracker, clock = self.make()
        tracker.record_failed_attempt("ip")
        clock.advance(250)
        tracker.record_failed_attempt("ip")
        tracker.record_failed_attempt("ip")  # locked at t=250
        clock.advance(200)  # 450s after the first failure
        assert tracker.is_locked_out("ip") is True
        assert tracker.get_lockout_time_remaining("ip") == 100

    def test_lockout_expires(self):
        tracker, clock = self.make()
        for _ in range(3):
            tracker.record_failed_attempt("ip")
        clock.advance(299.5)
        assert tracker.is_locked_out("ip") is True
        assert tracker.get_lockout_time_remaining("ip") == 1  # rounded up
        clock.advance(0.5)
        assert tracker.is_locked_out("ip") is False
        assert tracker.get_remaining_attempts("ip") == 3

    def test_failures_outside_the_window_are_forgotten(self):
        tracker, clock = self.make()
        tracker.record_failed_attempt("ip")
        tracker.record_failed_attempt("ip")
        clock.advance(301)
        tracker.record_failed_attempt("ip")
        assert tracker.is_locked_out("ip") is False
        assert tracker.get_remaining_attempts("ip") == 2

    def test_successful_login_clears_failures_and_lockout(self):
        tracker, _ = self.make(max_attempts=2)
        tracker.record_failed_attempt("ip")
        tracker.record_failed_attempt("ip")
        assert tracker.is_locked_out("ip") is True
        tracker.record_successful_login("ip")
        assert tracker.is_locked_out("ip") is False
        assert tracker.get_remaining_attempts("ip") == 2

    def test_concurrent_failures_are_all_counted(self):
        tracker, _ = self.make(max_attempts=1000)
        threads = [
            threading.Thread(
                target=lambda: [tracker.record_failed_attempt("ip") for _ in range(50)]
            )
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert tracker.get_remaining_attempts("ip") == 1000 - 400

    def test_expired_entries_are_pruned(self):
        tracker, clock = self.make(lockout=10)
        for index in range(1100):
            tracker.record_failed_attempt(f"10.0.{index // 256}.{index % 256}")
        clock.advance(11)
        tracker.record_failed_attempt("fresh")
        assert len(tracker._failures) == 1


class WallClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class TestSessionStore:
    def make(self, tmp_path=None, clock=None):
        from src.auth import SessionStore

        path = str(tmp_path / "web-sessions.json") if tmp_path else None
        return SessionStore(path, clock=clock or WallClock())

    def test_sessions_open_check_and_close(self):
        store = self.make()
        sid = store.open_session("epoch", idle=60, lifetime=3600, token_id="t1")
        assert store.check_session(sid, "epoch")
        assert not store.check_session(sid, "other-epoch")  # credentials changed
        assert not store.check_session(sid, "epoch")  # ...and it is gone for good
        sid = store.open_session("epoch", idle=60, lifetime=3600, token_id="t1")
        assert store.close_session(sid) == "t1"
        assert not store.check_session(sid, "epoch")
        assert store.close_session(sid) is None
        assert not store.check_session(None, "epoch")

    def test_idle_timeout_slides_but_lifetime_is_absolute(self):
        clock = WallClock()
        store = self.make(clock=clock)
        sid = store.open_session("e", idle=60, lifetime=150)
        for _ in range(2):
            clock.now += 50
            assert store.check_session(sid, "e")  # activity keeps it alive
        clock.now += 51  # 151 s after opening
        assert not store.check_session(sid, "e")
        idle = store.open_session("e", idle=60, lifetime=3600)
        clock.now += 61
        assert not store.check_session(idle, "e")

    def test_check_without_touch_does_not_extend(self):
        clock = WallClock()
        store = self.make(clock=clock)
        sid = store.open_session("e", idle=60, lifetime=3600)
        clock.now += 40
        assert store.check_session(sid, "e", touch=False)
        clock.now += 30
        assert not store.check_session(sid, "e")

    def test_tokens_are_random_hashed_and_persisted(self, tmp_path):
        store = self.make(tmp_path)
        first_id, first = store.issue_token("e", lifetime=600)
        second_id, second = store.issue_token("e", lifetime=600)
        assert first != second and first_id != second_id
        assert store.redeem_token(first, "e") == first_id
        stored = (tmp_path / "web-sessions.json").read_text()
        assert first.split(".", 1)[1] not in stored  # only a hash is kept
        restarted = self.make(tmp_path)  # survives a monitor restart
        assert restarted.redeem_token(second, "e") == second_id

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_token_file_is_private(self, tmp_path):
        store = self.make(tmp_path)
        store.issue_token("e", lifetime=600)
        assert (tmp_path / "web-sessions.json").stat().st_mode & 0o077 == 0

    def test_bad_expired_and_revoked_tokens(self, tmp_path):
        clock = WallClock()
        store = self.make(tmp_path, clock)
        token_id, token = store.issue_token("e", lifetime=600)
        assert store.redeem_token(token_id + ".wrong-secret", "e") is None
        assert store.redeem_token("garbage", "e") is None
        assert store.redeem_token(None, "e") is None
        assert store.redeem_token(token, "e") == token_id  # wrong guesses don't burn it
        assert store.redeem_token(token, "new-epoch") is None  # password changed
        assert store.redeem_token(token, "e") is None
        token_id, token = store.issue_token("e", lifetime=600)
        store.revoke_token(token_id)
        assert store.redeem_token(token, "e") is None
        token_id, token = store.issue_token("e", lifetime=600)
        clock.now += 601
        assert store.redeem_token(token, "e") is None
        assert store.token_count() == 0

    def test_close_all_ends_sessions_and_tokens(self, tmp_path):
        store = self.make(tmp_path)
        sid = store.open_session("e", idle=60, lifetime=600)
        _, token = store.issue_token("e", lifetime=600)
        store.close_all()
        assert not store.check_session(sid, "e")
        assert store.redeem_token(token, "e") is None
        assert self.make(tmp_path).token_count() == 0

    def test_corrupt_store_is_ignored(self, tmp_path):
        (tmp_path / "web-sessions.json").write_text("{not json")
        store = self.make(tmp_path)
        assert store.token_count() == 0
        _, token = store.issue_token("e", lifetime=600)
        assert self.make(tmp_path).redeem_token(token, "e")


class TestGlobalBackstop:
    def make(self, threshold=3):
        clock = WallClock()
        tracker = LoginAttemptTracker(max_attempts=5, lockout_duration=300, clock=clock)
        tracker.global_threshold = threshold
        return tracker, clock

    def test_failures_from_many_addresses_slow_everyone_down(self):
        tracker, clock = self.make()
        for n in range(2):
            tracker.record_failed_attempt(f"10.0.0.{n}")
        assert tracker.global_wait("10.9.9.9") == 0
        tracker.record_failed_attempt("10.0.0.2")  # threshold reached
        assert tracker.global_wait("10.9.9.9") == 1
        tracker.record_failed_attempt("10.0.0.3")
        assert tracker.global_wait("10.9.9.9") == 2  # the delay doubles
        clock.now += 2
        assert tracker.global_wait("10.9.9.9") == 0

    def test_delay_is_capped_and_window_expires(self):
        tracker, clock = self.make()
        for n in range(40):
            tracker.record_failed_attempt(f"10.0.{n}.1")
        assert tracker.global_wait("x") == tracker.global_max_delay
        clock.now += 301
        tracker.record_failed_attempt("10.1.1.1")  # old failures left the window
        clock.now += 61
        assert tracker.global_wait("x") == 0

    def test_known_good_addresses_are_exempt(self):
        tracker, _ = self.make()
        tracker.record_successful_login("192.0.2.1")
        for n in range(10):
            tracker.record_failed_attempt(f"10.0.0.{n}")
        assert tracker.global_wait("192.0.2.1") == 0
        assert tracker.global_wait("10.0.0.99") > 0
