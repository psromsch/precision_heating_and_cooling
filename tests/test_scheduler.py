"""Tests for the schedule resolution and validation engine."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.models.schedule import (
    RoomSchedule,
    ScheduleBlock,
    ScheduleMode,
    day_key_for,
)
from custom_components.precision_climate.scheduler.engine import (
    next_boundary,
    resolve_active_set,
    validate,
)

MON, SAT = 0, 5


def block(start_h, end_h, target=20.0, active=True):
    end = 1440 if end_h == 24 else end_h * 60
    return ScheduleBlock(start_min=start_h * 60, end_min=end, target=target, is_active=active)


def all_days(room_id, blocks):
    return RoomSchedule(room_id=room_id, mode=ScheduleMode.ALL_DAYS, blocks={"all": blocks})


# --- Day key mapping ---------------------------------------------------------

def test_day_key_mapping():
    assert day_key_for(ScheduleMode.ALL_DAYS, MON) == "all"
    assert day_key_for(ScheduleMode.WEEKDAY_WEEKEND, MON) == "weekday"
    assert day_key_for(ScheduleMode.WEEKDAY_WEEKEND, SAT) == "weekend"
    assert day_key_for(ScheduleMode.PER_DAY, SAT) == "sat"


# --- Resolution --------------------------------------------------------------

def test_resolve_picks_correct_block():
    sched = all_days("r1", [block(0, 8, target=18), block(8, 22, target=21), block(22, 24, target=17)])
    assert sched.resolve(MON, 7 * 60).target == 18
    assert sched.resolve(MON, 12 * 60).target == 21
    assert sched.resolve(MON, 23 * 60).target == 17


def test_resolve_active_set_marks_active_flag():
    a = all_days("a", [block(0, 24, active=True)])
    b = all_days("b", [block(0, 24, active=False)])
    resolved = resolve_active_set([a, b], MON, 600, default_room_id="b")
    flags = {r.room_id: r.is_active for r in resolved}
    assert flags == {"a": True, "b": False}


def test_default_fallback_promotes_when_no_active_room():
    a = all_days("a", [block(0, 24, active=False)])
    b = all_days("b", [block(0, 24, active=False)])
    resolved = resolve_active_set([a, b], MON, 600, default_room_id="b")
    by_id = {r.room_id: r for r in resolved}
    assert by_id["b"].is_active is True
    assert by_id["b"].via_default_fallback is True
    assert by_id["a"].is_active is False


def test_no_fallback_needed_when_a_room_is_active():
    a = all_days("a", [block(0, 24, active=True)])
    b = all_days("b", [block(0, 24, active=False)])
    resolved = resolve_active_set([a, b], MON, 600, default_room_id="b")
    assert all(not r.via_default_fallback for r in resolved)


# --- Validation --------------------------------------------------------------

def test_validation_passes_for_full_coverage():
    a = all_days("a", [block(0, 12, active=True), block(12, 24, active=True)])
    errors, warnings = validate([a], default_room_id="a")
    assert errors == []
    assert warnings == []


def test_validation_detects_gap():
    a = all_days("a", [block(0, 10), block(12, 24)])  # gap 10:00-12:00
    errors, _ = validate([a], default_room_id="a")
    assert any("gap" in e for e in errors)


def test_validation_detects_missing_midnight_start_and_end():
    a = all_days("a", [block(6, 22)])  # doesn't start at 0 or end at 24
    errors, _ = validate([a], default_room_id="a")
    assert any("00:00" in e for e in errors)
    assert any("24:00" in e for e in errors)


def test_validation_detects_overlap():
    a = RoomSchedule(
        "a",
        ScheduleMode.ALL_DAYS,
        {"all": [block(0, 14), block(12, 24)]},  # overlap 12:00-14:00
    )
    errors, _ = validate([a], default_room_id="a")
    assert any("overlap" in e for e in errors)


def test_validation_warns_on_no_active_interval():
    # active 8-22, passive otherwise -> 00:00-08:00 and 22:00-24:00 have no active
    a = all_days("a", [block(0, 8, active=False), block(8, 22, active=True), block(22, 24, active=False)])
    errors, warnings = validate([a], default_room_id="a")
    assert errors == []  # default room covers it
    assert any("no active room" in w for w in warnings)


def test_validation_errors_when_no_active_and_no_default():
    a = all_days("a", [block(0, 24, active=False)])
    errors, _ = validate([a], default_room_id=None)
    assert any("no default room" in e for e in errors)


def test_missing_default_room_is_error():
    a = all_days("a", [block(0, 24, active=True)])
    errors, _ = validate([a], default_room_id="ghost")
    assert any("not a configured room" in e for e in errors)


# --- Next boundary -----------------------------------------------------------

def test_next_boundary_same_day():
    a = all_days("a", [block(0, 8), block(8, 22), block(22, 24)])
    assert next_boundary([a], MON, 7 * 60) == (MON, 8 * 60)
    assert next_boundary([a], MON, 9 * 60) == (MON, 22 * 60)


def test_next_boundary_wraps_to_next_day():
    a = all_days("a", [block(0, 8), block(8, 24)])
    # after 8:00 the only remaining boundary today is none -> next day 00:00 (point 0)
    assert next_boundary([a], MON, 23 * 60) == (1, 0)
