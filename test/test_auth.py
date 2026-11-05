"""Tests for src.auth: user model, credential check, redirect safety, lockout."""

import threading

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
