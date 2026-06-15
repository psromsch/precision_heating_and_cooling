"""Pure-logic failsafe conditions.

Every failsafe here is Home Assistant agnostic and deterministic: timers receive
``now`` (a monotonic seconds value) explicitly rather than reading a clock, so
each condition can be exhaustively unit-tested. The coordinator owns the real
timers and clock and simply feeds them in.

Two reusable building blocks cover most cases:

* ``SustainedCondition`` -- fires once when a boolean condition has held
  continuously for at least ``duration_s``; resets when the condition clears.
* ``UnresponsiveTrv`` -- fires once when a room has been heating for at least
  ``window_s`` but its temperature has risen by less than ``min_rise``.

The remaining failsafes are simple stateless predicates.
"""

from __future__ import annotations


# --- Per-room heating state --------------------------------------------------

def is_heating(boiler_on: bool, trv_commanded_open: bool) -> bool:
    """A room is heating only when the boiler runs AND we commanded its TRV open.

    This is the "heating boolean" referenced throughout the design: it is based
    on what we *command*, not on what the TRV reports back, so it is available
    immediately and is what the time-windowed failsafes key off.
    """
    return boiler_on and trv_commanded_open


# --- Stateless predicates ----------------------------------------------------

def is_unauthorized_boiler(
    real_boiler_on: bool,
    master_on: bool,
    paused: bool,
    active_window_open: bool,
) -> bool:
    """True when the real boiler switch is ON but nothing authorises it.

    The control loop would never *command* the boiler on in these states; this
    guard catches the boiler being on anyway (manual toggle, integration hiccup)
    so the coordinator can force it off and alert.
    """
    if not real_boiler_on:
        return False
    return (not master_on) or paused or active_window_open


def is_overheating(temperature: float | None, heating: bool, threshold: float) -> bool:
    """True when a heating room has exceeded the absolute overheat threshold."""
    if temperature is None or not heating:
        return False
    return temperature > threshold


def trv_setpoint_mismatch(
    boiler_on: bool,
    room_should_heat: bool,
    actual_trv_target: float | None,
    schedule_target: float,
) -> bool:
    """True when a room that should be heating has a TRV target below its schedule target.

    We force a heating room's TRV to a high setpoint; if it reads back below the
    schedule target the valve did not take the command (reverted externally, or
    a stale/failed write).
    """
    if not boiler_on or not room_should_heat or actual_trv_target is None:
        return False
    return actual_trv_target < schedule_target


# --- Stateful timers ---------------------------------------------------------

class SustainedCondition:
    """Fires once when a boolean condition holds continuously for ``duration_s``."""

    def __init__(self, duration_s: float) -> None:
        self.duration_s = duration_s
        self._since: float | None = None
        self._alerted = False

    def reset(self) -> None:
        self._since = None
        self._alerted = False

    @property
    def active_since(self) -> float | None:
        return self._since

    def update(self, now: float, condition_active: bool) -> bool:
        """Advance the timer. Returns True exactly once, when first triggered."""
        if not condition_active:
            self.reset()
            return False
        if self._since is None:
            self._since = now
        if not self._alerted and (now - self._since) >= self.duration_s:
            self._alerted = True
            return True
        return False


class UnresponsiveTrv:
    """Fires once when a room heats for ``window_s`` but rises less than ``min_rise``."""

    def __init__(self, window_s: float = 2700.0, min_rise: float = 0.5) -> None:
        self.window_s = window_s
        self.min_rise = min_rise
        self._since: float | None = None
        self._start_temp: float | None = None
        self._alerted = False

    def reset(self) -> None:
        self._since = None
        self._start_temp = None
        self._alerted = False

    def update(self, now: float, heating: bool, temperature: float | None) -> bool:
        """Advance the timer. Returns True exactly once, when first triggered."""
        if not heating or temperature is None:
            self.reset()
            return False
        if self._since is None:
            self._since = now
            self._start_temp = temperature
        if (
            not self._alerted
            and (now - self._since) >= self.window_s
            and self._start_temp is not None
            and (temperature - self._start_temp) < self.min_rise
        ):
            self._alerted = True
            return True
        return False
