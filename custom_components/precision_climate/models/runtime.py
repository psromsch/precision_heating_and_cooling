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
    CONF_CHILD_LOCKS,
    CONF_DEFAULT_ROOM,
    CONF_LOWER_HYSTERESIS,
    CONF_NOTIFICATIONS,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_SERVICES,
    CONF_PRESENCE_ENABLED,
    CONF_PRESENCE_GRACE_MINUTES,
    CONF_PRESENCE_PERSONS,
    CONF_PRESENCE_ZONE,
    CONF_ROOM_ABSENT_ACTION,
    CONF_ROOM_ID,
    CONF_ROOM_PRESENCE_ENTITY,
    CONF_ROOM_PRESENCE_OFF_MINUTES,
    CONF_ROOM_PRESENCE_ON_MINUTES,
    CONF_ROOM_PRESENT_ACTION,
    CONF_ROOM_NAME,
    CONF_ROOM_ORDER,
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
    ABSENT_ACTION_AWAY,
    ABSENT_ACTION_PASSIVE,
    DEFAULT_PRESENCE_GRACE_MINUTES,
    DEFAULT_ROOM_PRESENCE_OFF_MINUTES,
    DEFAULT_ROOM_PRESENCE_ON_MINUTES,
    DEFAULT_SUNNY_END_MIN,
    DEFAULT_SUNNY_TARGET,
    PRESENT_ACTION_ACTIVE,
    PRESENT_ACTION_PASSIVE,
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
    # Optional child-lock entity per TRV: {trv_entity_id: child_lock_entity_id}.
    child_locks: dict[str, str] = field(default_factory=dict)
    # Optional per-room presence (occupancy) sensor + dwell/action config.
    presence_entity: str | None = None
    presence_on_minutes: float = 3.0
    presence_off_minutes: float = 5.0
    present_action: str = "active"   # "active" | "passive"
    absent_action: str = "passive"   # "passive" | "away"

    @property
    def child_lock_entities(self) -> list[str]:
        """The configured child-lock entity_ids for this room's TRVs."""
        return [self.child_locks[t] for t in self.trvs if self.child_locks.get(t)]

    @property
    def has_presence(self) -> bool:
        return bool(self.presence_entity)


@dataclass
class SunnyDayConfig:
    """Optional sunny-day savings configuration."""

    enabled: bool = False
    forecast_entity: str | None = None
    min_hours: float = 7.0
    reduced_target: float = DEFAULT_SUNNY_TARGET
    end_min: int = DEFAULT_SUNNY_END_MIN


@dataclass
class PresenceConfig:
    """Optional presence-based away mode configuration."""

    enabled: bool = False
    persons: list[str] = field(default_factory=list)
    zone: str | None = None
    grace_minutes: int = DEFAULT_PRESENCE_GRACE_MINUTES


@dataclass
class RuntimeConfig:
    """Everything the coordinator needs, parsed from the config entry."""

    boiler_switch: str
    rooms: list[RoomConfig]
    schedules: list[RoomSchedule]
    default_room: str | None
    sunny_day: SunnyDayConfig
    presence: PresenceConfig = field(default_factory=PresenceConfig)
    notify_services: list[str] = field(default_factory=list)
    notifications: dict[str, bool] = field(default_factory=dict)
    # Global settings managed from the card's config panel (boost, away, ...).
    settings: dict = field(default_factory=dict)

    def room_by_id(self, room_id: str) -> RoomConfig | None:
        return next((r for r in self.rooms if r.room_id == room_id), None)

    @property
    def boost_duration_hours(self) -> float:
        from ..const import CONF_BOOST_DURATION_HOURS, DEFAULT_BOOST_DURATION_HOURS

        return _safe_float(
            self.settings.get(CONF_BOOST_DURATION_HOURS),
            DEFAULT_BOOST_DURATION_HOURS,
        )

    def away_target(self, room_id: str) -> float | None:
        """The away-mode target for a room, or None if not configured."""
        from ..const import CONF_AWAY_TARGETS

        raw = self.settings.get(CONF_AWAY_TARGETS, {}).get(room_id)
        if raw is None:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    @property
    def soft_away_entity(self) -> str | None:
        """The alarm_control_panel that triggers soft away, or None."""
        from ..const import CONF_SOFT_AWAY_ENTITY

        return self.settings.get(CONF_SOFT_AWAY_ENTITY) or None

    @property
    def soft_away_delta(self) -> float:
        """°C to subtract from each target while soft away is active."""
        from ..const import CONF_SOFT_AWAY_DELTA, DEFAULT_SOFT_AWAY_DELTA

        return _safe_float(
            self.settings.get(CONF_SOFT_AWAY_DELTA), DEFAULT_SOFT_AWAY_DELTA
        )

    @property
    def soft_away_states(self) -> list[str]:
        """Alarm states that count as armed for soft away."""
        from ..const import CONF_SOFT_AWAY_STATES, DEFAULT_SOFT_AWAY_STATES

        raw = self.settings.get(CONF_SOFT_AWAY_STATES)
        states = _safe_list(raw)
        return states or list(DEFAULT_SOFT_AWAY_STATES)


