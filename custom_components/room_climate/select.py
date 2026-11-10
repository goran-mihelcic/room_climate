"""Boost method selector (rooms with more than one way to boost: radiators, A/C, floor)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RoomClimateConfigEntry
from .const import BOOST_PARTS
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
    if len(ctl.boost_methods) > 1:  # radiators alone: the only method is raising them
        async_add_entities([BoostMethodSelect(ctl)])


class BoostMethodSelect(RoomClimateMixin, SelectEntity, RestoreEntity):
    """How the room boosts: radiators, A/C in heat mode, floor heating, or a combination."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:rocket-launch"
    _attr_translation_key = "boost_method"

    def __init__(self, ctl: RoomController) -> None:
        super().__init__(ctl, "boost_method", "Boost method")
        self._attr_options = ctl.boost_methods

    @property
    def current_option(self) -> str:
        return self._ctl.boost_method

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in BOOST_PARTS:
            self._ctl.values["boost_method"] = last.state

    async def async_select_option(self, option: str) -> None:
        self._ctl.values["boost_method"] = option
        self.async_write_ha_state()
        self._ctl.settings_changed()
