"""Deterministic tests for the pure auto-stop policy."""

from dataclasses import replace

import pytest

from src.lifecycle.auto_stop import (
    AutoStopConfig,
    AutoStopDecision,
    AutoStopState,
    cancelled,
    on_expiry,
    on_roster,
    on_unavailable,
)

CONFIG = AutoStopConfig(enabled=True, delay=60, startup_guard=30)


def armed_state(now=131.0, generation=1):
    state, decision = on_roster(
        AutoStopState(),
        config=CONFIG,
        now=now,
        generation=generation,
        player_count=0,
        sample_seq=1,
        started_at=100.0,
    )
    assert decision == AutoStopDecision.NONE
    assert state.deadline == now + 60
    return state


def expire(state, *, generation=1, seq=5, config=CONFIG):
    """The deadline expires while poll ``seq`` is the newest one issued."""
    return on_expiry(state, config=config, generation=generation, poll_seq=seq)


def sample(state, *, now, count, seq, generation=1):
    return on_roster(
        state,
        config=CONFIG,
        now=now,
        generation=generation,
        player_count=count,
        sample_seq=seq,
        started_at=100.0,
    )


class TestArming:
    def test_startup_guard_cancels_without_arming(self):
        state, decision = sample(AutoStopState(), now=110.0, count=0, seq=1)

        assert decision == AutoStopDecision.CANCEL
        assert state.deadline is None
        assert state.last_valid_roster_empty is True

    def test_empty_roster_arms_after_guard(self):
        state = armed_state()
        assert state.generation == 1
        assert state.last_valid_sample_at == 131.0
        assert state.last_valid_roster_empty is True

    def test_later_empty_samples_keep_the_original_deadline(self):
        state, decision = sample(armed_state(), now=150.0, count=0, seq=2)

        assert decision == AutoStopDecision.NONE
        assert state.deadline == 191.0

    def test_players_cancel(self):
        state, decision = sample(armed_state(), now=140.0, count=1, seq=2)

        assert decision == AutoStopDecision.CANCEL
        assert state.deadline is None
        assert state.last_valid_roster_empty is False

    def test_unavailable_cancels_and_forgets_sample(self):
        state, decision = on_unavailable(armed_state())

        assert decision == AutoStopDecision.CANCEL
        assert state == AutoStopState()

    def test_disabled_config_cancels(self):
        state, decision = on_roster(
            AutoStopState(),
            config=AutoStopConfig(enabled=False, delay=60),
            now=200.0,
            generation=1,
            player_count=0,
            sample_seq=1,
            started_at=100.0,
        )

        assert decision == AutoStopDecision.CANCEL
        assert state.deadline is None

    def test_cancelled_keeps_the_last_sample_record(self):
        state = cancelled(armed_state())

        assert state.deadline is None
        assert state.last_valid_sample_at == 131.0
        assert state.last_valid_roster_empty is True


class TestExpiry:
    def test_expiry_never_stops_on_what_is_already_known(self):
        # Even a sample taken a moment ago cannot confirm: a player may have
        # joined since. Only a poll issued after the expiry can.
        fresh, _ = sample(armed_state(), now=190.9, count=0, seq=5)

        state, decision = expire(fresh, seq=5)

        assert decision == AutoStopDecision.REVALIDATE
        assert state.revalidation == 1
        assert state.revalidation_seq == 6
        assert state.deadline is None

    def test_sample_from_a_poll_issued_before_expiry_cannot_confirm(self):
        state, _ = expire(armed_state(), seq=5)

        state, decision = sample(state, now=192.0, count=0, seq=5)

        assert decision == AutoStopDecision.REVALIDATE
        assert state.revalidation == 1

    def test_empty_sample_issued_after_expiry_confirms(self):
        state, _ = expire(armed_state(), seq=5)

        state, decision = sample(state, now=192.0, count=0, seq=6)

        assert decision == AutoStopDecision.STOP
        assert state.deadline is None

    def test_player_seen_after_expiry_cancels(self):
        state, _ = expire(armed_state(), seq=5)

        state, decision = sample(state, now=192.0, count=1, seq=6)

        assert decision == AutoStopDecision.CANCEL
        assert state.deadline is None
        assert state.revalidation is None

    def test_unavailable_sample_after_expiry_cancels(self):
        state, _ = expire(armed_state(), seq=5)

        state, decision = on_unavailable(state)

        assert decision == AutoStopDecision.CANCEL
        assert state == AutoStopState()

    @pytest.mark.parametrize(
        "kwargs",
        [{"generation": 2}, {"config": AutoStopConfig(enabled=False, delay=60)}],
    )
    def test_generation_change_or_disabled_cancels(self, kwargs):
        _state, decision = expire(armed_state(generation=1), **kwargs)

        assert decision == AutoStopDecision.CANCEL

    def test_expiry_during_revalidation_waits(self):
        state, _ = expire(armed_state(), seq=5)
        state = replace(state, deadline=200.0, generation=1)  # defensive branch

        _state, decision = expire(state)

        assert decision == AutoStopDecision.NONE
