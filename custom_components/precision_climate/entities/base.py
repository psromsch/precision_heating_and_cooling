"""Shared base entity for Precision Climate."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from ..const import DOMAIN


class PrecisionBaseEntity(Entity):
    """Base that links the entity to the coordinator and the integration device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, entry_id: str) -> None:
        self._coordinator = coordinator
        self._entry_id = entry_id

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry_id)},
            "name": "Precision Climate",
            "manufacturer": "Precision Climate",
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self._handle_update))

    def _handle_update(self) -> None:
        self.async_write_ha_state()
