"""Pure gating decisions that the coordinator drives with live state + timers.

The coordinator owns the sensors and the ``async_call_later`` timers; these
functions decide *what* to do, so the debounce / seeding / recency rules are
unit-testable without a running Home Assistant. Keep them side-effect free.
"""

from __future__ import annotations

from .mode import PRESENCE_ABSENT, PRESENCE_PRESENT

# plan_presence_update actions
PRESENCE_HOLD = "hold"    # unavailable/unknown, or already in the target state
PRESENCE_APPLY = "apply"  # set the confirmed state now, no dwell
PRESENCE_DWELL = "dwell"  # arm a dwell timer for `minutes`, then apply


def plan_presence_update(
    *,
    current: str | None,
    is_on: bool,
    available: bool,
    on_minutes: float,
    off_minutes: float,
) -> tuple[str, str | None, float | None]:
    """Decide how to handle a new occupancy reading.

    ``current`` is the room's confirmed presence state (``present`` / ``absent``
    / ``None`` when never seeded — e.g. a template sensor still initialising at
    startup). Returns ``(action, target, minutes)``:

    * ``(HOLD, None, None)``    — sensor unavailable: hold the last confirmed state.
    * ``(HOLD, target, None)``  — already in the target state: nothing to confirm.
    * ``(APPLY, target, 0.0)``  — first reading since startup: apply immediately
      (the dwell debounces transitions from a KNOWN state; the initial value is
      taken at face value, so a long-clear sensor doesn't force the room active
      and wait out the off-dwell after a restart).
    * ``(DWELL, target, mins)`` — a genuine transition: dwell before applying.
    """
    if not available:
        return PRESENCE_HOLD, None, None
    target = PRESENCE_PRESENT if is_on else PRESENCE_ABSENT
    if current == target:
        return PRESENCE_HOLD, target, None
    if current is None:
        return PRESENCE_APPLY, target, 0.0
    return PRESENCE_DWELL, target, (on_minutes if is_on else off_minutes)


def child_lock_recently_unlocked(
    locks: list[tuple[bool, float | None] | None],
    window_seconds: float,
) -> bool:
    """True if the room's child lock is currently unlocked but was locked until
    recently — the "unlocked to dial a boost" gesture, so it should re-lock.

    ``locks`` is one entry per lock entity: ``(is_on, seconds_since_change)`` or
    ``None`` for an unknown/missing lock. An unknown lock, a fully-locked room,
    or a lock that has been off longer than ``window_seconds`` all return False
    (a long-standing unlock was deliberate — leave it).
    """
    if not locks:
        return False
    if any(lock is None for lock in locks):
        return False  # an unknown lock -> don't touch it
    off = [secs for is_on, secs in locks if not is_on]
    if not off:
        return False  # already fully locked -> nothing to restore
    return any(secs is not None and secs <= window_seconds for secs in off)
