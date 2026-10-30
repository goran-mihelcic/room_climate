"""Resume automatic button for a room."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RoomClimateConfigEntry
from .controller import RoomController
from .entity import RoomClimateMixin
from .house import HouseController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomClimateConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    ctl = entry.runtime_data
    if isinstance(ctl, HouseController):
        return
    async_add_entities([ResumeButton(ctl)])


class ResumeButton(RoomClimateMixin, ButtonEntity):
    """Drop manual changes and override: the room follows its schedule again."""

    _attr_icon = "mdi:calendar-sync"

    def __init__(self, ctl: RoomController) -> None:
        super().__init__(ctl, "resume", "Resume automatic")

    async def async_press(self) -> None:
        self._ctl.resume_automatic()
