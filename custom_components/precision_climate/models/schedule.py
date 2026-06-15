"""Schedule data model.

A room's schedule is a set of contiguous time blocks per "day key". Each block
carries the target temperature and whether the room is *active* during that
block. Times are expressed as minutes since midnight (0..1440) to keep the logic
trivially testable and timezone-free; the coordinator converts real datetimes
into (weekday, minute) pairs before calling in.

Blocks live within a single day [0, 1440]. A comfort period that spans midnight
(e.g. 22:00-06:00) is represented as two blocks on consecutive days; the config
flow is responsible for that split.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

MINUTES_PER_DAY = 1440


class ScheduleMode(str, Enum):
    """How a room's week is structured."""

    ALL_DAYS = "all_days"            # one schedule for every day
    WEEKDAY_WEEKEND = "weekday_weekend"  # Mon-Fri vs Sat-Sun
    PER_DAY = "per_day"             # an independent schedule per weekday


# Day keys used inside RoomSchedule.blocks.
DAY_KEYS_PER_DAY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def day_key_for(mode: ScheduleMode, weekday: int) -> str:
    """Map a weekday (0=Mon .. 6=Sun) to the schedule's day key for the mode."""
    if mode is ScheduleMode.ALL_DAYS:
        return "all"
    if mode is ScheduleMode.WEEKDAY_WEEKEND:
        return "weekend" if weekday >= 5 else "weekday"
    return DAY_KEYS_PER_DAY[weekday]


@dataclass(frozen=True)
class ScheduleBlock:
    """A single contiguous slice of a day for one room."""

    start_min: int  # inclusive, 0..1439
    end_min: int    # exclusive boundary, 1..1440
    target: float
    is_active: bool

    def covers(self, minute: int) -> bool:
        return self.start_min <= minute < self.end_min


@dataclass
class RoomSchedule:
    """The full weekly schedule for one room."""

    room_id: str
    mode: ScheduleMode
    blocks: dict[str, list[ScheduleBlock]]

    def resolve(self, weekday: int, minute: int) -> ScheduleBlock | None:
        """Return the block in effect at (weekday, minute), or None if uncovered."""
        key = day_key_for(self.mode, weekday)
        for block in self.blocks.get(key, []):
            if block.covers(minute):
                return block
        return None

    def coverage_errors(self) -> list[str]:
        """Return human-readable errors if any day is not fully, contiguously covered."""
        errors: list[str] = []
        for key, blocks in self.blocks.items():
            if not blocks:
                errors.append(f"{self.room_id}/{key}: no blocks defined")
                continue
            ordered = sorted(blocks, key=lambda b: b.start_min)
            if ordered[0].start_min != 0:
                errors.append(
                    f"{self.room_id}/{key}: day does not start at 00:00 "
                    f"(first block starts at {ordered[0].start_min} min)"
                )
            for prev, nxt in zip(ordered, ordered[1:]):
                if nxt.start_min < prev.end_min:
                    errors.append(
                        f"{self.room_id}/{key}: overlap at {nxt.start_min} min"
                    )
                elif nxt.start_min > prev.end_min:
                    errors.append(
                        f"{self.room_id}/{key}: gap between {prev.end_min} and "
                        f"{nxt.start_min} min"
                    )
            if ordered[-1].end_min != MINUTES_PER_DAY:
                errors.append(
                    f"{self.room_id}/{key}: day does not end at 24:00 "
                    f"(last block ends at {ordered[-1].end_min} min)"
                )
        return errors
