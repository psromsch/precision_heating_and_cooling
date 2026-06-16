"""Switch entities: the heating master switch and per-room pause switches."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entities.base import PrecisionBaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [MasterSwitch(coordinator, entry.entry_id)]
    entities += [
        RoomPauseSwitch(coordinator, entry.entry_id, room)
        for room in coordinator.config.rooms
    ]
    async_add_entities(entities)


class MasterSwitch(PrecisionBaseEntity, SwitchEntity):
    """Enables or disables the whole heating system."""

    _attr_name = "Heating Master"
    _attr_icon = "mdi:radiator"

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_master"

    @property
    def is_on(self) -> bool:
        return self._coordinator.master_on

    async def async_turn_on(self, **kwargs) -> None:
        await self._coordinator.async_set_master(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coordinator.async_set_master(False)


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
