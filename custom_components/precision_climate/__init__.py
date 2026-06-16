"""The Precision Climate integration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .const import CONF_ROOMS, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

# Platforms are added in the entities milestone. Keep in sync with the files in
# the entities/ package.
PLATFORMS: list[str] = ["switch", "binary_sensor", "sensor"]

SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_SET_ROOM_PAUSE = "set_room_pause"
# Frontend modules served and auto-loaded by the integration.
CARD_FILENAMES = [
    "precision-climate-schedule-card.js",
    "precision-climate-history-card.js",
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Precision Climate from a config entry."""
    # Imported lazily so the pure-logic submodules remain importable without
    # Home Assistant installed (e.g. in the unit-test sandbox).
    from .coordinator import PrecisionClimateCoordinator

    coordinator = PrecisionClimateCoordinator(hass, {**entry.data, **entry.options})
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    _async_cleanup_orphan_entities(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    await _async_register_card(hass)
    _async_register_services(hass)
    return True


def _async_cleanup_orphan_entities(hass, entry, coordinator) -> None:
    """Remove entities for rooms that no longer exist.

    Deleting a room leaves its sensors/binary_sensors registered but
    permanently "unavailable". On each setup we rebuild the set of unique_ids
    the current config produces and purge any registry entry for this config
    entry that isn't in it.
    """
    from homeassistant.helpers import entity_registry as er

    entry_id = entry.entry_id
    valid = {f"{entry_id}_master", f"{entry_id}_status"}
    for room in coordinator.config.rooms:
        valid.add(f"{entry_id}_{room.room_id}_target")
        valid.add(f"{entry_id}_{room.room_id}_heating")
        valid.add(f"{entry_id}_{room.room_id}_pause")

    registry = er.async_get(hass)
    for ent in list(registry.entities.values()):
        if ent.config_entry_id == entry_id and ent.unique_id not in valid:
            registry.async_remove(ent.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_unload()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the set_schedule service once (shared across entries)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        return

    import voluptuous as vol

    from .models.schedule_update import ScheduleUpdateError, apply_schedule_update

    schema = vol.Schema(
        {
            vol.Required("room_id"): str,
            vol.Required("day_key"): str,
            vol.Required("blocks"): [
                {
                    vol.Required("start_min"): vol.Coerce(int),
                    vol.Required("end_min"): vol.Coerce(int),
                    vol.Required("target"): vol.Coerce(float),
                    vol.Required("is_active"): vol.Coerce(bool),
                }
            ],
        }
    )

    async def _handle_set_schedule(call: "ServiceCall") -> None:
        room_id = call.data["room_id"]
        day_key = call.data["day_key"]
        blocks = call.data["blocks"]

        # Find the entry that owns this room and update its stored schedule.
        for entry in hass.config_entries.async_entries(DOMAIN):
            merged = {**entry.data, **entry.options}
            rooms = merged.get(CONF_ROOMS, [])
            if not any(r.get("room_id") == room_id for r in rooms):
                continue
            try:
                new_rooms = apply_schedule_update(rooms, room_id, day_key, blocks)
            except ScheduleUpdateError as err:
                raise vol.Invalid(str(err)) from err
            new_options = {**entry.options, CONF_ROOMS: new_rooms}
            # Triggers the update listener -> reload -> re-evaluation.
            hass.config_entries.async_update_entry(entry, options=new_options)
            return
        raise vol.Invalid(f"No configured room '{room_id}' found")

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _handle_set_schedule, schema=schema
    )

    pause_schema = vol.Schema(
        {
            vol.Required("room_id"): str,
            vol.Required("paused"): vol.Coerce(bool),
        }
    )

    async def _handle_set_room_pause(call: "ServiceCall") -> None:
        room_id = call.data["room_id"]
        paused = call.data["paused"]
        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if coordinator is None:
                continue
            if any(r.room_id == room_id for r in coordinator.config.rooms):
                await coordinator.async_set_room_paused(room_id, paused)
                return
        raise vol.Invalid(f"No configured room '{room_id}' found")

    hass.services.async_register(
        DOMAIN, SERVICE_SET_ROOM_PAUSE, _handle_set_room_pause, schema=pause_schema
    )


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve and auto-load the visual cards as frontend modules."""
    if hass.data.get(f"{DOMAIN}_card_registered"):
        return
    hass.data[f"{DOMAIN}_card_registered"] = True

    version = _manifest_version()
    www_dir = os.path.join(os.path.dirname(__file__), "www")

    for filename in CARD_FILENAMES:
        url = f"/{DOMAIN}/{filename}"
        card_path = os.path.join(www_dir, filename)

        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(url, card_path, False)]
            )
        except ImportError:
            # Fallback for very old cores.
            hass.http.register_static_path(url, card_path, False)

        try:
            from homeassistant.components.frontend import add_extra_js_url

            add_extra_js_url(hass, f"{url}?v={version}")
        except Exception:  # noqa: BLE001 - frontend not loaded; cards can be added manually
            pass


def _manifest_version() -> str:
    """Return the integration version from manifest.json for cache-busting."""
    import json

    try:
        path = os.path.join(os.path.dirname(__file__), "manifest.json")
        with open(path, encoding="utf-8") as fh:
            return str(json.load(fh).get("version", "0"))
    except Exception:  # noqa: BLE001
        return "0"
