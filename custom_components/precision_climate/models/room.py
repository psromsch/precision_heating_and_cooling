"""Room data model for the control loop.

These structures are intentionally Home Assistant agnostic: they hold only the
values the pure control logic needs. The coordinator is responsible for reading
HA state and populating these snapshots, and for translating the resulting
decisions back into HA service calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoomState:
    """A snapshot of a single room at the moment the control loop runs.

    ``target`` is the schedule-resolved setpoint for *right now*. ``is_active``
    is likewise the schedule-resolved active/passive flag for right now. The
    hysteresis values are offsets in degrees relative to ``target``.
    """

    room_id: str
    target: float
    is_active: bool
    lower_hysteresis: float
    upper_hysteresis: float
    temperature: float | None = None
    window_open: bool = False

    @property
    def demand_threshold(self) -> float:
        """Temperature at/below which an active room calls for the boiler."""
        return self.target - self.lower_hysteresis

    @property
    def satisfied_threshold(self) -> float:
        """Temperature at/above which a room is considered satisfied (TRV closes)."""
        return self.target + self.upper_hysteresis


@dataclass
class SystemState:
    """Global state fed into the control loop.

    The previous boiler and TRV states are required because both the boiler and
    each TRV use *latching* hysteresis: once flowing, they keep flowing until the
    opposite threshold is crossed.
    """

    master_on: bool = True
    paused: bool = False
    boiler_on: bool = False
    trv_open: dict[str, bool] = field(default_factory=dict)
    sunny_day_active: bool = False
    sunny_day_target: float | None = None


@dataclass
class ControlDecision:
    """The output of one control-loop evaluation."""

    boiler_on: bool
    trv_open: dict[str, bool]
    reason: str
