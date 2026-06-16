"""Tests for the human-friendly schedule text parser."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.models.schedule import RoomSchedule, ScheduleMode
from custom_components.precision_climate.models.schedule_text import (
    ParseError,
    blocks_to_dicts,
    dicts_to_text,
    parse_day_schedule,
)


def test_parses_valid_schedule():
    text = """
    00:00-08:00 18 passive
    08:00-22:00 21 active
    22:00-24:00 17 passive
    """
    blocks = parse_day_schedule(text)
    assert len(blocks) == 3
    assert blocks[0].start_min == 0 and blocks[0].end_min == 480
    assert blocks[1].target == 21.0 and blocks[1].is_active is True
    assert blocks[2].end_min == 1440 and blocks[2].is_active is False


def test_ignores_blank_lines_and_comments():
    text = "# morning\n08:00-24:00 20 active\n\n"
    assert len(parse_day_schedule(text)) == 1


def test_accepts_alternate_active_keywords():
    assert parse_day_schedule("00:00-24:00 20 a")[0].is_active is True
    assert parse_day_schedule("00:00-24:00 20 passive")[0].is_active is False
    assert parse_day_schedule("00:00-24:00 20 true")[0].is_active is True


@pytest.mark.parametrize(
    "bad",
    [
        "08:00-09:00 20",            # too few fields
        "0800-0900 20 active",       # bad time format
        "09:00-08:00 20 active",     # end before start
        "08:00-09:00 hot active",    # bad target
        "08:00-09:00 20 maybe",      # bad active word
        "25:00-26:00 20 active",     # out of range
    ],
)
def test_rejects_invalid_lines(bad):
    with pytest.raises(ParseError):
        parse_day_schedule(bad)


def test_parsed_blocks_feed_validation_cleanly():
    blocks = parse_day_schedule("00:00-12:00 19 active\n12:00-24:00 21 active")
    sched = RoomSchedule("r1", ScheduleMode.ALL_DAYS, {"all": blocks})
    assert sched.coverage_errors() == []


def test_blocks_to_dicts_roundtrip_shape():
    blocks = parse_day_schedule("00:00-24:00 20 active")
    dicts = blocks_to_dicts(blocks)
    assert dicts == [{"start_min": 0, "end_min": 1440, "target": 20.0, "is_active": True}]


def test_dicts_to_text_roundtrips_through_parser():
    original = "00:00-08:00 18 passive\n08:00-24:00 21 active"
    dicts = blocks_to_dicts(parse_day_schedule(original))
    text = dicts_to_text(dicts)
    # Re-parsing the rendered text yields the same blocks.
    assert blocks_to_dicts(parse_day_schedule(text)) == dicts
    assert text == original
