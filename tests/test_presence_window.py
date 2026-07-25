"""Tests for the per-room presence time-window gating (RoomConfig)."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.models.runtime import RoomConfig


def room(**kw):
    base = dict(room_id="r1", name="R1", trvs=["climate.r1"], thermometer="sensor.r1")
    base.update(kw)
    return RoomConfig(**base)


def test_no_sensor_never_rules():
    r = room()
    assert r.presence_rules_at(600) is False


def test_sensor_no_window_rules_all_day():
    r = room(presence_entity="binary_sensor.p")
    assert r.presence_rules_at(0) is True
    assert r.presence_rules_at(720) is True
    assert r.presence_rules_at(1439) is True


def test_daytime_window():
    # 08:00 (480) to 20:00 (1200): presence rules inside, schedule outside.
    r = room(presence_entity="binary_sensor.p", presence_start="08:00", presence_end="20:00")
    assert r.presence_rules_at(7 * 60) is False       # 07:00 -> schedule
    assert r.presence_rules_at(8 * 60) is True         # 08:00 -> presence (inclusive start)
    assert r.presence_rules_at(12 * 60) is True        # midday -> presence
    assert r.presence_rules_at(20 * 60) is False       # 20:00 -> schedule (exclusive end)
    assert r.presence_rules_at(22 * 60) is False       # 22:00 -> schedule


def test_window_wraps_midnight():
    # 22:00 (1320) to 06:00 (360): overnight window.
    r = room(presence_entity="binary_sensor.p", presence_start="22:00", presence_end="06:00")
    assert r.presence_rules_at(23 * 60) is True        # 23:00 -> presence
    assert r.presence_rules_at(2 * 60) is True          # 02:00 -> presence
    assert r.presence_rules_at(6 * 60) is False         # 06:00 -> schedule
    assert r.presence_rules_at(12 * 60) is False        # midday -> schedule


def test_seconds_format_and_equal_bounds():
    # HH:MM:SS parses; equal start/end degenerates to "all day".
    r = room(presence_entity="binary_sensor.p", presence_start="09:30:00", presence_end="09:30:00")
    assert r.presence_rules_at(0) is True
    r2 = room(presence_entity="binary_sensor.p", presence_start="09:30:00", presence_end="17:00:00")
    assert r2.presence_rules_at(10 * 60) is True
    assert r2.presence_rules_at(8 * 60) is False
