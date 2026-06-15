"""Sensors: the effective (schedule-resolved) target temperature per room."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entities.base import PrecisionBaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        RoomTargetSensor(coordinator, entry.entry_id, room)
        for room in coordinator.config.rooms
    )


class RoomTargetSensor(PrecisionBaseEntity, SensorEntity):
    """The temperature target currently in effect for a room."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, entry_id: str, room) -> None:
        super().__init__(coordinator, entry_id)
        self._room = room
        self._attr_name = f"{room.name} Target"
        self._attr_unique_id = f"{entry_id}_{room.room_id}_target"

    @property
    def native_value(self) -> float | None:
        return self._coordinator.resolved_targets.get(self._room.room_id)

    @property
    def extra_state_attributes(self) -> dict:
        return {"active": self._coordinator.resolved_active.get(self._room.room_id)}