def _safe_float(value, default: float) -> float:
    """Coerce a settings value to float, falling back on garbage.

    The settings dict is writable via the unvalidated ``set_settings`` service;
    a bad value must never make ``build_runtime`` raise, because that bricks
    entry setup on every reload/restart until storage is hand-edited.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple, set)) else []


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
        present_action = raw.get(CONF_ROOM_PRESENT_ACTION, PRESENT_ACTION_ACTIVE)
        if present_action not in (PRESENT_ACTION_ACTIVE, PRESENT_ACTION_PASSIVE):
            present_action = PRESENT_ACTION_ACTIVE
        absent_action = raw.get(CONF_ROOM_ABSENT_ACTION, ABSENT_ACTION_PASSIVE)
        if absent_action not in (ABSENT_ACTION_PASSIVE, ABSENT_ACTION_AWAY):
            absent_action = ABSENT_ACTION_PASSIVE
        rooms.append(
            RoomConfig(
                room_id=room_id,
                name=raw.get(CONF_ROOM_NAME, room_id),
                trvs=list(raw.get(CONF_TRVS, [])),
                thermometer=raw[CONF_THERMOMETER],
                windows=list(raw.get(CONF_WINDOWS, [])),
                lower_hysteresis=float(raw.get(CONF_LOWER_HYSTERESIS, 0.5)),
                upper_hysteresis=float(raw.get(CONF_UPPER_HYSTERESIS, 0.5)),
                child_locks=dict(raw.get(CONF_CHILD_LOCKS, {})),
                presence_entity=raw.get(CONF_ROOM_PRESENCE_ENTITY) or None,
                presence_on_minutes=_safe_float(
                    raw.get(CONF_ROOM_PRESENCE_ON_MINUTES),
                    DEFAULT_ROOM_PRESENCE_ON_MINUTES,
                ),
                presence_off_minutes=_safe_float(
                    raw.get(CONF_ROOM_PRESENCE_OFF_MINUTES),
                    DEFAULT_ROOM_PRESENCE_OFF_MINUTES,
                ),
                present_action=present_action,
                absent_action=absent_action,
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

    settings = dict(data.get(CONF_SETTINGS, {}))

    # Apply the user-defined display order (set from the schedule card). Rooms
    # listed in room_order come first in that order; any room not listed keeps
    # its original relative position at the end. Both visual cards iterate this
    # ordering, so reordering here reorders the schedule card and history card
    # together. Control logic is order-independent, so this is purely cosmetic.
    room_order = _safe_list(settings.get(CONF_ROOM_ORDER, []))
    if room_order:
        rank = {rid: i for i, rid in enumerate(room_order)}
        fallback = len(room_order)
        rooms.sort(key=lambda r: rank.get(r.room_id, fallback))
        schedules.sort(key=lambda s: rank.get(s.room_id, fallback))

    presence = PresenceConfig(
        enabled=bool(settings.get(CONF_PRESENCE_ENABLED, False)),
        persons=_safe_list(settings.get(CONF_PRESENCE_PERSONS, [])),
        zone=settings.get(CONF_PRESENCE_ZONE) or None,
        grace_minutes=_safe_int(
            settings.get(CONF_PRESENCE_GRACE_MINUTES), DEFAULT_PRESENCE_GRACE_MINUTES
        ),
    )

    return RuntimeConfig(
        boiler_switch=data[CONF_BOILER_SWITCH],
        rooms=rooms,
        schedules=schedules,
        default_room=data.get(CONF_DEFAULT_ROOM),
        sunny_day=sunny,
        presence=presence,
        notify_services=notify_services,
        notifications=dict(data.get(CONF_NOTIFICATIONS, {})),
        settings=settings,
    )
