"""Room Climate: a house device plus one device per room that drives its climate devices."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_ENTRY_TYPE, TYPE_HOUSE
from .controller import RoomController
from .house import HouseController

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.SENSOR,
]

type RoomClimateConfigEntry = ConfigEntry[RoomController | HouseController]


async def async_setup_entry(hass: HomeAssistant, entry: RoomClimateConfigEntry) -> bool:
    """Set up the house or one room."""
    if entry.data.get(CONF_ENTRY_TYPE) == TYPE_HOUSE:
        controller: RoomController | HouseController = HouseController(hass, entry)
    else:
        controller = RoomController(hass, entry)
    entry.runtime_data = controller
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RoomClimateConfigEntry) -> bool:
    """Unload the house or a room."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: RoomClimateConfigEntry) -> bool:
    """Rooms from 0.2.x have no house entry and different settings: they must be re-added."""
    if entry.version < 2:
        _LOGGER.error(
            "Room Climate entry '%s' is from version 0.2 and cannot be migrated. "
            "Delete it, add the House, then add the room again",
            entry.title,
        )
        return False
    return True


async def _async_options_updated(hass: HomeAssistant, entry: RoomClimateConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
