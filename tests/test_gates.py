"""Tests for the pure coordinator gates (presence dwell + child-lock relock).

These cover behaviour that previously lived only in the (untested) coordinator
— in particular the presence startup-seeding rule that regressed twice.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.control.gates import (
    PRESENCE_APPLY,
    PRESENCE_DWELL,
    PRESENCE_HOLD,
    child_lock_recently_unlocked,
    plan_presence_update,
    soft_away_presence_active,
    trv_drift_grace_active,
)
from custom_components.precision_climate.control.mode import (
    PRESENCE_ABSENT,
    PRESENCE_PRESENT,
)


def plan(current, is_on, available=True, on=3.0, off=5.0):
    return plan_presence_update(
        current=current, is_on=is_on, available=available, on_minutes=on, off_minutes=off
    )


# --- presence dwell / seeding ------------------------------------------------

def test_unavailable_sensor_holds():
    assert plan("present", is_on=False, available=False) == (PRESENCE_HOLD, None, None)


def test_already_in_target_state_is_noop():
    assert plan("absent", is_on=False) == (PRESENCE_HOLD, PRESENCE_ABSENT, None)
    assert plan("present", is_on=True) == (PRESENCE_HOLD, PRESENCE_PRESENT, None)


def test_first_reading_after_startup_applies_immediately():
    # The regression we fixed twice: no baseline yet (template sensor was
    # 'unavailable' at seed time) -> apply the first real reading at face value,
    # NOT after the off-dwell.
    assert plan(None, is_on=False) == (PRESENCE_APPLY, PRESENCE_ABSENT, 0.0)
    assert plan(None, is_on=True) == (PRESENCE_APPLY, PRESENCE_PRESENT, 0.0)


def test_genuine_transition_dwells_with_the_right_delay():
    # absent -> present uses the ON dwell; present -> absent uses the OFF dwell.
    assert plan("absent", is_on=True, on=3.0, off=5.0) == (PRESENCE_DWELL, PRESENCE_PRESENT, 3.0)
    assert plan("present", is_on=False, on=3.0, off=5.0) == (PRESENCE_DWELL, PRESENCE_ABSENT, 5.0)


# --- child-lock relock gate --------------------------------------------------

W = 300.0  # window seconds


def test_no_locks_configured():
    assert child_lock_recently_unlocked([], W) is False


def test_unknown_lock_never_touched():
    assert child_lock_recently_unlocked([None], W) is False
    assert child_lock_recently_unlocked([(False, 10.0), None], W) is False


def test_fully_locked_room_nothing_to_restore():
    assert child_lock_recently_unlocked([(True, 5.0), (True, 900.0)], W) is False


def test_recently_unlocked_relocks():
    # Off, changed 10 s ago (unlocked to boost) -> re-lock.
    assert child_lock_recently_unlocked([(False, 10.0)], W) is True
    # One of two locks recently off is enough.
    assert child_lock_recently_unlocked([(True, 999.0), (False, 30.0)], W) is True


def test_long_standing_unlock_left_alone():
    # Off for well over the window -> deliberately unlocked, keep it unlocked.
    assert child_lock_recently_unlocked([(False, 900.0)], W) is False


def test_off_with_unknown_change_time_is_not_recent():
    assert child_lock_recently_unlocked([(False, None)], W) is False


# --- soft-away presence trigger ----------------------------------------------

def soft(available=True, is_on=False, secs=999.0, grace=600.0):
    return soft_away_presence_active(
        available=available, is_on=is_on, seconds_since_change=secs, grace_seconds=grace
    )


def test_soft_presence_unavailable_never_engages():
    assert soft(available=False) is False


def test_soft_presence_someone_home_never_engages():
    assert soft(is_on=True, secs=9999.0) is False


def test_soft_presence_off_past_grace_engages():
    assert soft(is_on=False, secs=700.0, grace=600.0) is True


def test_soft_presence_off_within_grace_waits():
    assert soft(is_on=False, secs=120.0, grace=600.0) is False


def test_soft_presence_zero_grace_engages_instantly():
    assert soft(is_on=False, secs=0.0, grace=0.0) is True


def test_soft_presence_off_unknown_change_time_conservative():
    assert soft(is_on=False, secs=None, grace=600.0) is False


# --- TRV drift grace / virtual child lock ------------------------------------

def grace(since, g=180.0, lock=False):
    return trv_drift_grace_active(
        seconds_since_change=since, grace_seconds=g, virtual_child_lock=lock
    )


def test_recent_manual_turn_is_not_fought():
    # A hand on the dial 10 s ago -> hold off correcting.
    assert grace(10.0) is True


def test_stale_setpoint_is_corrected():
    # Sitting still far longer than the grace -> correct it.
    assert grace(600.0) is False


def test_never_moved_setpoint_is_corrected():
    assert grace(None) is False


def test_virtual_child_lock_bypasses_the_grace():
    # The whole point: with the lock on, a hand that JUST moved the dial is
    # overridden immediately instead of being protected for the grace window.
    assert grace(0.5, lock=True) is False
    assert grace(10.0, lock=True) is False
    assert grace(None, lock=True) is False
