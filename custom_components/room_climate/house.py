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
from homeassistant.util import dt as dt_util

from .const import (
    AWAY_STATES,
    CONF_AT_HOME,
    CONF_HEAT_PUMP,
    CONF_NOTIFY,
    CONF_OUTDOOR,
    CONF_PREPARE,
    CONF_REFERENCE,
    CONF_SEASON,
    DOMAIN,
    HOUSE_NUMBERS,
    ON_STATES,
    SEASON_COMFORT,
    SEASON_COOL,
    SEASON_FROST,
    SEASON_HEAT,
    SEASON_MAP,
    UPDATE_INTERVAL_SECONDS,
)
from .debug_log import DebugFileLog
from .driver import KIND_HEAT, ClimateDriver, DeviceGroup, to_float

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
        # Computed season (with an outdoor sensor)
        self.season: str | None = None
        self.preheat: bool = False
        self.outdoor: float | None = None
        self.reference: float | None = None
        self.pending: tuple[str, datetime] | None = None  # (next season, wanted since)
        self._last_at_home: bool | None = None
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
        inputs = (CONF_AT_HOME, CONF_SEASON, CONF_PREPARE, CONF_OUTDOOR, CONF_REFERENCE)
        watched = [opts[k] for k in inputs if opts.get(k)]

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
            if self._evaluate_season():
                self._rooms_update()
            self._notify()
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
        self._evaluate_season()
        self._notify()
        self._rooms_update()

    @callback
    def _rooms_update(self) -> None:
        for room in get_data(self.hass).rooms.values():
            room.request_update()

    # ------------------------------------------------------------------
    # Season, computed from the outdoor and reference (indoor) temperatures
    # ------------------------------------------------------------------
    @property
    def computes_season(self) -> bool:
        """With an outdoor sensor the house computes the season; before 0.4 it was read."""
        return bool(self.entry.options.get(CONF_OUTDOOR))

    def _read_temperature(self, key: str) -> float | None:
        entity_id = self.entry.options.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        return to_float(state.state) if state else None

    def _season_for(
        self, current: str | None, outdoor: float, reference: float | None, at_home: bool
    ) -> str:
        """The season the temperatures ask for, in priority order, with hysteresis.

        A season that is already on keeps going until its temperature is the hysteresis
        back on the other side of its threshold.
        """
        v = self.values

        def above(value: float | None, threshold: float, season: str) -> bool:
            margin = v["season_hysteresis"] if current == season else 0.0
            return value is not None and value > threshold - margin

        def below(value: float | None, threshold: float, season: str) -> bool:
            margin = v["season_hysteresis"] if current == season else 0.0
            return value is not None and value < threshold + margin

        if above(outdoor, v["cool_season"], SEASON_COOL) or above(
            reference, v["cool_indoor"], SEASON_COOL
        ):
            return SEASON_COOL
        if at_home and (
            below(outdoor, v["heat_season"], SEASON_HEAT)
            or below(reference, v["heat_indoor"], SEASON_HEAT)
        ):
            return SEASON_HEAT
        frost_margin = v["season_hysteresis"] if current == SEASON_FROST else 0.0
        if not at_home and outdoor <= v["frost_season"] + frost_margin:
            return SEASON_FROST
        return SEASON_COMFORT

    @callback
    def _evaluate_season(self) -> bool:
        """Update the computed season and pre-heat. True if either changed."""
        if not self.computes_season:
            return False
        v = self.values
        now = dt_util.utcnow()
        self.outdoor = self._read_temperature(CONF_OUTDOOR)
        self.reference = self._read_temperature(CONF_REFERENCE)
        at_home, prepare = self.read_at_home(), self.read_prepare()
        presence_changed = self._last_at_home is not None and at_home != self._last_at_home
        self._last_at_home = at_home

        current = self.season
        wanted = current  # an unavailable outdoor sensor holds the season
        if self.outdoor is not None:
            wanted = self._season_for(current, self.outdoor, self.reference, at_home)
        season = current
        if wanted == current:
            self.pending = None
        elif current is None or presence_changed or v["season_delay"] <= 0:
            season, self.pending = wanted, None  # start-up and presence act at once
        elif self.pending is None or self.pending[0] != wanted:
            self.pending = (wanted, now)
        elif now - self.pending[1] >= timedelta(minutes=v["season_delay"]):
            season, self.pending = wanted, None

        # Prepare house while away in heating weather: rooms in use pre-heat.
        heating_weather = self.outdoor is not None and self.outdoor < v["heat_season"]
        preheat = (
            prepare
            and not at_home
            and (season == SEASON_FROST or (season == SEASON_COMFORT and heating_weather))
        )

        was_heating = self.season in (SEASON_HEAT, SEASON_FROST) or self.preheat
        changed = (season, preheat) != (self.season, self.preheat)
        self.season, self.preheat = season, preheat
        if changed:
            _LOGGER.info(
                "%s: season %s%s (outdoor %s, reference %s, at home %s)",
                self.entry.title,
                season,
                ", pre-heat" if preheat else "",
                self.outdoor,
                self.reference,
                at_home,
            )
        if (season in (SEASON_HEAT, SEASON_FROST) or preheat) and not was_heating:
            self.hass.async_create_task(self._async_heat_pump_on())
        return changed

    async def _async_heat_pump_on(self) -> None:
        """Heating starts: switch the heat pump on. It is never switched off (hot water)."""
        entity_id = self.entry.options.get(CONF_HEAT_PUMP)
        if not entity_id:
            return
        domain = entity_id.split(".", 1)[0]
        _LOGGER.info("%s: heating starts, turning on %s", self.entry.title, entity_id)
        try:
            await self.hass.services.async_call(
                domain, "turn_on", {"entity_id": entity_id}, blocking=True
            )
        except Exception as err:  # a missing or broken heat pump must not stop the house
            _LOGGER.warning("%s: could not turn on %s: %s", self.entry.title, entity_id, err)

    # ------------------------------------------------------------------
    # Inputs, read live so rooms never see a stale value
    # ------------------------------------------------------------------
    def notify_targets(self) -> list[str]:
        return list(self.entry.options.get(CONF_NOTIFY) or [])

    def read_season(self) -> str | None:
        """The season, or None while it is not known yet (rooms then wait)."""
        if self.computes_season:
            return self.season
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
        common = {
            "at_home": self.read_at_home(),
            "prepare_house": self.read_prepare(),
            "floor_thermostats": dict(self.pump_targets),
        }
        if self.computes_season:
            return {
                "outdoor_temperature": self.outdoor,
                "reference_temperature": self.reference,
                "next_season": self.pending[0] if self.pending else None,
                "next_season_since": self.pending[1].isoformat() if self.pending else None,
                "preheat": self.preheat,
                **common,
            }
        entity_id = self.entry.options.get(CONF_SEASON)
        state = self.hass.states.get(entity_id) if entity_id else None
        return {
            "season_entity": entity_id,
            "season_entity_state": state.state if state else None,
            **common,
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
