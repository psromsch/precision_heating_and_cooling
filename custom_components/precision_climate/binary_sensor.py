"""Binary sensors: one "heating" sensor per room.

A room's heating sensor is True when the boiler is running AND we have commanded
that room's TRV open -- the agreed "heating boolean".
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entities.base import PrecisionBaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        RoomHeatingBinarySensor(coordinator, entry.entry_id, room)
        for room in coordinator.config.rooms
    )


class RoomHeatingBinarySensor(PrecisionBaseEntity, BinarySensorEntity):
    """Indicates whether a room is actively heating."""

    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, coordinator, entry_id: str, room) -> None:
        super().__init__(coordinator, entry_id)
        self._room = room
        self._attr_name = f"{room.name} Heating"
        self._attr_unique_id = f"{entry_id}_{room.room_id}_heating"

    @property
    def is_on(self) -> bool:
        return self._coordinator.room_heating.get(self._room.room_id, False)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "active": self._coordinator.resolved_active.get(self._room.room_id),
            "target": self._coordinator.resolved_targets.get(self._room.room_id),
        }
