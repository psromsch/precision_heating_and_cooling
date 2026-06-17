"""Parse a stored config-entry dict into typed runtime objects.

This layer is deliberately Home Assistant agnostic so it can be unit-tested:
the config flow writes a plain dict, and both the coordinator (at runtime) and
the tests build the same typed objects from it. It does no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..const import (
    CONF_BLOCK_ACTIVE,
    CONF_BLOCK_END,
    CONF_BLOCK_START,
    CONF_BLOCK_TARGET,
    CONF_BOILER_SWITCH,
    CONF_DEFAULT_ROOM,
    CONF_LOWER_HYSTERESIS,
    CONF_NOTIFICATIONS,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_SERVICES,
    CONF_ROOM_ID,
    CONF_ROOM_NAME,
    CONF_ROOMS,
    CONF_SCHEDULE_BLOCKS,
    CONF_SCHEDULE_MODE,
    CONF_SETTINGS,
    CONF_SUNNY_DAY,
    CONF_SUNNY_ENABLED,
    CONF_SUNNY_END_MIN,
    CONF_SUNNY_FORECAST_ENTITY,
    CONF_SUNNY_MIN_HOURS,
    CONF_SUNNY_TARGET,
    CONF_THERMOMETER,
    CONF_TRVS,
    CONF_UPPER_HYSTERESIS,
    CONF_WINDOWS,
    DEFAULT_SUNNY_END_MIN,
    DEFAULT_SUNNY_TARGET,
)
from .schedule import RoomSchedule, ScheduleBlock, ScheduleMode


@dataclass
class RoomConfig:
    """Static configuration for one room (entities + hysteresis)."""

    room_id: str
    name: str
    trvs: list[str]
    thermometer: str
    windows: list[str] = field(default_factory=list)
    lower_hysteresis: float = 0.5
    upper_hysteresis: float = 0.5


@dataclass
class SunnyDayConfig:
    """Optional sunny-day savings configuration."""

    enabled: bool = False
    forecast_entity: str | None = None
    min_hours: float = 7.0
    reduced_target: float = DEFAULT_SUNNY_TARGET
    end_min: int = DEFAULT_SUNNY_END_MIN


@dataclass
class RuntimeConfig:
    """Everything the coordinator needs, parsed from the config entry."""

    boiler_switch: str
    rooms: list[RoomConfig]
    schedules: list[RoomSchedule]
    default_room: str | None
    sunny_day: SunnyDayConfig
    notify_services: list[str] = field(default_factory=list)
    notifications: dict[str, bool] = field(default_factory=dict)
    # Global settings managed from the card's config panel (boost, away, ...).
    settings: dict = field(default_factory=dict)

    def room_by_id(self, room_id: str) -> RoomConfig | None:
        return next((r for r in self.rooms if r.room_id == room_id), None)

    @property
    def boost_duration_hours(self) -> float:
        from ..const import CONF_BOOST_DURATION_HOURS, DEFAULT_BOOST_DURATION_HOURS

        return float(
            self.settings.get(CONF_BOOST_DURATION_HOURS, DEFAULT_BOOST_DURATION_HOURS)
        )


def _parse_blocks(raw_blocks: dict) -> dict[str, list[ScheduleBlock]]:
    parsed: dict[str, list[ScheduleBlock]] = {}
    for day_key, blocks in raw_blocks.items():
        parsed[day_key] = [
            ScheduleBlock(
                start_min=int(b[CONF_BLOCK_START]),
                end_min=int(b[CONF_BLOCK_END]),
                target=float(b[CONF_BLOCK_TARGET]),
                is_active=bool(b[CONF_BLOCK_ACTIVE]),
            )
            for b in blocks
        ]
    return parsed


def build_runtime(data: dict) -> RuntimeConfig:
    """Build a RuntimeConfig from a stored config-entry dict."""
    rooms: list[RoomConfig] = []
    schedules: list[RoomSchedule] = []

    for raw in data.get(CONF_ROOMS, []):
        room_id = raw[CONF_ROOM_ID]
        rooms.append(
            RoomConfig(
                room_id=room_id,
                name=raw.get(CONF_ROOM_NAME, room_id),
                trvs=list(raw.get(CONF_TRVS, [])),
                thermometer=raw[CONF_THERMOMETER],
                windows=list(raw.get(CONF_WINDOWS, [])),
                lower_hysteresis=float(raw.get(CONF_LOWER_HYSTERESIS, 0.5)),
                upper_hysteresis=float(raw.get(CONF_UPPER_HYSTERESIS, 0.5)),
            )
        )
        schedules.append(
            RoomSchedule(
                room_id=room_id,
                mode=ScheduleMode(raw[CONF_SCHEDULE_MODE]),
                blocks=_parse_blocks(raw.get(CONF_SCHEDULE_BLOCKS, {})),
            )
        )

    raw_sunny = data.get(CONF_SUNNY_DAY, {})
    sunny = SunnyDayConfig(
        enabled=bool(raw_sunny.get(CONF_SUNNY_ENABLED, False)),
        forecast_entity=raw_sunny.get(CONF_SUNNY_FORECAST_ENTITY),
        min_hours=float(raw_sunny.get(CONF_SUNNY_MIN_HOURS, 7.0)),
        reduced_target=float(raw_sunny.get(CONF_SUNNY_TARGET, DEFAULT_SUNNY_TARGET)),
        end_min=int(raw_sunny.get(CONF_SUNNY_END_MIN, DEFAULT_SUNNY_END_MIN)),
    )

    # Notify services: prefer the new list key, fall back to the legacy single
    # string for any entries created before the multi-select change.
    notify_services = list(data.get(CONF_NOTIFY_SERVICES, []))
    legacy = data.get(CONF_NOTIFY_SERVICE)
    if legacy and legacy not in notify_services:
        notify_services.append(legacy)

    return RuntimeConfig(
        boiler_switch=data[CONF_BOILER_SWITCH],
        rooms=rooms,
        schedules=schedules,
        default_room=data.get(CONF_DEFAULT_ROOM),
        sunny_day=sunny,
        notify_services=notify_services,
        notifications=dict(data.get(CONF_NOTIFICATIONS, {})),
        settings=dict(data.get(CONF_SETTINGS, {})),
    )
