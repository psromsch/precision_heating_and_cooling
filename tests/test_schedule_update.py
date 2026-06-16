"""Tests for applying validated schedule edits (used by the card service)."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.models.schedule_update import (
    ScheduleUpdateError,
    apply_schedule_update,
)


def rooms():
    return [
        {
            "room_id": "living",
            "name": "Living",
            "schedule_mode": "all_days",
            "schedule_blocks": {
                "all": [
                    {"start_min": 0, "end_min": 1440, "target": 18.0, "is_active": True}
                ]
            },
        },
        {
            "room_id": "office",
            "name": "Office",
            "schedule_mode": "weekday_weekend",
            "schedule_blocks": {
                "weekday": [
                    {"start_min": 0, "end_min": 1440, "target": 19.0, "is_active": False}
                ],
                "weekend": [
                    {"start_min": 0, "end_min": 1440, "target": 20.0, "is_active": False}
                ],
            },
        },
    ]


def test_replaces_day_blocks_and_leaves_others_untouched():
    new_blocks = [
        {"start_min": 0, "end_min": 480, "target": 17, "is_active": False},
        {"start_min": 480, "end_min": 1440, "target": 21, "is_active": True},
    ]
    out = apply_schedule_update(rooms(), "office", "weekday", new_blocks)
    office = next(r for r in out if r["room_id"] == "office")
    assert len(office["schedule_blocks"]["weekday"]) == 2
    # weekend untouched
    assert office["schedule_blocks"]["weekend"][0]["target"] == 20.0


def test_does_not_mutate_input():
    original = rooms()
    apply_schedule_update(
        original, "living", "all",
        [{"start_min": 0, "end_min": 1440, "target": 22, "is_active": True}],
    )
    assert original[0]["schedule_blocks"]["all"][0]["target"] == 18.0


def test_sorts_blocks_by_start():
    out = apply_schedule_update(
        rooms(), "living", "all",
        [
            {"start_min": 720, "end_min": 1440, "target": 21, "is_active": True},
            {"start_min": 0, "end_min": 720, "target": 18, "is_active": False},
        ],
    )
    blocks = out[0]["schedule_blocks"]["all"]
    assert [b["start_min"] for b in blocks] == [0, 720]


def test_rejects_unknown_room():
    with pytest.raises(ScheduleUpdateError):
        apply_schedule_update(rooms(), "ghost", "all", [])


def test_rejects_wrong_day_key_for_mode():
    with pytest.raises(ScheduleUpdateError):
        apply_schedule_update(
            rooms(), "living", "weekday",
            [{"start_min": 0, "end_min": 1440, "target": 18, "is_active": True}],
        )


def test_rejects_gap_in_coverage():
    with pytest.raises(ScheduleUpdateError):
        apply_schedule_update(
            rooms(), "living", "all",
            [{"start_min": 0, "end_min": 600, "target": 18, "is_active": True}],
        )


def test_rejects_overlap():
    with pytest.raises(ScheduleUpdateError):
        apply_schedule_update(
            rooms(), "living", "all",
            [
                {"start_min": 0, "end_min": 800, "target": 18, "is_active": True},
                {"start_min": 700, "end_min": 1440, "target": 21, "is_active": True},
            ],
        )


def test_rejects_malformed_block():
    with pytest.raises(ScheduleUpdateError):
        apply_schedule_update(
            rooms(), "living", "all",
            [{"start_min": 0, "end_min": 1440, "target": "hot", "is_active": True}],
        )
