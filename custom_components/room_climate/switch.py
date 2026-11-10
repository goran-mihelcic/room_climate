"""Room: In use, Override, Floor heating; bathroom: Heating on;
house: Automatic control, Debug log file."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

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
        async_add_entities([AutomaticSwitch(ctl), DebugLogSwitch(ctl)])
        return
    entities: list[SwitchEntity] = [InUseSwitch(ctl), OverrideSwitch(ctl)]
    if ctl.room_floor.entities:
        entities.append(FloorHeatingSwitch(ctl))
    if ctl.has_bathroom:
        entities.append(BathHeatingSwitch(ctl))
    async_add_entities(entities)


class AutomaticSwitch(RoomClimateMixin, SwitchEntity, RestoreEntity):
    """Room Climate manages the devices. Off: every device is left as it is."""

    _attr_icon = "mdi:robot"

    def __init__(self, ctl: HouseController) -> None:
        super().__init__(ctl, "automatic", "Automatic control")

    @property
    def is_on(self) -> bool:
        return self._ctl.automatic

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "off":
            self._ctl.values["automatic"] = False  # restored before anything is driven

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._ctl.set_automatic(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._ctl.set_automatic(False)
        self.async_write_ha_state()


class DebugLogSwitch(RoomClimateMixin, SwitchEntity, RestoreEntity):
    """Write everything Room Climate logs, at debug level, to its own file."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:file-document-outline"

    def __init__(self, ctl: HouseController) -> None:
        super().__init__(ctl, "debug_log", "Debug log file")

    @property
    def is_on(self) -> bool:
        return self._ctl.debug_log.active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"path": self._ctl.debug_log.path}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            await self._ctl.debug_log.async_enable()

    async def async_will_remove_from_hass(self) -> None:
        await self._ctl.debug_log.async_disable()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._ctl.debug_log.async_enable()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._ctl.debug_log.async_disable()
        self.async_write_ha_state()


class InUseSwitch(RoomClimateMixin, SwitchEntity, RestoreEntity):
    """Whether the room is occupied. Rooms not in use hold frost protection and never cool."""

    _attr_icon = "mdi:bed"

    def __init__(self, ctl: RoomController) -> None:
        super().__init__(ctl, "in_use", "In use")

    @property
    def is_on(self) -> bool:
        return bool(self._ctl.values["in_use"])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._ctl.values["in_use"] = last.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, value: bool) -> None:
        self._ctl.values["in_use"] = value
        self.async_write_ha_state()
        self._ctl.settings_changed()


class TimedSwitch(RoomClimateMixin, SwitchEntity, RestoreEntity):
    """On for the duration in `<minutes_key>` (0: until switched off), kept across restarts.

    Its state lives in ctl.values[key] and ctl.values[f"{key}_until"].
    """

    def __init__(
        self, ctl: RoomController, key: str, name: str, minutes_key: str, bathroom: bool = False
    ) -> None:
        super().__init__(ctl, key, name, bathroom)
        self._minutes_key = minutes_key
        self._until_key = f"{key}_until"
        self._unsub_expiry: CALLBACK_TYPE | None = None

    @property
    def is_on(self) -> bool:
        return bool(self._ctl.values[self._key])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        until = self._ctl.values[self._until_key]
        return {"until": until.isoformat() if until else None}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._ctl.end_timers[self._key] = self._end  # used by the Resume automatic button
        last = await self.async_get_last_state()
        if last is None or last.state != "on":
            return
        until_raw = last.attributes.get("until")
        until = dt_util.parse_datetime(until_raw) if until_raw else None
        if until is not None and until <= dt_util.utcnow():
            return  # expired while Home Assistant was down
        self._ctl.values[self._key] = True
        self._ctl.values[self._until_key] = until
        self._schedule_expiry(until)

    async def async_will_remove_from_hass(self) -> None:
        self._ctl.end_timers.pop(self._key, None)
        self._cancel_expiry()

    async def async_turn_on(self, **kwargs: Any) -> None:
        minutes = float(self._ctl.values.get(self._minutes_key) or 0)
        until = dt_util.utcnow() + timedelta(minutes=minutes) if minutes > 0 else None
        self._ctl.values[self._key] = True
        self._ctl.values[self._until_key] = until
        self._schedule_expiry(until)
        self.async_write_ha_state()
        self._ctl.settings_changed()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._end()

    @callback
    def _end(self, _now: datetime | None = None) -> None:
        self._cancel_expiry()
        self._ctl.values[self._key] = False
        self._ctl.values[self._until_key] = None
        self.async_write_ha_state()
        self._ctl.settings_changed()

    def _schedule_expiry(self, until: datetime | None) -> None:
        self._cancel_expiry()
        if until is not None:
            self._unsub_expiry = async_track_point_in_utc_time(self.hass, self._end, until)

    def _cancel_expiry(self) -> None:
        if self._unsub_expiry is not None:
            self._unsub_expiry()
            self._unsub_expiry = None


class OverrideSwitch(TimedSwitch):
    """Temporary override temperature; expires after the override duration."""

    _attr_icon = "mdi:hand-back-right"

    def __init__(self, ctl: RoomController) -> None:
        super().__init__(ctl, "override", "Override", "override_minutes")


class BathHeatingSwitch(TimedSwitch):
    """Bathroom heating outside its schedule, for the Heating on duration.

    Radiators at the bathroom comfort temperature while the room is heating; floor heating
    in any season but cooling.
    """

    _attr_icon = "mdi:heat-wave"

    def __init__(self, ctl: RoomController) -> None:
        super().__init__(ctl, "bath_on", "Heating on", "bath_on_minutes", bathroom=True)


class FloorHeatingSwitch(RoomClimateMixin, SwitchEntity, RestoreEntity):
    """The room's floor heating also heats outside a boost, whenever the room heats."""

    _attr_icon = "mdi:heating-coil"

    def __init__(self, ctl: RoomController) -> None:
        super().__init__(ctl, "floor_heating", "Floor heating")

    @property
    def is_on(self) -> bool:
        return bool(self._ctl.values["floor_heating"])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._ctl.values["floor_heating"] = last.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, value: bool) -> None:
        self._ctl.values["floor_heating"] = value
        self.async_write_ha_state()
        self._ctl.request_update()  # resets only the floor heating's manual state
