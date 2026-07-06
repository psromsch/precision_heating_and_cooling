"""Tests for the pure per-room mode resolution (target + active flag)."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.control.mode import (
    PRESENCE_ABSENT,
    PRESENCE_PRESENT,
    resolve_room_mode,
)

PAUSE = 5.0


def resolve(**kw):
    base = dict(
        schedule_target=20.0,
        schedule_active=True,
        away_target=15.0,
        pause_target=PAUSE,
    )
    base.update(kw)
    return resolve_room_mode(**base)


def test_plain_schedule_passes_through():
    assert resolve(schedule_active=True) == (20.0, True)
    assert resolve(schedule_active=False) == (20.0, False)


def test_boost_wins_over_everything():
    t, a = resolve(
        boost_target=22.0,
        paused=True,
        manual_room_away=True,
        global_away=True,
    )
    assert (t, a) == (22.0, True)


def test_pause_drops_target_keeps_flag():
    assert resolve(paused=True) == (PAUSE, True)


def test_manual_room_away_caps_and_forces_passive():
    # New rule: away = passive, even if the schedule says active.
    assert resolve(schedule_active=True, manual_room_away=True) == (15.0, False)


def test_global_away_caps_only_keeps_active():
    # The one exception: global away keeps the schedule's active flag so the
    # boiler can still be driven to hold the away target.
    assert resolve(schedule_active=True, global_away=True) == (15.0, True)
    assert resolve(schedule_active=False, global_away=True) == (15.0, False)


def test_presence_present_active():
    t, a = resolve(
        schedule_active=False,
        has_presence=True,
        presence_state=PRESENCE_PRESENT,
        present_action="active",
    )
    assert (t, a) == (20.0, True)


def test_presence_present_passive():
    t, a = resolve(
        schedule_active=True,
        has_presence=True,
        presence_state=PRESENCE_PRESENT,
        present_action="passive",
    )
    assert (t, a) == (20.0, False)


def test_presence_absent_passive():
    t, a = resolve(
        schedule_active=True,
        has_presence=True,
        presence_state=PRESENCE_ABSENT,
        absent_action="passive",
    )
    assert (t, a) == (20.0, False)


def test_presence_absent_away_caps_and_passive():
    t, a = resolve(
        schedule_active=True,
        has_presence=True,
        presence_state=PRESENCE_ABSENT,
        absent_action="away",
    )
    assert (t, a) == (15.0, False)


def test_presence_unconfirmed_falls_back_to_schedule():
    # No confirmed reading yet -> schedule flag stands.
    t, a = resolve(
        schedule_active=True,
        has_presence=True,
        presence_state=None,
    )
    assert (t, a) == (20.0, True)


def test_manual_away_overrides_presence_present():
    # Manual per-room away beats presence saying "occupied -> active".
    t, a = resolve(
        schedule_active=True,
        manual_room_away=True,
        has_presence=True,
        presence_state=PRESENCE_PRESENT,
        present_action="active",
    )
    assert (t, a) == (15.0, False)


def test_present_passive_absent_away_combo():
    # The user's example: present = passive, absent = away.
    present = resolve(
        has_presence=True,
        presence_state=PRESENCE_PRESENT,
        present_action="passive",
        absent_action="away",
    )
    assert present == (20.0, False)
    absent = resolve(
        has_presence=True,
        presence_state=PRESENCE_ABSENT,
        present_action="passive",
        absent_action="away",
    )
    assert absent == (15.0, False)


def test_away_without_configured_target_still_passive():
    # No away target configured -> can't cap, but away still forces passive.
    assert resolve(away_target=None, manual_room_away=True) == (20.0, False)


# --- Soft away (alarm armed) -------------------------------------------------

def test_soft_away_lowers_target_keeps_active():
    # Alarm armed, no other away -> target drops by the delta, flag unchanged.
    assert resolve(soft_away_active=True, soft_away_delta=2.0) == (18.0, True)
    assert resolve(
        schedule_active=False, soft_away_active=True, soft_away_delta=2.0
    ) == (18.0, False)


def test_soft_away_inactive_no_change():
    assert resolve(soft_away_active=False, soft_away_delta=2.0) == (20.0, True)


def test_soft_away_clamped_to_away_target():
    # A big delta can't drop below the away target (soft stays gentler).
    assert resolve(away_target=15.0, soft_away_active=True, soft_away_delta=9.0) == (
        15.0,
        True,
    )


def test_manual_away_overrules_soft_away():
    # Room already away -> away target, soft away ignored entirely.
    assert resolve(
        manual_room_away=True, soft_away_active=True, soft_away_delta=2.0
    ) == (15.0, False)


def test_global_away_overrules_soft_away():
    assert resolve(
        global_away=True, soft_away_active=True, soft_away_delta=2.0
    ) == (15.0, True)


def test_boost_overrules_soft_away():
    assert resolve(
        boost_target=22.0, soft_away_active=True, soft_away_delta=2.0
    ) == (22.0, True)


def test_soft_away_with_presence_absent_away_uses_away():
    # Presence sends the room to away -> away wins over soft away.
    assert resolve(
        has_presence=True,
        presence_state=PRESENCE_ABSENT,
        absent_action="away",
        soft_away_active=True,
        soft_away_delta=2.0,
    ) == (15.0, False)
