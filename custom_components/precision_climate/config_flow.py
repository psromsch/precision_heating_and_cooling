"""Config flow for Precision Climate.

Installation is intentionally tiny: pick the boiler switch and (optionally) the
notify services. Everything else -- rooms, schedules, default room, sunny day,
notification toggles -- is managed afterwards from the integration's *Configure*
button via the options flow, so you can build and edit your setup at any time
without reinstalling.

Schedules are entered with the text format parsed by ``models.schedule_text``;
gaps/overlaps are rejected before they are saved.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
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
    DOMAIN,
)
from .models.schedule import (
    DAY_KEYS_PER_DAY,
    RoomSchedule,
    ScheduleBlock,
    ScheduleMode,
)
from .models.schedule_text import (
    blocks_to_dicts,
    parse_day_schedule,
)
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

# Default schedule seeded for newly created rooms; edited later via the card.
DEFAULT_DAY_SCHEDULE = "00:00-24:00 18 active"


def _entity_picker(domain, multiple=False):
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=domain, multiple=multiple)
    )


def _hysteresis_number():
    # min is 0 so one side can be 0; we validate that not BOTH are 0.
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=0.0, max=5.0, step=0.1, mode="box")
    )


def _temp_number():
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=5.0, max=25.0, step=0.1, mode="box", unit_of_measurement="°C"
        )
    )


def _notify_services_selector(hass: HomeAssistant):
    """Dropdown of the notify.* services currently registered in HA."""
    services = hass.services.async_services().get("notify", {})
    options = sorted(f"notify.{name}" for name in services)
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options, multiple=True, custom_value=True, mode="dropdown"
        )
    )


def _rooms_to_schedules(rooms: list[dict]) -> list[RoomSchedule]:
    """Rebuild RoomSchedule objects from stored room dicts for validation."""
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


def _room_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_ROOM_NAME, default=d.get(CONF_ROOM_NAME, "")): selector.TextSelector(),
            vol.Required(CONF_TRVS, default=d.get(CONF_TRVS, [])): _entity_picker(
                "climate", multiple=True
            ),
            vol.Required(
                CONF_THERMOMETER, default=d.get(CONF_THERMOMETER, "")
            ): _entity_picker("sensor"),
            vol.Optional(CONF_WINDOWS, default=d.get(CONF_WINDOWS, [])): _entity_picker(
                "binary_sensor", multiple=True
            ),
            vol.Required(
                CONF_LOWER_HYSTERESIS, default=d.get(CONF_LOWER_HYSTERESIS, 0.5)
            ): _hysteresis_number(),
            vol.Required(
                CONF_UPPER_HYSTERESIS, default=d.get(CONF_UPPER_HYSTERESIS, 0.5)
            ): _hysteresis_number(),
            vol.Required(
                CONF_SCHEDULE_MODE,
                default=d.get(CONF_SCHEDULE_MODE, ScheduleMode.ALL_DAYS.value),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[m.value for m in ScheduleMode],
                    translation_key="schedule_mode",
                )
            ),
        }
    )


class PrecisionClimateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Tiny install wizard: just the boiler and notify services."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        if user_input is not None:
            data = {
                CONF_BOILER_SWITCH: user_input[CONF_BOILER_SWITCH],
                CONF_NOTIFY_SERVICES: user_input.get(CONF_NOTIFY_SERVICES, []),
            }
            return self.async_create_entry(title="Precision Climate", data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_BOILER_SWITCH): _entity_picker("switch"),
                vol.Optional(
                    CONF_NOTIFY_SERVICES, default=[]
                ): _notify_services_selector(self.hass),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PrecisionClimateOptionsFlow()


class PrecisionClimateOptionsFlow(config_entries.OptionsFlow):
    """Manage rooms, schedules, default room, sunny day and notifications.

    On modern Home Assistant ``self.config_entry`` is provided automatically by
    the framework, so we must NOT set it ourselves. Working state is loaded
    lazily the first time a step runs.
    """

    _loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        entry = self.config_entry
        merged = {**entry.data, **entry.options}
        self._rooms: list[dict] = [dict(r) for r in merged.get(CONF_ROOMS, [])]
        self._default_room = merged.get(CONF_DEFAULT_ROOM)
        self._notify_services = list(merged.get(CONF_NOTIFY_SERVICES, []))
        self._notifications = dict(merged.get(CONF_NOTIFICATIONS, {}))
        self._sunny = dict(merged.get(CONF_SUNNY_DAY, {}))
        # Global settings (boost/away/...) are managed from the card's config
        # panel via the set_settings service. Preserve them so editing a room
        # here doesn't wipe them.
        self._settings = dict(merged.get(CONF_SETTINGS, {}))
        # Transient state while adding/editing a room.
        self._editing_id: str | None = None
        self._current_room: dict | None = None
        self._detail = ""
        self._loaded = True

    # --- Menu ----------------------------------------------------------------

    async def async_step_init(self, user_input: dict | None = None):
        self._ensure_loaded()
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_room", "manage_rooms", "settings"],
        )

    def _save(self):
        """Persist the working state to the entry options and close the dialog."""
        options = {
            CONF_ROOMS: self._rooms,
            CONF_DEFAULT_ROOM: self._default_room,
            CONF_NOTIFY_SERVICES: self._notify_services,
            CONF_NOTIFICATIONS: self._notifications,
            CONF_SUNNY_DAY: self._sunny,
            CONF_SETTINGS: self._settings,
        }
        return self.async_create_entry(title="", data=options)

    # --- Add / edit a room ---------------------------------------------------

    async def async_step_add_room(self, user_input: dict | None = None):
        self._ensure_loaded()
        if user_input is None:
            # Fresh "add" only; on submit we must keep any editing id intact.
            self._editing_id = None
        return await self._room_form(user_input)

    async def _room_form(self, user_input: dict | None, defaults: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            lower = float(user_input[CONF_LOWER_HYSTERESIS])
            upper = float(user_input[CONF_UPPER_HYSTERESIS])
            name = user_input[CONF_ROOM_NAME].strip()
            room_id = self._editing_id or slugify(name) or f"room_{len(self._rooms) + 1}"
            existing = {r[CONF_ROOM_ID] for r in self._rooms if r[CONF_ROOM_ID] != self._editing_id}

            if lower == 0 and upper == 0:
                errors["base"] = "hysteresis_both_zero"
            elif room_id in existing:
                errors["base"] = "duplicate_room"
            else:
                # The schedule is no longer entered here. New rooms get a default
                # full-day block; editing a room preserves its existing schedule
                # (which is edited via the visual card). A schedule-mode change
                # re-seeds defaults for the new set of day keys.
                prev = next(
                    (r for r in self._rooms if r[CONF_ROOM_ID] == self._editing_id), {}
                )
                mode = ScheduleMode(user_input[CONF_SCHEDULE_MODE])
                prev_blocks = (
                    prev.get(CONF_SCHEDULE_BLOCKS, {})
                    if prev.get(CONF_SCHEDULE_MODE) == user_input[CONF_SCHEDULE_MODE]
                    else {}
                )
                default_day = blocks_to_dicts(parse_day_schedule(DEFAULT_DAY_SCHEDULE))
                blocks_by_day = {
                    key: prev_blocks.get(key) or [dict(b) for b in default_day]
                    for key in _DAY_KEYS_FOR_MODE[mode]
                }
                is_new = self._editing_id is None
                self._current_room = {
                    CONF_ROOM_ID: room_id,
                    CONF_ROOM_NAME: name,
                    CONF_TRVS: user_input[CONF_TRVS],
                    CONF_THERMOMETER: user_input[CONF_THERMOMETER],
                    CONF_WINDOWS: user_input.get(CONF_WINDOWS, []),
                    CONF_LOWER_HYSTERESIS: lower,
                    CONF_UPPER_HYSTERESIS: upper,
                    CONF_SCHEDULE_MODE: user_input[CONF_SCHEDULE_MODE],
                    CONF_SCHEDULE_BLOCKS: blocks_by_day,
                }
                # Replace if editing, else append.
                self._rooms = [
                    r for r in self._rooms if r[CONF_ROOM_ID] != room_id
                ]
                self._rooms.append(self._current_room)
                if self._default_room is None:
                    self._default_room = room_id
                if is_new:
                    return await self.async_step_room_created()
                return self._save()

        return self.async_show_form(
            step_id="add_room",
            data_schema=_room_schema(defaults or user_input),
            errors=errors,
        )

    async def async_step_room_created(self, user_input: dict | None = None):
        """Confirmation shown after creating a room with the default schedule."""
        if user_input is not None:
            return self._save()
        assert self._current_room is not None
        return self.async_show_form(
            step_id="room_created",
            data_schema=vol.Schema({}),
            description_placeholders={"name": self._current_room[CONF_ROOM_NAME]},
        )

    # --- Manage existing rooms ----------------------------------------------

    async def async_step_manage_rooms(self, user_input: dict | None = None):
        self._ensure_loaded()
        if not self._rooms:
            return self.async_abort(reason="no_rooms")

        if user_input is not None:
            room_id = user_input["room"]
            if user_input["action"] == "delete":
                self._rooms = [r for r in self._rooms if r[CONF_ROOM_ID] != room_id]
                if self._default_room == room_id:
                    self._default_room = self._rooms[0][CONF_ROOM_ID] if self._rooms else None
                return self._save()
            # edit
            room = next(r for r in self._rooms if r[CONF_ROOM_ID] == room_id)
            self._editing_id = room_id
            self._current_room = dict(room)
            return await self._room_form(None, defaults=room)

        room_options = [
            {"value": r[CONF_ROOM_ID], "label": r[CONF_ROOM_NAME]} for r in self._rooms
        ]
        schema = vol.Schema(
            {
                vol.Required("room"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=room_options)
                ),
                vol.Required("action", default="edit"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["edit", "delete"], translation_key="room_action"
                    )
                ),
            }
        )
        return self.async_show_form(step_id="manage_rooms", data_schema=schema)

    # --- General settings ----------------------------------------------------

    async def async_step_settings(self, user_input: dict | None = None):
        self._ensure_loaded()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._default_room = user_input.get(CONF_DEFAULT_ROOM)
            self._notify_services = user_input.get(CONF_NOTIFY_SERVICES, [])
            self._notifications = {
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
            self._sunny = sunny

            # Validate the whole configuration before saving.
            if self._rooms:
                blocking, _warnings = validate(
                    _rooms_to_schedules(self._rooms), self._default_room
                )
                if blocking:
                    errors["base"] = "config_invalid"
                    self._detail = "; ".join(blocking)
            if not errors:
                return self._save()

        room_options = [
            {"value": r[CONF_ROOM_ID], "label": r[CONF_ROOM_NAME]} for r in self._rooms
        ]
        schema_dict: dict = {}
        if room_options:
            default_room = self._default_room if self._default_room in {
                r[CONF_ROOM_ID] for r in self._rooms
            } else None
            key = (
                vol.Optional(CONF_DEFAULT_ROOM, default=default_room)
                if default_room
                else vol.Optional(CONF_DEFAULT_ROOM)
            )
            schema_dict[key] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=room_options)
            )
        schema_dict[
            vol.Optional(CONF_NOTIFY_SERVICES, default=self._notify_services)
        ] = _notify_services_selector(self.hass)
        # EntitySelector rejects an empty-string default, so only set a default
        # when a forecast entity was actually configured.
        forecast = self._sunny.get(CONF_SUNNY_FORECAST_ENTITY)
        forecast_key = (
            vol.Optional(CONF_SUNNY_FORECAST_ENTITY, default=forecast)
            if forecast
            else vol.Optional(CONF_SUNNY_FORECAST_ENTITY)
        )
        schema_dict.update({
            vol.Required(
                CONF_SUNNY_ENABLED, default=self._sunny.get(CONF_SUNNY_ENABLED, False)
            ): bool,
            forecast_key: _entity_picker("sensor"),
            vol.Optional(
                CONF_SUNNY_MIN_HOURS, default=self._sunny.get(CONF_SUNNY_MIN_HOURS, 7)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=24, step=0.5, mode="box")
            ),
            vol.Optional(
                CONF_SUNNY_TARGET, default=self._sunny.get(CONF_SUNNY_TARGET, DEFAULT_SUNNY_TARGET)
            ): _temp_number(),
        })
        for kind in NOTIFICATION_KINDS:
            schema_dict[
                vol.Required(f"notify_{kind}", default=self._notifications.get(kind, True))
            ] = bool

        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"detail": self._detail},
        )
