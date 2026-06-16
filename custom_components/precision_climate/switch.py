"""Switch entities: the heating master switch and the pause switch."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entities.base import PrecisionBaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MasterSwitch(coordinator, entry.entry_id)])


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
