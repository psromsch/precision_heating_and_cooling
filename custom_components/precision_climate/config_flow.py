"""Config flow for Precision Climate.

A multi-step wizard:

    user      -> system settings (boiler switch, notify service)
    room      -> one room's entities + hysteresis + schedule mode
    schedule  -> that room's schedule as text (validated for full coverage)
    room_menu -> add another room or finish
    finish    -> default room, sunny day, notification toggles, final validation

Schedules are entered with the text format parsed by ``models.schedule_text``;
gaps/overlaps are rejected before the entry is created. The options flow lets the
user tweak the lightweight settings (default room, sunny day, notifications)
without re-running the whole wizard.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_BLOCK_ACTIVE,
    CONF_BLOCK_END,
    CONF_BLOCK_START,
    CONF_BLOCK_TARGET,
    CONF_BOILER_SWITCH,
    CONF_DEFAULT_ROOM,
    CONF_LOWER_HYSTERESIS,
    CONF_NOTIFICATIONS,
    CONF_NOTIFY_SERVICE,
    CONF_ROOM_ID,
    CONF_ROOM_NAME,
    CONF_ROOMS,
    CONF_SCHEDULE_BLOCKS,
    CONF_SCHEDULE_MODE,
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
    DOMAIN,
)
from .models.schedule import (
    DAY_KEYS_PER_DAY,
    RoomSchedule,
    ScheduleBlock,
    ScheduleMode,
)
from .models.schedule_text import ParseError, blocks_to_dicts, parse_day_schedule
from .scheduler.engine import validate

# The notification kinds the user can toggle on/off.
NOTIFICATION_KINDS = [
    "unauthorized_boiler",
    "prolonged_heating",
    "overheating",
    "trv_mismatch",
    "trv_unresponsive",
    "trv_unavailable",
    "window",
    "sunny_day",
]

_DAY_KEYS_FOR_MODE = {
    ScheduleMode.ALL_DAYS: ["all"],
    ScheduleMode.WEEKDAY_WEEKEND: ["weekday", "weekend"],
    ScheduleMode.PER_DAY: list(DAY_KEYS_PER_DAY),
}


def _entity_picker(domain, multiple=False):
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain, multiple=multiple)
    )


def _hysteresis_number():
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=0.1, max=5.0, step=0.1, mode="box")
    )


def _rooms_to_schedules(rooms: list[dict]) -> list[RoomSchedule]:
    """Rebuild RoomSchedule objects from the stored room dicts for validation."""
    schedules: list[RoomSchedule] = []
    for r in rooms:
        blocks = {
            key: [
                ScheduleBlock(
                    b[CONF_BLOCK_START],
                    b[CONF_BLOCK_END],
                    b[CONF_BLOCK_TARGET],
                    b[CONF_BLOCK_ACTIVE],
                )
                for b in day_blocks
            ]
            for key, day_blocks in r[CONF_SCHEDULE_BLOCKS].items()
        }
        schedules.append(
            RoomSchedule(r[CONF_ROOM_ID], ScheduleMode(r[CONF_SCHEDULE_MODE]), blocks)
        )
    return schedules


class PrecisionClimateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the setup wizard."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._rooms: list[dict] = []
        self._current_room: dict | None = None

    # --- Step: system settings ----------------------------------------------

    async def async_step_user(self, user_input: dict | None = None):
        if user_input is not None:
            self._data[CONF_BOILER_SWITCH] = user_input[CONF_BOILER_SWITCH]
            if user_input.get(CONF_NOTIFY_SERVICE):
                self._data[CONF_NOTIFY_SERVICE] = user_input[CONF_NOTIFY_SERVICE]
            return await self.async_step_room()

        schema = vol.Schema(
            {
                vol.Required(CONF_BOILER_SWITCH): _entity_picker("switch"),
                vol.Optional(CONF_NOTIFY_SERVICE): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    # --- Step: a room's entities --------------------------------------------

    async def async_step_room(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_ROOM_NAME].strip()
            room_id = slugify(name) or f"room_{len(self._rooms) + 1}"
            existing = {r[CONF_ROOM_ID] for r in self._rooms}
            if room_id in existing:
                errors["base"] = "duplicate_room"
            else:
                self._current_room = {
                    CONF_ROOM_ID: room_id,
                    CONF_ROOM_NAME: name,
                    CONF_TRVS: user_input[CONF_TRVS],
                    CONF_THERMOMETER: user_input[CONF_THERMOMETER],
                    CONF_WINDOWS: user_input.get(CONF_WINDOWS, []),
                    CONF_LOWER_HYSTERESIS: user_input[CONF_LOWER_HYSTERESIS],
                    CONF_UPPER_HYSTERESIS: user_input[CONF_UPPER_HYSTERESIS],
                    CONF_SCHEDULE_MODE: user_input[CONF_SCHEDULE_MODE],
                }
                return await self.async_step_schedule()

        schema = vol.Schema(
            {
                vol.Required(CONF_ROOM_NAME): selector.TextSelector(),
                vol.Required(CONF_TRVS): _entity_picker("climate", multiple=True),
                vol.Required(CONF_THERMOMETER): _entity_picker("sensor"),
                vol.Optional(CONF_WINDOWS, default=[]): _entity_picker(
                    "binary_sensor", multiple=True
                ),
                vol.Required(CONF_LOWER_HYSTERESIS, default=0.5): _hysteresis_number(),
                vol.Required(CONF_UPPER_HYSTERESIS, default=0.5): _hysteresis_number(),
                vol.Required(
                    CONF_SCHEDULE_MODE, default=ScheduleMode.ALL_DAYS.value
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[m.value for m in ScheduleMode],
                        translation_key="schedule_mode",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="room", data_schema=schema, errors=errors
        )

    # --- Step: that room's schedule text ------------------------------------

    async def async_step_schedule(self, user_input: dict | None = None):
        assert self._current_room is not None
        mode = ScheduleMode(self._current_room[CONF_SCHEDULE_MODE])
        day_keys = _DAY_KEYS_FOR_MODE[mode]
        errors: dict[str, str] = {}

        if user_input is not None:
            blocks_by_day: dict[str, list[dict]] = {}
            try:
                for key in day_keys:
                    blocks = parse_day_schedule(user_input.get(key, ""))
                    sched = RoomSchedule(
                        self._current_room[CONF_ROOM_ID], mode, {key: blocks}
                    )
                    coverage = sched.coverage_errors()
                    if coverage:
                        errors["base"] = "schedule_coverage"
                        self._schedule_detail = "; ".join(coverage)
                        break
                    blocks_by_day[key] = blocks_to_dicts(blocks)
            except ParseError as err:
                errors["base"] = "schedule_parse"
                self._schedule_detail = str(err)

            if not errors:
                self._current_room[CONF_SCHEDULE_BLOCKS] = blocks_by_day
                self._rooms.append(self._current_room)
                self._current_room = None
                return await self.async_step_room_menu()

        schema = vol.Schema(
            {
                vol.Required(key, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                )
                for key in day_keys
            }
        )
        return self.async_show_form(
            step_id="schedule",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "detail": getattr(self, "_schedule_detail", ""),
                "example": "00:00-08:00 18 passive\n08:00-24:00 21 active",
            },
        )

    # --- Step: add another room or finish -----------------------------------

    async def async_step_room_menu(self, user_input: dict | None = None):
        if user_input is not None:
            if user_input["add_another"]:
                return await self.async_step_room()
            return await self.async_step_finish()
        schema = vol.Schema({vol.Required("add_another", default=False): bool})
        return self.async_show_form(
            step_id="room_menu",
            data_schema=schema,
            description_placeholders={"count": str(len(self._rooms))},
        )

    # --- Step: finishing settings + validation ------------------------------

    async def async_step_finish(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        room_options = [
            {"value": r[CONF_ROOM_ID], "label": r[CONF_ROOM_NAME]} for r in self._rooms
        ]

        if user_input is not None:
            self._data[CONF_ROOMS] = self._rooms
            self._data[CONF_DEFAULT_ROOM] = user_input.get(CONF_DEFAULT_ROOM)
            self._data[CONF_NOTIFICATIONS] = {
                kind: user_input.get(f"notify_{kind}", True) for kind in NOTIFICATION_KINDS
            }
            sunny = {CONF_SUNNY_ENABLED: user_input.get(CONF_SUNNY_ENABLED, False)}
            if sunny[CONF_SUNNY_ENABLED]:
                sunny.update(
                    {
                        CONF_SUNNY_FORECAST_ENTITY: user_input.get(CONF_SUNNY_FORECAST_ENTITY),
                        CONF_SUNNY_MIN_HOURS: user_input.get(CONF_SUNNY_MIN_HOURS, 7),
                        CONF_SUNNY_TARGET: user_input.get(CONF_SUNNY_TARGET, DEFAULT_SUNNY_TARGET),
                        CONF_SUNNY_END_MIN: DEFAULT_SUNNY_END_MIN,
                    }
                )
            self._data[CONF_SUNNY_DAY] = sunny

            schedules = _rooms_to_schedules(self._rooms)
            blocking, _warnings = validate(schedules, self._data[CONF_DEFAULT_ROOM])
            if blocking:
                errors["base"] = "config_invalid"
                self._schedule_detail = "; ".join(blocking)
            else:
                return self.async_create_entry(title="Precision Climate", data=self._data)

        schema_dict: dict = {
            vol.Optional(CONF_DEFAULT_ROOM): selector.SelectSelector(
                selector.SelectSelectorConfig(options=room_options)
            ),
            vol.Required(CONF_SUNNY_ENABLED, default=False): bool,
            vol.Optional(CONF_SUNNY_FORECAST_ENTITY): _entity_picker("sensor"),
            vol.Optional(CONF_SUNNY_MIN_HOURS, default=7): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=24, step=0.5, mode="box")
            ),
            vol.Optional(CONF_SUNNY_TARGET, default=DEFAULT_SUNNY_TARGET): selector.NumberSelector(
                selector.NumberSelectorConfig(min=5.0, max=25.0, step=0.5, mode="box", unit_of_measurement="°C")
            ),
        }
        for kind in NOTIFICATION_KINDS:
            schema_dict[vol.Required(f"notify_{kind}", default=True)] = bool

        return self.async_show_form(
            step_id="finish",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"detail": getattr(self, "_schedule_detail", "")},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PrecisionClimateOptionsFlow(config_entry)


class PrecisionClimateOptionsFlow(config_entries.OptionsFlow):
    """Lightweight options: toggle notifications, default room, sunny day."""

    def __init__(self, config_entry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None):
        data = {**self.config_entry.data, **self.config_entry.options}
        rooms = data.get(CONF_ROOMS, [])
        notifications = data.get(CONF_NOTIFICATIONS, {})
        sunny = data.get(CONF_SUNNY_DAY, {})

        if user_input is not None:
            new_options = dict(self.config_entry.options)
            new_options[CONF_DEFAULT_ROOM] = user_input.get(CONF_DEFAULT_ROOM)
            new_options[CONF_NOTIFICATIONS] = {
                kind: user_input.get(f"notify_{kind}", True) for kind in NOTIFICATION_KINDS
            }
            new_sunny = dict(sunny)
            new_sunny[CONF_SUNNY_ENABLED] = user_input.get(CONF_SUNNY_ENABLED, False)
            new_options[CONF_SUNNY_DAY] = new_sunny
            return self.async_create_entry(title="", data=new_options)

        room_options = [
            {"value": r[CONF_ROOM_ID], "label": r[CONF_ROOM_NAME]} for r in rooms
        ]
        schema_dict: dict = {
            vol.Optional(
                CONF_DEFAULT_ROOM, default=data.get(CONF_DEFAULT_ROOM)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=room_options)
            ),
            vol.Required(
                CONF_SUNNY_ENABLED, default=sunny.get(CONF_SUNNY_ENABLED, False)
            ): bool,
        }
        for kind in NOTIFICATION_KINDS:
            schema_dict[
                vol.Required(f"notify_{kind}", default=notifications.get(kind, True))
            ] = bool
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_dict))
