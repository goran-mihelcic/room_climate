"""Time windows: room comfort period and bathroom morning/evening periods."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RoomClimateConfigEntry
from .const import BATH_TIMES, ROOM_TIMES
from .controller import RoomController
from .entity import RoomClimateMixin


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomClimateConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    ctl = entry.runtime_data
    entities = [RoomTime(ctl, key, name, False) for key, (name, _d) in ROOM_TIMES.items()]
    if ctl.has_bathroom:
        entities += [RoomTime(ctl, key, name, True) for key, (name, _d) in BATH_TIMES.items()]
    async_add_entities(entities)


class RoomTime(RoomClimateMixin, TimeEntity, RestoreEntity):
    """A start or end time, restored after restarts."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, ctl: RoomController, key: str, name: str, bathroom: bool) -> None:
        super().__init__(ctl, key, name, bathroom)
        self._attr_native_value = ctl.values[key]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                self._attr_native_value = time.fromisoformat(last.state)
            except ValueError:
                pass
        self._ctl.values[self._key] = self._attr_native_value

    async def async_set_value(self, value: time) -> None:
        self._attr_native_value = value
        self._ctl.values[self._key] = value
        self.async_write_ha_state()
        self._ctl.settings_changed()
