"""Room Climate: one device per room that drives heaters, coolers and a bathroom."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .controller import RoomController

PLATFORMS: list[Platform] = [Platform.NUMBER, Platform.TIME, Platform.SWITCH, Platform.SENSOR]

type RoomClimateConfigEntry = ConfigEntry[RoomController]


async def async_setup_entry(hass: HomeAssistant, entry: RoomClimateConfigEntry) -> bool:
    """Set up one room."""
    controller = RoomController(hass, entry)
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RoomClimateConfigEntry) -> bool:
    """Unload a room."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: RoomClimateConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
