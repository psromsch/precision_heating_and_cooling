"""Schedule resolution and validation.

This turns a set of per-room weekly schedules into the per-room ``(target,
is_active)`` values the control loop needs *right now*, and validates a whole
configuration up-front so the config flow can reject gaps or no-active-room
windows before they ever reach the control loop.

Two safety nets work together:

* Validation reports, for every weekday, any interval where *no* room is
  naturally active. This is surfaced to the user as a warning.
* At runtime ``resolve_active_set`` guarantees at least one active room by
  promoting the configured default room whenever the natural active count is
  zero, so the boiler logic always has something to act on.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..models.schedule import (
    DAY_KEYS_PER_DAY,
    MINUTES_PER_DAY,
    RoomSchedule,
    day_key_for,
)


@dataclass
class ResolvedRoom:
    """A room's schedule-resolved state at a given moment."""

    room_id: str
    target: float
    is_active: bool
    via_default_fallback: bool = False


def resolve_active_set(
    schedules: Iterable[RoomSchedule],
    weekday: int,
    minute: int,
    default_room_id: str | None,
) -> list[ResolvedRoom]:
    """Resolve every room at (weekday, minute), applying the default fallback.

    If no room is naturally active at this moment, the ``default_room_id`` room
    is promoted to active so the system always has at least one active room.
    """
    resolved: list[ResolvedRoom] = []
    for sched in schedules:
        block = sched.resolve(weekday, minute)
        if block is None:
            # Uncovered slot: skip; validation should have caught this. The room
            # simply has no target right now and cannot participate.
            continue
        resolved.append(
            ResolvedRoom(
                room_id=sched.room_id,
                target=block.target,
                is_active=block.is_active,
            )
        )

    if resolved and not any(r.is_active for r in resolved) and default_room_id:
        for r in resolved:
            if r.room_id == default_room_id:
                r.is_active = True
                r.via_default_fallback = True
                break

    return resolved


def _boundaries_for_day(
    schedules: Iterable[RoomSchedule], weekday: int
) -> list[int]:
    """Collect the sorted distinct block-start minutes across rooms for a day."""
    points: set[int] = {0}
    for sched in schedules:
        key = day_key_for(sched.mode, weekday)
        for block in sched.blocks.get(key, []):
            points.add(block.start_min)
    return sorted(p for p in points if 0 <= p < MINUTES_PER_DAY)


def active_coverage_warnings(
    schedules: Iterable[RoomSchedule],
) -> list[str]:
    """Return warnings for intervals (per weekday) where no room is naturally active.

    These intervals are still made safe at runtime by the default fallback, but
    the user should know they exist.
    """
    schedules = list(schedules)
    warnings: list[str] = []
    for weekday in range(7):
        boundaries = _boundaries_for_day(schedules, weekday)
        for minute in boundaries:
            any_active = any(
                (block := s.resolve(weekday, minute)) is not None and block.is_active
                for s in schedules
            )
            if not any_active:
                warnings.append(
                    f"{DAY_KEYS_PER_DAY[weekday]} at {minute // 60:02d}:"
                    f"{minute % 60:02d}: no active room"
                )
    return warnings


def validate(
    schedules: Iterable[RoomSchedule],
    default_room_id: str | None,
) -> tuple[list[str], list[str]]:
    """Validate a configuration.

    Returns ``(errors, warnings)``. Errors are blocking (coverage gaps, missing
    default room). Warnings are advisory (intervals with no naturally active
    room that will rely on the default fallback).
    """
    schedules = list(schedules)
    errors: list[str] = []
    for sched in schedules:
        errors.extend(sched.coverage_errors())

    room_ids = {s.room_id for s in schedules}
    if default_room_id is not None and default_room_id not in room_ids:
        errors.append(f"default room '{default_room_id}' is not a configured room")

    warnings = active_coverage_warnings(schedules)
    if warnings and not default_room_id:
        errors.append(
            "there are moments with no active room and no default room is set"
        )

    return errors, warnings


def next_boundary(
    schedules: Iterable[RoomSchedule], weekday: int, minute: int
) -> tuple[int, int]:
    """Return the (weekday, minute) of the next schedule boundary after now.

    Used by the coordinator to schedule the next time-based re-evaluation. Scans
    forward up to 7 days; falls back to next midnight if nothing else is found.
    """
    schedules = list(schedules)
    for offset in range(8):
        day = (weekday + offset) % 7
        for point in _boundaries_for_day(schedules, day):
            if offset == 0 and point <= minute:
                continue
            return day, point
    return (weekday + 1) % 7, 0
