"""Pure helpers for editing a room's schedule from outside the options flow.

The visual dashboard card edits one room/day at a time and saves through a
service. That service must apply exactly the same validation the options flow
does (full 00:00-24:00 coverage, no gaps or overlaps) before the change is
persisted. Keeping this logic here, HA-agnostic, makes it unit-testable and
shared between the service handler and the tests.
"""

from __future__ import annotations

from copy import deepcopy

from ..const import (
    CONF_BLOCK_ACTIVE,
    CONF_BLOCK_END,
    CONF_BLOCK_START,
    CONF_BLOCK_TARGET,
    CONF_ROOM_ID,
    CONF_SCHEDULE_BLOCKS,
    CONF_SCHEDULE_MODE,
)
from .schedule import DAY_KEYS_PER_DAY, RoomSchedule, ScheduleBlock, ScheduleMode

# Valid day keys for each schedule mode.
DAY_KEYS_FOR_MODE: dict[ScheduleMode, list[str]] = {
    ScheduleMode.ALL_DAYS: ["all"],
    ScheduleMode.WEEKDAY_WEEKEND: ["weekday", "weekend"],
    ScheduleMode.PER_DAY: list(DAY_KEYS_PER_DAY),
}


class ScheduleUpdateError(ValueError):
    """Raised when a requested schedule edit is invalid."""


def _normalise_block(raw: dict) -> dict:
    """Coerce one incoming block dict into the stored shape, validating fields."""
    try:
        start = int(raw[CONF_BLOCK_START])
        end = int(raw[CONF_BLOCK_END])
        target = float(raw[CONF_BLOCK_TARGET])
        is_active = bool(raw[CONF_BLOCK_ACTIVE])
    except (KeyError, TypeError, ValueError) as err:
        raise ScheduleUpdateError(f"Malformed block {raw!r}: {err}") from err
    if not (0 <= start < end <= 1440):
        raise ScheduleUpdateError(
            f"Block times out of range or end<=start: {start}-{end}"
        )
    return {
        CONF_BLOCK_START: start,
        CONF_BLOCK_END: end,
        CONF_BLOCK_TARGET: target,
        CONF_BLOCK_ACTIVE: is_active,
    }


def apply_schedule_update(
    rooms: list[dict],
    room_id: str,
    day_key: str,
    blocks: list[dict],
) -> list[dict]:
    """Return a new ``rooms`` list with ``room_id``'s ``day_key`` blocks replaced.

    Validates that the day key is valid for the room's schedule mode and that
    the resulting day is fully and contiguously covered. Raises
    ``ScheduleUpdateError`` otherwise. The input list is not mutated.
    """
    rooms = deepcopy(rooms)
    room = next((r for r in rooms if r[CONF_ROOM_ID] == room_id), None)
    if room is None:
        raise ScheduleUpdateError(f"Unknown room '{room_id}'")

    mode = ScheduleMode(room[CONF_SCHEDULE_MODE])
    valid_keys = DAY_KEYS_FOR_MODE[mode]
    if day_key not in valid_keys:
        raise ScheduleUpdateError(
            f"Day key '{day_key}' is not valid for mode '{mode.value}' "
            f"(expected one of {valid_keys})"
        )

    normalised = [_normalise_block(b) for b in blocks]

    # Validate coverage for just this day by building a one-day RoomSchedule.
    parsed = [
        ScheduleBlock(
            b[CONF_BLOCK_START], b[CONF_BLOCK_END], b[CONF_BLOCK_TARGET], b[CONF_BLOCK_ACTIVE]
        )
        for b in normalised
    ]
    errors = RoomSchedule(room_id, mode, {day_key: parsed}).coverage_errors()
    if errors:
        raise ScheduleUpdateError("; ".join(errors))

    schedule_blocks = dict(room.get(CONF_SCHEDULE_BLOCKS, {}))
    schedule_blocks[day_key] = sorted(normalised, key=lambda b: b[CONF_BLOCK_START])
    room[CONF_SCHEDULE_BLOCKS] = schedule_blocks
    return rooms
