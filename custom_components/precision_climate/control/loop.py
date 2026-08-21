"""The core control loop.

This module is pure logic: given a snapshot of every room and the global system
state, it decides whether the boiler should run and whether each room's TRV
should be open (force flow) or closed (block flow). It performs no I/O and knows
nothing about Home Assistant, which is what makes it fully unit-testable.

Algorithm (heating):

* Boiler (system-level latching hysteresis, driven by ACTIVE rooms only):
    - Turn ON when any active room temp <= demand_threshold (target - lower_hyst).
    - Turn OFF when ALL active rooms are satisfied (temp >= satisfied_threshold).
    - Turn OFF when every active room has an unavailable thermometer (reason:
      no_active_temp) — it is safer to stop the boiler than to hold indefinitely
      with no temperature feedback.
    - Otherwise hold the previous boiler state.

* TRV per room (latching hysteresis). The valve rule is the SAME for active and
  passive rooms — the only difference between the two is that active rooms drive
  the boiler and passive rooms do not (see the boiler rules above). A passive
  room is therefore just an active room that can't summon the boiler: it "rides"
  whenever the boiler is already running for some active room.
    - Any room with a thermometer: open when temp < target, close when temp >=
      satisfied_threshold (target + upper_hyst); otherwise hold. Heat only
      actually flows while the boiler is on, so a passive valve open with the
      boiler off is inert — the room waits, then heats the instant an active
      room fires the boiler, and its ride stops (stays where it reached) when
      the boiler goes off again.
    - Active room, thermometer unavailable: close (cannot confirm demand,
      fail-safe shut).
    - Passive room, thermometer unavailable: hold previous state.

  Note: lower_hysteresis is meaningless for a passive room — it only ever set
  the boiler-demand threshold, and passive rooms never demand. Only target and
  upper_hysteresis affect a passive room.

Overrides (highest priority first):
    1. Master OFF / paused        -> boiler OFF, all TRVs CLOSED.
    2. Room window open            -> that room is paused: its valve is CLOSED
       and it is excluded from boiler demand. Per-room, not house-wide — the
       boiler keeps running for other active rooms. Only when EVERY active room
       is windowed does the boiler turn off.
    3. Sunny-day savings active    -> active-room targets are reduced to the
       configured minimum before any of the above is evaluated.

The cooling variant is the mirror image (operators flip); it is not yet wired in
but ``Mode`` is threaded through so the structure is ready for it.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..const import Mode
from ..models.room import ControlDecision, RoomState, SystemState


def _effective_target(room: RoomState, system: SystemState) -> float:
    """Apply the sunny-day reduced target to active rooms when savings are on.

    Sunny-day only ever *lowers* the target (min), so it composes with any
    reduction already applied upstream (soft-away, away): it can't raise a room
    that is already below the sunny target back up to it.
    """
    if (
        system.sunny_day_active
        and room.is_active
        and system.sunny_day_target is not None
    ):
        return min(room.target, system.sunny_day_target)
    return room.target


def _trv_intent(
    room: RoomState,
    eff_target: float,
    prev_open: bool,
) -> bool:
    """Decide whether a room's TRV should be open, with latching hysteresis."""
    if room.window_open:
        # Window open -> pause this room: close its valve so we don't heat a
        # vented room. Applies to active and passive rooms alike. (When the
        # 'respect window sensors' toggle is off, window_open is already False.)
        return False
    if room.temperature is None:
        if room.is_active:
            # Active room with no thermometer: close the valve. We cannot confirm
            # demand or satisfaction, so the safe choice is to stop flow.
            return False
        # Passive room with no thermometer: hold — no action is safest.
        return prev_open

    satisfied_threshold = eff_target + room.upper_hysteresis

    if room.temperature >= satisfied_threshold:
        return False  # close: room is satisfied (rode up to target + upper_hyst)
    if room.temperature < eff_target:
        # Both active and passive rooms open below target. Passive rooms only
        # heat while the boiler is already on (they can't drive it); an open
        # passive valve with the boiler off is inert, so gating here is
        # unnecessary — the boiler being off simply means no flow.
        return True

    return prev_open  # in the hysteresis band [target, target + upper]: hold


def evaluate(
    rooms: Iterable[RoomState],
    system: SystemState,
    mode: Mode = Mode.HEAT,
) -> ControlDecision:
    """Run one evaluation of the control loop and return the desired state."""
    rooms = list(rooms)

    # --- Override 1: master off or paused -> everything off/closed. ----------
    if not system.master_on or system.paused:
        reason = "master_off" if not system.master_on else "paused"
        return ControlDecision(
            boiler_on=False,
            trv_open={room.room_id: False for room in rooms},
            reason=reason,
        )

    eff_targets = {room.room_id: _effective_target(room, system) for room in rooms}

    # --- Per-room TRV decisions (independent of the boiler). -----------------
    trv_open = {
        room.room_id: _trv_intent(
            room, eff_targets[room.room_id], system.trv_open.get(room.room_id, False)
        )
        for room in rooms
    }

    active_rooms_all = [room for room in rooms if room.is_active]

    # --- Override 2: windowed rooms are paused (per-room, not house-wide). ----
    # A room with an open window is excluded from boiler demand (its valve is
    # already closed above). The boiler runs for whatever active rooms remain;
    # if EVERY active room is windowed, nothing calls for heat -> boiler off.
    active_rooms = [room for room in active_rooms_all if not room.window_open]
    if active_rooms_all and not active_rooms:
        return ControlDecision(
            boiler_on=False, trv_open=trv_open, reason="active_windows_open"
        )

    # --- Boiler latching hysteresis, driven by active rooms only. ------------
    known_active = [r for r in active_rooms if r.temperature is not None]

    # If there are active rooms but every thermometer is offline, turn the
    # boiler off. Holding indefinitely with no feedback is more dangerous than
    # briefly cutting heat until at least one sensor recovers.
    if active_rooms and not known_active:
        return ControlDecision(
            boiler_on=False, trv_open=trv_open, reason="no_active_temp"
        )

    demand = any(
        r.temperature <= (eff_targets[r.room_id] - r.lower_hysteresis)
        for r in known_active
    )
    satisfied_all = bool(known_active) and all(
        r.temperature >= (eff_targets[r.room_id] + r.upper_hysteresis)
        for r in known_active
    )

    if demand:
        boiler_on = True
        reason = "demand"
    elif satisfied_all:
        boiler_on = False
        reason = "all_satisfied"
    else:
        boiler_on = system.boiler_on
        reason = "hold"

    return ControlDecision(boiler_on=boiler_on, trv_open=trv_open, reason=reason)
