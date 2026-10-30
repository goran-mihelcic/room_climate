"""Season sensor for the house; target and status sensors for a room and its bathroom."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RoomClimateConfigEntry
from .const import (
    BATH_STATUSES,
    HOUSE_STATUSES,
    ROOM_STATUSES,
    SEASON_COMFORT,
    SEASON_COOL,
    SEASON_FROST,
    SEASON_HEAT,
)
from .controller import RoomController
from .entity import RoomClimateMixin
from .house import HouseController

SEASONS = (SEASON_HEAT, SEASON_COOL, SEASON_FROST, SEASON_COMFORT)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomClimateConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    ctl = entry.runtime_data
    if isinstance(ctl, HouseController):
        async_add_entities([SeasonSensor(ctl)])
        return
    entities: list[SensorEntity] = [
        TargetSensor(ctl, "target", "Target temperature", bathroom=False),
        StatusSensor(ctl, "status", "Status", bathroom=False),
    ]
    if ctl.has_bathroom:
        entities += [
            TargetSensor(ctl, "bath_target", "Target temperature", bathroom=True),
            StatusSensor(ctl, "bath_status", "Status", bathroom=True),
        ]
    async_add_entities(entities)


class SeasonSensor(RoomClimateMixin, SensorEntity, RestoreEntity):
    """The HVAC season as the integration understands it, plus the floor thermostats."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = HOUSE_STATUSES
    _attr_translation_key = "house_season"
    _attr_icon = "mdi:home-thermometer"

    def __init__(self, ctl: HouseController) -> None:
        super().__init__(ctl, "season", "Season")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # A computed season carries on after a restart, so hysteresis and delay keep
        # applying instead of the season being worked out from scratch.
        last = await self.async_get_last_state()
        if self._ctl.computes_season and last is not None and last.state in SEASONS:
            self._ctl.season = last.state
            self._ctl.indoor_started = last.attributes.get("started_by") == "indoor"
        self.async_on_remove(self._ctl.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str:
        return self._ctl.read_season() or "waiting"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._ctl.attributes()


class _ControllerSensor(RoomClimateMixin, SensorEntity):
    def __init__(self, ctl: RoomController, key: str, name: str, bathroom: bool) -> None:
        super().__init__(ctl, key, name, bathroom)
        self._bathroom = bathroom

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._ctl.async_add_listener(self.async_write_ha_state))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._ctl.bath_attributes() if self._bathroom else self._ctl.room_attributes()


class TargetSensor(_ControllerSensor):
    """The temperature the room (or bathroom) is being driven to."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermostat"

    @property
    def native_value(self) -> float | None:
        return self._ctl.bath_target if self._bathroom else self._ctl.room_target


class StatusSensor(_ControllerSensor):
    """What the room (or bathroom) is doing right now."""

    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, ctl: RoomController, key: str, name: str, bathroom: bool) -> None:
        super().__init__(ctl, key, name, bathroom)
        self._attr_translation_key = "bath_status" if bathroom else "room_status"
        self._attr_options = BATH_STATUSES if bathroom else ROOM_STATUSES

    @property
    def native_value(self) -> str:
        return self._ctl.bath_status if self._bathroom else self._ctl.room_status

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return None  # attributes live on the target sensor
