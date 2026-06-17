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

# When a room is paused, its effective target drops to this so it stops calling
# for heat until resumed (the schedule itself is untouched).
PAUSE_TARGET = 5.0


def force_flow_setpoint(mode: Mode) -> float:
    """Return the TRV setpoint that fully opens the valve for the given mode."""
    return TRV_FORCE_FLOW_HEAT if mode is Mode.HEAT else TRV_FORCE_FLOW_COOL


def block_flow_setpoint(mode: Mode) -> float:
    """Return the TRV setpoint that fully closes the valve for the given mode."""
    return TRV_BLOCK_FLOW_HEAT if mode is Mode.HEAT else TRV_BLOCK_FLOW_COOL


# --- Config entry keys -------------------------------------------------------
# Top-level
CONF_BOILER_SWITCH = "boiler_switch"
CONF_ROOMS = "rooms"
CONF_DEFAULT_ROOM = "default_room"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_NOTIFY_SERVICES = "notify_services"
CONF_NOTIFICATIONS = "notifications"
CONF_SUNNY_DAY = "sunny_day"
CONF_SETTINGS = "settings"  # global settings managed from the card's config panel

# Global settings keys (inside the CONF_SETTINGS dict)
CONF_BOOST_DURATION_HOURS = "boost_duration_hours"

# Defaults for global settings
DEFAULT_BOOST_DURATION_HOURS = 1.0

# Per-room
CONF_ROOM_ID = "room_id"
CONF_ROOM_NAME = "name"
CONF_TRVS = "trvs"
CONF_THERMOMETER = "thermometer"
CONF_WINDOWS = "windows"
CONF_LOWER_HYSTERESIS = "lower_hysteresis"
CONF_UPPER_HYSTERESIS = "upper_hysteresis"
CONF_SCHEDULE_MODE = "schedule_mode"
CONF_SCHEDULE_BLOCKS = "schedule_blocks"  # dict[day_key, list[block dict]]

# Schedule block
CONF_BLOCK_START = "start_min"
CONF_BLOCK_END = "end_min"
CONF_BLOCK_TARGET = "target"
CONF_BLOCK_ACTIVE = "is_active"

# Sunny day
CONF_SUNNY_ENABLED = "enabled"
CONF_SUNNY_FORECAST_ENTITY = "forecast_entity"
CONF_SUNNY_MIN_HOURS = "min_hours"
CONF_SUNNY_TARGET = "reduced_target"
CONF_SUNNY_END_MIN = "end_min"  # window ends at this minute-of-day (default midday)

# Defaults
DEFAULT_SUNNY_END_MIN = 12 * 60   # midday
DEFAULT_SUNNY_TARGET = 17.0

# --- Failsafe default thresholds (seconds / degrees) -------------------------
PROLONGED_HEATING_SECONDS = 5 * 60 * 60        # 5 hours of continuous boiler run
TRV_MISMATCH_SECONDS = 10 * 60                 # boiler on 10 min with wrong TRV target
TRV_UNRESPONSIVE_SECONDS = 45 * 60             # boiler on 45 min, temp barely rises
TRV_UNRESPONSIVE_MIN_RISE = 0.5                # minimum acceptable rise over the window
TRV_UNAVAILABLE_SECONDS = 15 * 60              # TRV offline 15 min while heating
DEFAULT_OVERHEAT_THRESHOLD = 24.0              # absolute °C overheat alert
