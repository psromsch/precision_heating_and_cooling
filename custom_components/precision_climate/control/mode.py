"""Pure resolution of a room's effective (target, active) for one cycle.

Given the schedule's base target/active flag plus every override input, decide
the room's effective target temperature and whether it drives the boiler
(``is_active``). This is deliberately Home-Assistant agnostic so the precedence
— which has grown rich (boost, pause, per-room away, presence, global away) —
can be unit-tested in isolation.

Precedence, highest wins:
  1. Boost            -> (boost target, active).
  2. Pause            -> (pause target, schedule active flag unchanged).
  3. Per-room away    -> (min(target, away target), PASSIVE). "Away = passive."
     This covers both the manual per-room away toggle AND presence deciding the
     room is vacant with absent_action == "away".
  4. Presence         -> occupied: present_action; vacant: absent_action.
  5. Global away      -> caps the target only; KEEPS the active/passive flag, so
     an active room can still fire the boiler to hold the away target (the sole
     exception to "away = passive").
  6. Soft away        -> if an alarm is armed and NO away (per-room or global)
     is in effect, lower the schedule target by a fixed delta, clamped so it
     never drops below the room's away target (soft is always gentler than
     full away). Active/passive is untouched.
  7. Schedule         -> the base target + active flag.
"""

from __future__ import annotations

from ..const import (
    ABSENT_ACTION_AWAY,
    PRESENT_ACTION_ACTIVE,
)

# Presence states as resolved by the coordinator's dwell timers.
PRESENCE_PRESENT = "present"
PRESENCE_ABSENT = "absent"


def resolve_room_mode(
    *,
    schedule_target: float,
    schedule_active: bool,
    away_target: float | None,
    pause_target: float,
    boost_target: float | None = None,
    paused: bool = False,
    manual_room_away: bool = False,
    global_away: bool = False,
    has_presence: bool = False,
    presence_state: str | None = None,  # PRESENCE_PRESENT | PRESENCE_ABSENT | None
    present_action: str = PRESENT_ACTION_ACTIVE,
    absent_action: str = "passive",
    soft_away_active: bool = False,
    soft_away_delta: float = 0.0,
) -> tuple[float, bool]:
    """Return the effective ``(target, is_active)`` for a room this cycle."""
    # 1. Boost wins over everything.
    if boost_target is not None:
        return boost_target, True

    # 2. Pause: drop the target so the room stops calling for heat. The active
    #    flag is left untouched (a paused room at pause_target never demands
    #    anyway), matching prior behaviour.
    if paused:
        return pause_target, schedule_active

    target = schedule_target
    active = schedule_active

    # 4. Presence override (only for rooms with a sensor and a confirmed state).
    #    Vacant + absent_action "away" folds into the per-room away path below.
    room_away = manual_room_away
    if has_presence and presence_state == PRESENCE_PRESENT:
        active = present_action == PRESENT_ACTION_ACTIVE
    elif has_presence and presence_state == PRESENCE_ABSENT:
        if absent_action == ABSENT_ACTION_AWAY:
            room_away = True
        else:  # passive
            active = False
    # presence_state None (no sensor / unconfirmed / sensor unavailable): keep
    # the schedule's active flag as the fallback.

    # 3. Per-room away (manual toggle or presence-absent-away): cap + PASSIVE.
    #    A real away overrules soft away entirely.
    if room_away:
        if away_target is not None:
            target = min(target, away_target)
        return target, False

    # 5. Global away: cap the target only, keep the active flag so the boiler can
    #    still be driven to hold the away temperature. Overrules soft away.
    if global_away:
        if away_target is not None:
            target = min(target, away_target)
        return target, active

    # 6. Soft away: no other away is in effect, so if the alarm is armed lower
    #    the target by the delta — but never below the away target (soft stays
    #    gentler than full away).
    if soft_away_active and soft_away_delta > 0:
        reduced = target - soft_away_delta
        if away_target is not None:
            reduced = max(reduced, away_target)
        target = reduced

    return target, active
