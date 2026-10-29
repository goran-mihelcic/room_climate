"""Setpoints and tuning values for the house and each room."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RoomClimateConfigEntry
from .const import BATH_NUMBERS, CONFIG_NUMBERS, HOUSE_NUMBERS, ROOM_NUMBERS
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
        async_add_entities(
            [RoomNumber(ctl, key, spec, False) for key, spec in HOUSE_NUMBERS.items()]
        )
        return
    entities = [RoomNumber(ctl, key, spec, False) for key, spec in ROOM_NUMBERS.items()]
    if ctl.has_bathroom:
        entities += [RoomNumber(ctl, key, spec, True) for key, spec in BATH_NUMBERS.items()]
    async_add_entities(entities)


class RoomNumber(RoomClimateMixin, RestoreNumber):
    """A value stored by the integration and restored after restarts."""

    _attr_mode = NumberMode.BOX

    def __init__(
        self, ctl: RoomController | HouseController, key: str, spec: tuple, bathroom: bool
    ) -> None:
        name, low, high, step, default, unit = spec
        super().__init__(ctl, key, name, bathroom)
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_native_value = ctl.values.get(key, default)
        if key in CONFIG_NUMBERS:
            self._attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            value = min(max(last.native_value, self.native_min_value), self.native_max_value)
            self._attr_native_value = value
        self._ctl.values[self._key] = self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._ctl.values[self._key] = value
        self.async_write_ha_state()
        self._ctl.settings_changed()
