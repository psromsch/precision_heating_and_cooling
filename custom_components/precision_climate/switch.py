"""Switch entities: the heating master switch and per-room pause switches."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entities.base import PrecisionBaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [
        MasterSwitch(coordinator, entry.entry_id),
        AwayModeSwitch(coordinator, entry.entry_id),
    ]
    entities += [
        RoomPauseSwitch(coordinator, entry.entry_id, room)
        for room in coordinator.config.rooms
    ]
    async_add_entities(entities)


class MasterSwitch(PrecisionBaseEntity, SwitchEntity, RestoreEntity):
    """Enables or disables the whole heating system.

    Restored across restarts: a system switched off for the season must not
    silently re-enable (and start heating) just because HA rebooted.
    """

    _attr_name = "Heating Master"
    _attr_icon = "mdi:radiator"

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_master"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        # Default is ON; only an explicit OFF needs restoring.
        if last is not None and last.state == STATE_OFF:
            await self._coordinator.async_set_master(False)

    @property
    def is_on(self) -> bool:
        return self._coordinator.master_on

    async def async_turn_on(self, **kwargs) -> None:
        await self._coordinator.async_set_master(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coordinator.async_set_master(False)


class AwayModeSwitch(PrecisionBaseEntity, SwitchEntity, RestoreEntity):
    """Away mode: while on, every room's target is capped at its configured
    away target. State is restored across restarts/reloads, and the switch can
    be driven by presence automations."""

    _attr_name = "Away Mode"
    _attr_icon = "mdi:home-export-outline"

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_away"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        # Only restore manual away — presence and holiday away are re-derived on
        # startup by their own subsystems. Restoring presence-away as "manual"
        # would make it sticky and prevent presence from ever clearing it again.
        if (
            last is not None
            and last.state == STATE_ON
            and self._coordinator.away_source == "manual"
        ):
            await self._coordinator.async_set_away(True)

    @property
    def is_on(self) -> bool:
        return self._coordinator.away_on

    async def async_turn_on(self, **kwargs) -> None:
        await self._coordinator.async_set_away(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coordinator.async_set_away(False)


class RoomPauseSwitch(PrecisionBaseEntity, SwitchEntity, RestoreEntity):
    """Pauses a single room: while on, the room's target drops so it stops
    calling for heat. State is restored across restarts/reloads so a config
    edit doesn't silently resume a paused room."""

    _attr_icon = "mdi:pause-circle"

    def __init__(self, coordinator, entry_id: str, room) -> None:
        super().__init__(coordinator, entry_id)
        self._room = room
        self._attr_name = f"{room.name} Pause"
        self._attr_unique_id = f"{entry_id}_{room.room_id}_pause"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            await self._coordinator.async_set_room_paused(self._room.room_id, True)

    @property
    def is_on(self) -> bool:
        return self._coordinator.room_paused(self._room.room_id)

    async def async_turn_on(self, **kwargs) -> None:
        await self._coordinator.async_set_room_paused(self._room.room_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coordinator.async_set_room_paused(self._room.room_id, False)
