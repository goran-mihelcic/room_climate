"""Room Climate: a house device plus one device per room that drives its climate devices."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_ENTRY_TYPE, DOMAIN, TYPE_HOUSE
from .controller import RoomController
from .house import HouseController

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
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
    _async_register_devices(hass, entry, controller)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


def _async_register_devices(
    hass: HomeAssistant, entry: RoomClimateConfigEntry, controller: RoomController | HouseController
) -> None:
    """Create the devices up front so the bathroom can be linked to its room.

    `via_device` in DeviceInfo is deprecated from HA 2026.8 and `via_device_id` is not
    accepted on creation before that, so the link is set with an update, which every
    supported version accepts.
    """
    devices = dr.async_get(hass)
    main = devices.async_get_or_create(config_entry_id=entry.entry_id, **controller.device_info)
    bath_id = (DOMAIN, f"{entry.entry_id}_bath")
    if isinstance(controller, RoomController) and controller.has_bathroom:
        bath = devices.async_get_or_create(
            config_entry_id=entry.entry_id, **controller.bath_device_info
        )
        if bath.via_device_id != main.id:
            devices.async_update_device(bath.id, via_device_id=main.id)
        return
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if bath_id in device.identifiers:  # bathroom removed in Configure
            devices.async_remove_device(device.id)


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
