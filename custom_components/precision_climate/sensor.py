"""Sensors: the effective (schedule-resolved) target temperature per room."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entities.base import PrecisionBaseEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = [
        RoomTargetSensor(coordinator, entry.entry_id, room)
        for room in coordinator.config.rooms
    ]
    entities.append(SystemStatusSensor(coordinator, entry.entry_id))
    async_add_entities(entities)


class SystemStatusSensor(PrecisionBaseEntity, SensorEntity):
    """Diagnostic: the latest boiler decision and what the loop actually saw."""

    _attr_name = "Status"
    _attr_icon = "mdi:state-machine"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_status"

    @property
    def native_value(self) -> str:
        # e.g. "demand", "all_satisfied", "hold", "master_off", "active_window_open"
        return self._coordinator.last_reason

    @property
    def extra_state_attributes(self) -> dict:
        from homeassistant.helpers import entity_registry as er

        c = self._coordinator
        registry = er.async_get(self.hass)
        entry_id = self._attr_unique_id.removesuffix("_status")

        def own_entity(suffix: str, domain: str) -> str | None:
            """Resolve one of our own entities' current entity_id by unique_id."""
            return registry.async_get_entity_id(domain, DOMAIN, f"{entry_id}_{suffix}")

        rooms = {}
        for room in c.config.rooms:
            rid = room.room_id
            boost = c.room_boost(rid)
            rooms[room.name] = {
                "room_id": rid,
                "temperature": c.observed_temps.get(rid),
                "target": c.resolved_targets.get(rid),
                "active": c.resolved_active.get(rid),
                "trv_open": c.trv_open.get(rid),
                "heating": c.room_heating.get(rid),
                "paused": c.room_paused(rid),
                "boosted": boost is not None,
                "boost_target": boost["target"] if boost else None,
                "boost_expires": boost["expires"].isoformat() if boost else None,
                # Source entity_ids so the history card can plot recorded data
                # without any per-room dashboard configuration.
                "thermometer_entity_id": room.thermometer,
                "target_entity_id": own_entity(f"{rid}_target", "sensor"),
                "heating_entity_id": own_entity(f"{rid}_heating", "binary_sensor"),
            }
        return {
            "boiler_on": c.boiler_on,
            "master_on": c.master_on,
            "master_switch_entity_id": own_entity("master", "switch"),
            "boiler_switch_entity_id": c.config.boiler_switch,
            "paused": c.paused,
            "rooms": rooms,
            # Global settings managed from the card's config panel.
            "settings": dict(c.config.settings),
            # Consumed by the visual schedule card to render/edit schedules.
            "schedules": c.schedule_payload(),
        }


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
