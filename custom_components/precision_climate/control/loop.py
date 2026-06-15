"""The core control loop.

This module is pure logic: given a snapshot of every room and the global system
state, it decides whether the boiler should run and whether each room's TRV
should be open (force flow) or closed (block flow). It performs no I/O and knows
nothing about Home Assistant, which is what makes it fully unit-testable.

Algorithm (heating):

* Boiler (system-level latching hysteresis, driven by ACTIVE rooms only):
    - Turn ON when any active room temp <= demand_threshold (target - lower_hyst).
    - Turn OFF when ALL active rooms are satisfied (temp >= satisfied_threshold).
    - Otherwise hold the previous boiler state.

* TRV per room (latching hysteresis, independent of the boiler so passive rooms
  can "open and wait"):
    - Active room: open when temp < target, close when temp >= satisfied_threshold.
    - Passive room: open when temp <= demand_threshold, close when temp >=
      satisfied_threshold.
    - Otherwise hold the previous TRV state.

Overrides (highest priority first):
    1. Master OFF / paused        -> boiler OFF, all TRVs CLOSED.
    2. Active-room window open     -> boiler OFF (TRVs follow normal per-room rule).
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
    """Apply the sunny-day reduced target to active rooms when savings are on."""
    if (
        system.sunny_day_active
        and room.is_active
        and system.sunny_day_target is not None
    ):
        return system.sunny_day_target
    return room.target


def _trv_intent(
    room: RoomState,
    eff_target: float,
    prev_open: bool,
) -> bool:
    """Decide whether a room's TRV should be open, with latching hysteresis."""
    if room.temperature is None:
        # Thermometer unavailable: hold the previous state rather than guess.
        return prev_open

    satisfied_threshold = eff_target + room.upper_hysteresis
    demand_threshold = eff_target - room.lower_hysteresis

    if room.temperature >= satisfied_threshold:
        return False  # close: room is satisfied
    if room.is_active:
        if room.temperature < eff_target:
            return True  # active rooms open as soon as they fall below target
    else:
        if room.temperature <= demand_threshold:
            return True  # passive rooms only open once genuinely cold

    return prev_open  # in the hysteresis band: hold


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

    active_rooms = [room for room in rooms if room.is_active]

    # --- Override 2: any active-room window open -> boiler must stay off. -----
    if any(room.window_open for room in active_rooms):
        return ControlDecision(
            boiler_on=False, trv_open=trv_open, reason="active_window_open"
        )

    # --- Boiler latching hysteresis, driven by active rooms only. ------------
    known_active = [r for r in active_rooms if r.temperature is not None]

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
