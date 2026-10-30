"""House level: season, presence, global settings and the floor (pump) thermostats."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .const import (
    AWAY_STATES,
    CONF_AT_HOME,
    CONF_NOTIFY,
    CONF_PREPARE,
    CONF_SEASON,
    DOMAIN,
    HOUSE_NUMBERS,
    ON_STATES,
    SEASON_MAP,
    UPDATE_INTERVAL_SECONDS,
)
from .debug_log import DebugFileLog
from .driver import KIND_HEAT, ClimateDriver, DeviceGroup

if TYPE_CHECKING:
    from .controller import RoomController

_LOGGER = logging.getLogger(__name__)


@dataclass
class DomainData:
    """Shared between config entries: the house (if set up) and every room."""

    house: HouseController | None = None
    rooms: dict[str, RoomController] = field(default_factory=dict)


def get_data(hass: HomeAssistant) -> DomainData:
    return hass.data.setdefault(DOMAIN, DomainData())


class HouseController:
    """Reads the house inputs for the rooms and drives the floor thermostats."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.values: dict[str, Any] = {k: v[4] for k, v in HOUSE_NUMBERS.items()}
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Room Climate",
            model="House controller",
        )
        self.driver = ClimateDriver(hass, entry.title, self.notify_targets)
        self.debug_log = DebugFileLog(hass)
        self.pumps = DeviceGroup("pumps", KIND_HEAT, [])
        # room entry_id -> {floor thermostat: highest radiator setpoint, or None}
        self._demands: dict[str, dict[str, float | None]] = {}
        self.pump_targets: dict[str, float | None] = {}
        self._listeners: list[CALLBACK_TYPE] = []
        self._running = False
        self._pump_debouncer = Debouncer(
            hass, _LOGGER, cooldown=2.0, immediate=True, function=self._async_drive_pumps
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        data = get_data(self.hass)
        data.house = self
        opts = self.entry.options
        watched = [opts[k] for k in (CONF_AT_HOME, CONF_SEASON, CONF_PREPARE) if opts.get(k)]

        @callback
        def _inputs_changed(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            _LOGGER.debug(
                "%s: %s is now %s",
                self.entry.title,
                event.data["entity_id"],
                new_state.state if new_state else None,
            )
            self.settings_changed()

        @callback
        def _tick(_now: datetime) -> None:
            self._schedule_pumps()

        @callback
        def _started(_hass: HomeAssistant) -> None:
            self._running = True
            self.entry.async_on_unload(
                async_track_state_change_event(self.hass, watched, _inputs_changed)
            )
            self.entry.async_on_unload(
                async_track_time_interval(
                    self.hass, _tick, timedelta(seconds=UPDATE_INTERVAL_SECONDS)
                )
            )
            self.settings_changed()

        @callback
        def _stop() -> None:
            self._running = False
            self._pump_debouncer.async_shutdown()
            if data.house is self:
                data.house = None
            for room in data.rooms.values():
                room.request_update()

        self.entry.async_on_unload(async_at_started(self.hass, _started))
        self.entry.async_on_unload(_stop)

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(update_callback)

        def _remove() -> None:
            self._listeners.remove(update_callback)

        return _remove

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    @callback
    def settings_changed(self) -> None:
        """An input or a setting on the house device changed: every room re-plans."""
        self._notify()
        for room in get_data(self.hass).rooms.values():
            room.request_update()

    # ------------------------------------------------------------------
    # Inputs, read live so rooms never see a stale value
    # ------------------------------------------------------------------
    def notify_targets(self) -> list[str]:
        return list(self.entry.options.get(CONF_NOTIFY) or [])

    def read_season(self) -> str | None:
        """Mapped season, or None while the season entity is unavailable or unknown."""
        entity_id = self.entry.options.get(CONF_SEASON)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        season = SEASON_MAP.get(state.state.strip().lower())
        if season is None:
            _LOGGER.warning("%s: unknown HVAC season '%s'", self.entry.title, state.state)
        return season

    def read_at_home(self) -> bool:
        entity_id = self.entry.options.get(CONF_AT_HOME)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None:
            return True
        return state.state.lower() not in AWAY_STATES  # unknown/unavailable -> assume home

    def read_prepare(self) -> bool:
        entity_id = self.entry.options.get(CONF_PREPARE)
        state = self.hass.states.get(entity_id) if entity_id else None
        return state is not None and state.state.lower() in ON_STATES

    def attributes(self) -> dict[str, Any]:
        entity_id = self.entry.options.get(CONF_SEASON)
        state = self.hass.states.get(entity_id) if entity_id else None
        return {
            "season_entity": entity_id,
            "season_entity_state": state.state if state else None,
            "at_home": self.read_at_home(),
            "prepare_house": self.read_prepare(),
            "floor_thermostats": dict(self.pump_targets),
        }

    # ------------------------------------------------------------------
    # Floor thermostats: each runs its pump while any radiator on it wants heat
    # ------------------------------------------------------------------
    @callback
    def set_pump_demand(self, room_id: str, demand: dict[str, float | None] | None) -> None:
        """A room reports its highest radiator setpoint per floor thermostat (None: no opinion)."""
        if demand is None:
            if self._demands.pop(room_id, None) is None:
                return
        elif self._demands.get(room_id) == demand:
            return
        else:
            self._demands[room_id] = demand
        self._schedule_pumps()

    @callback
    def _schedule_pumps(self) -> None:
        if self._running:
            self._pump_debouncer.async_schedule_call()

    async def _async_drive_pumps(self) -> None:
        targets: dict[str, float | None] = {}
        for demand in self._demands.values():
            for thermostat, setpoint in demand.items():
                current = targets.get(thermostat)
                if setpoint is not None and (current is None or setpoint > current):
                    targets[thermostat] = setpoint
                else:
                    targets.setdefault(thermostat, current)
        if targets != self.pump_targets:
            _LOGGER.debug("%s: floor thermostats %s", self.entry.title, targets)
        self.pump_targets = targets
        self.pumps.entities = list(targets)
        for thermostat, setpoint in targets.items():
            mode = "off" if setpoint is None else "heat"
            await self.driver.async_drive(self.pumps, thermostat, mode, setpoint)
        self._notify()
