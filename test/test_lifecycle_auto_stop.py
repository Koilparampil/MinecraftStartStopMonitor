"""Deterministic tests for the pure auto-stop policy."""

from dataclasses import replace

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


def expire(state, *, now=191.0, generation=1, inflight=False, due=195.0, seq=5):
    return on_expiry(
        state,
        config=CONFIG,
        now=now,
        generation=generation,
        poll_inflight=inflight,
        poll_due_at=due,
        poll_seq=seq,
        poll_interval=2,
    )


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


class TestAutoStopPolicy:
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

    def test_fresh_empty_sample_stops_on_expiry(self):
        state, _ = sample(armed_state(), now=190.0, count=0, seq=2)
        state, decision = expire(state)

        assert decision == AutoStopDecision.STOP
        assert state.deadline is None

    def test_inflight_poll_revalidates_instead_of_stopping(self):
        state, decision = expire(armed_state(), inflight=True, due=None)

        assert decision == AutoStopDecision.REVALIDATE
        assert state.revalidation == 1
        assert state.revalidation_seq == 6
        assert state.deadline is None

    def test_due_poll_revalidates(self):
        _state, decision = expire(armed_state(), due=191.0)

        assert decision == AutoStopDecision.REVALIDATE

    def test_stale_sample_revalidates(self):
        # The armed sample was taken at 131; 60 s later it is stale.
        _state, decision = expire(armed_state())

        assert decision == AutoStopDecision.REVALIDATE

    def test_generation_change_cancels(self):
        _state, decision = expire(armed_state(generation=1), generation=2)

        assert decision == AutoStopDecision.CANCEL

    def test_expiry_during_revalidation_waits(self):
        state, _ = expire(armed_state(), inflight=True, due=None)
        state = replace(state, deadline=200.0, generation=1)  # defensive branch

        _state, decision = expire(state, now=201.0)

        assert decision == AutoStopDecision.NONE

    def test_old_inflight_sample_requests_another(self):
        state, decision = expire(armed_state(), inflight=True, due=None)
        assert decision == AutoStopDecision.REVALIDATE

        state, decision = sample(state, now=192.0, count=0, seq=5)

        assert decision == AutoStopDecision.REVALIDATE
        assert state.revalidation == 1

    def test_current_empty_sample_confirms(self):
        state, _ = expire(armed_state(), inflight=True, due=None)
        state, decision = sample(state, now=192.0, count=0, seq=6)

        assert decision == AutoStopDecision.STOP
        assert state.deadline is None

    def test_current_non_empty_sample_cancels_revalidation(self):
        state, _ = expire(armed_state(), inflight=True, due=None)
        state, decision = sample(state, now=192.0, count=1, seq=6)

        assert decision == AutoStopDecision.CANCEL
        assert state.deadline is None
        assert state.revalidation is None
