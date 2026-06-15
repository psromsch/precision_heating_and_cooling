"""Constants for the Precision Climate integration."""

from __future__ import annotations

from enum import Enum

DOMAIN = "precision_climate"


class Mode(str, Enum):
    """Operating mode for the control loop.

    Only HEAT is implemented today. COOL is reserved so the control logic can
    be extended without restructuring: the algorithm is symmetric and only the
    comparison operators and the flow setpoints flip between the two modes.
    """

    HEAT = "heat"
    COOL = "cool"


# TRV setpoints used to *force* or *block* water/refrigerant flow through a
# radiator valve. We do not rely on the TRV's own thermostat; our control loop
# is the thermostat. To open a valve we command an unreachably high target (in
# heating), to close it we command an unreachably low one.
TRV_FORCE_FLOW_HEAT = 28.0  # set this to fully OPEN a TRV when heating
TRV_BLOCK_FLOW_HEAT = 4.0   # set this to fully CLOSE a TRV when heating

# Mirror values for the future cooling implementation.
TRV_FORCE_FLOW_COOL = 4.0
TRV_BLOCK_FLOW_COOL = 28.0


def force_flow_setpoint(mode: Mode) -> float:
    """Return the TRV setpoint that fully opens the valve for the given mode."""
    return TRV_FORCE_FLOW_HEAT if mode is Mode.HEAT else TRV_FORCE_FLOW_COOL


def block_flow_setpoint(mode: Mode) -> float:
    """Return the TRV setpoint that fully closes the valve for the given mode."""
    return TRV_BLOCK_FLOW_HEAT if mode is Mode.HEAT else TRV_BLOCK_FLOW_COOL
