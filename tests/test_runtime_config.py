"""Tests for parsing a stored config-entry dict into runtime objects."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.models.runtime import build_runtime
from custom_components.precision_climate.models.schedule import ScheduleMode


def sample_data():
    return {
        "boiler_switch": "switch.boiler",
        "default_room": "living",
        "notify_service": "notify.mobile_app_phone",
        "notifications": {"overheating": False},
        "rooms": [
            {
                "room_id": "living",
                "name": "Living Room",
                "trvs": ["climate.living_trv1", "climate.living_trv2"],
                "thermometer": "sensor.living_temp",
                "windows": ["binary_sensor.living_window"],
                "lower_hysteresis": 0.4,
                "upper_hysteresis": 0.6,
                "schedule_mode": "all_days",
                "schedule_blocks": {
                    "all": [
                        {"start_min": 0, "end_min": 480, "target": 18.0, "is_active": False},
                        {"start_min": 480, "end_min": 1440, "target": 21.0, "is_active": True},
                    ]
                },
            }
        ],
        "sunny_day": {
            "enabled": True,
            "forecast_entity": "sensor.sunny_hours",
            "min_hours": 7,
            "reduced_target": 17.0,
            "end_min": 720,
        },
    }


def test_build_runtime_parses_rooms_and_entities():
    rt = build_runtime(sample_data())
    assert rt.boiler_switch == "switch.boiler"
    assert rt.default_room == "living"
    assert len(rt.rooms) == 1
    room = rt.rooms[0]
    assert room.name == "Living Room"
    assert room.trvs == ["climate.living_trv1", "climate.living_trv2"]
    assert room.thermometer == "sensor.living_temp"
    assert room.windows == ["binary_sensor.living_window"]
    assert room.lower_hysteresis == 0.4
    assert room.upper_hysteresis == 0.6


def test_build_runtime_parses_schedule():
    rt = build_runtime(sample_data())
    sched = rt.schedules[0]
    assert sched.mode is ScheduleMode.ALL_DAYS
    block = sched.resolve(weekday=0, minute=600)
    assert block is not None
    assert block.target == 21.0
    assert block.is_active is True


def test_build_runtime_parses_sunny_and_notifications():
    rt = build_runtime(sample_data())
    assert rt.sunny_day.enabled is True
    assert rt.sunny_day.forecast_entity == "sensor.sunny_hours"
    assert rt.sunny_day.reduced_target == 17.0
    assert rt.sunny_day.end_min == 720
    assert rt.notify_services == ["notify.mobile_app_phone"]
    assert rt.notifications == {"overheating": False}


def test_notify_services_list_key_preferred():
    data = sample_data()
    del data["notify_service"]
    data["notify_services"] = ["notify.a", "notify.b"]
    rt = build_runtime(data)
    assert rt.notify_services == ["notify.a", "notify.b"]


def test_room_by_id_lookup():
    rt = build_runtime(sample_data())
    assert rt.room_by_id("living").name == "Living Room"
    assert rt.room_by_id("ghost") is None


def test_settings_parsed_and_boost_duration_default():
    rt = build_runtime(sample_data())
    # No settings key -> empty dict and the default boost duration.
    assert rt.settings == {}
    assert rt.boost_duration_hours == 1.0


def test_settings_boost_duration_override():
    data = sample_data()
    data["settings"] = {"boost_duration_hours": 2.5}
    rt = build_runtime(data)
    assert rt.settings["boost_duration_hours"] == 2.5
    assert rt.boost_duration_hours == 2.5


def test_away_target_lookup():
    data = sample_data()
    data["settings"] = {"away_targets": {"living": 15.5}}
    rt = build_runtime(data)
    assert rt.away_target("living") == 15.5
    # Unconfigured room -> None (no away override for it).
    assert rt.away_target("bedroom") is None


def test_away_target_invalid_value_is_none():
    data = sample_data()
    data["settings"] = {"away_targets": {"living": "not-a-number"}}
    rt = build_runtime(data)
    assert rt.away_target("living") is None


def test_child_locks_parsed_and_entities_helper():
    data = sample_data()
    data["rooms"][0]["child_locks"] = {
        "climate.living_trv1": "switch.living_trv1_child_lock",
    }
    rt = build_runtime(data)
    room = rt.rooms[0]
    assert room.child_locks == {"climate.living_trv1": "switch.living_trv1_child_lock"}
    # Only TRVs with a configured lock are returned, in TRV order.
    assert room.child_lock_entities == ["switch.living_trv1_child_lock"]


def test_child_locks_default_empty():
    rt = build_runtime(sample_data())
    room = rt.rooms[0]
    assert room.child_locks == {}
    assert room.child_lock_entities == []


def test_presence_config_defaults():
    rt = build_runtime(sample_data())
    assert rt.presence.enabled is False
    assert rt.presence.persons == []
    assert rt.presence.zone is None
    assert rt.presence.grace_minutes == 10


def test_presence_config_parsed():
    data = sample_data()
    data["settings"] = {
        "presence_enabled": True,
        "presence_persons": ["person.alice"],
        "presence_zone": "zone.santiago",
        "presence_grace_minutes": 5,
    }
    rt = build_runtime(data)
    assert rt.presence.enabled is True
    assert rt.presence.persons == ["person.alice"]
    assert rt.presence.zone == "zone.santiago"
    assert rt.presence.grace_minutes == 5


def test_defaults_applied_when_optional_fields_missing():
    data = {
        "boiler_switch": "switch.b",
        "rooms": [
            {
                "room_id": "r1",
                "thermometer": "sensor.t",
                "schedule_mode": "all_days",
                "schedule_blocks": {"all": [{"start_min": 0, "end_min": 1440, "target": 20, "is_active": True}]},
            }
        ],
    }
    rt = build_runtime(data)
    room = rt.rooms[0]
    assert room.name == "r1"          # falls back to room_id
    assert room.trvs == []
    assert room.windows == []
    assert room.lower_hysteresis == 0.5
    assert rt.sunny_day.enabled is False
    assert rt.default_room is None
