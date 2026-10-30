"""House level: season, presence, global settings and the floor (pump) thermostats."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .command_queue import async_queued_call
from .const import (
    AWAY_STATES,
    CONF_AT_HOME,
    CONF_HEAT_PUMP,
    CONF_HOT_WATER,
    CONF_NOTIFY,
    CONF_OUTDOOR,
    CONF_PREPARE,
    CONF_REFERENCE,
    CONF_SEASON,
    CONF_TEMP_SENSOR,
    DEFAULT_HEAT_PUMP_RESPONSE_SECONDS,
    DOMAIN,
    HEAT_PUMP_NUMBERS,
    HOT_WATER_NUMBERS,
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
from .driver import AC_MODE_ATTEMPTS, KIND_HEAT, ClimateDriver, DeviceGroup, to_float

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


@dataclass
class _Kept:
    """What the heat pump or hot water was last told, and when it must show it by."""

    on: bool | None = None
    temp: float | None = None
    stage: str = "on"  # "on": waiting for on/off; "temp": for the temperature
    deadline: datetime | None = None
    attempt: int = 0

    def reset(self) -> None:
        self.stage, self.deadline, self.attempt = "on", None, 0


class HouseController:
    """Reads the house inputs for the rooms and drives the floor thermostats."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.values: dict[str, Any] = {k: v[4] for k, v in HOUSE_NUMBERS.items()}
        if entry.options.get(CONF_HEAT_PUMP) or entry.options.get(CONF_HOT_WATER):
            self.values.update({k: v[4] for k, v in HEAT_PUMP_NUMBERS.items()})
        if entry.options.get(CONF_HOT_WATER):
            self.values.update({k: v[4] for k, v in HOT_WATER_NUMBERS.items()})
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Room Climate",
            model="House controller",
        )
        self.driver = ClimateDriver(
            hass,
            entry.title,
            self.notify_targets,
            command_pause=lambda: float(self.values["command_pause"]),
        )
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
        self.indoor_started = False  # the current season was started by the indoor temperature
        # Heat pump and hot water: kept on (heating) or off and checked, like the A/Cs
        self._kept_lock = asyncio.Lock()
        self._kept = {CONF_HEAT_PUMP: _Kept(), CONF_HOT_WATER: _Kept()}
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
            self._check_heat_pump()

        @callback
        def _heat_pump_changed(_event: Event[EventStateChangedData]) -> None:
            self._check_heat_pump()  # switched off while heating is needed: on again

        @callback
        def _started(_hass: HomeAssistant) -> None:
            self._running = True
            self.entry.async_on_unload(
                async_track_state_change_event(self.hass, watched, _inputs_changed)
            )
            if kept := [opts[k] for k in (CONF_HEAT_PUMP, CONF_HOT_WATER) if opts.get(k)]:
                self.entry.async_on_unload(
                    async_track_state_change_event(self.hass, kept, _heat_pump_changed)
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
        self._check_heat_pump()

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

    def _reference_room_target(self) -> float | None:
        """The current target of the room whose temperature sensor is the reference."""
        reference = self.entry.options.get(CONF_REFERENCE)
        for room in get_data(self.hass).rooms.values():
            if reference and room.entry.options.get(CONF_TEMP_SENSOR) == reference:
                return room.room_target
        return None

    def _season_for(
        self, current: str | None, outdoor: float, reference: float | None, at_home: bool
    ) -> tuple[str, bool]:
        """The season the temperatures ask for, in priority order, and whether only the
        indoor (reference) temperature asks for it.

        A season that is already on keeps going until its temperature is the hysteresis
        back on the other side of its threshold. Heating or cooling that the indoor
        temperature started keeps going until the reference room reaches its own target.
        """
        v = self.values
        h = v["season_hysteresis"]
        target = self._reference_room_target()

        def above(value: float | None, threshold: float, season: str) -> bool:
            margin = h if current == season else 0.0
            return value is not None and value > threshold - margin

        def below(value: float | None, threshold: float, season: str) -> bool:
            margin = h if current == season else 0.0
            return value is not None and value < threshold + margin

        cool_out = above(outdoor, v["cool_season"], SEASON_COOL)
        if current == SEASON_COOL and self.indoor_started:
            stop = v["cool_indoor"] - h if target is None else min(v["cool_indoor"] - h, target)
            cool_in = reference is not None and reference > stop
        else:
            cool_in = above(reference, v["cool_indoor"], SEASON_COOL)
        if cool_out or cool_in:
            return SEASON_COOL, not cool_out

        heat_out = below(outdoor, v["heat_season"], SEASON_HEAT)
        if current == SEASON_HEAT and self.indoor_started:
            stop = v["heat_indoor"] + h if target is None else max(v["heat_indoor"] + h, target)
            heat_in = reference is not None and reference < stop
        else:
            heat_in = below(reference, v["heat_indoor"], SEASON_HEAT)
        if at_home and (heat_out or heat_in):
            return SEASON_HEAT, not heat_out

        frost_margin = h if current == SEASON_FROST else 0.0
        if not at_home and outdoor <= v["frost_season"] + frost_margin:
            return SEASON_FROST, False
        return SEASON_COMFORT, False

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
        wanted, indoor = current, self.indoor_started  # unavailable outdoor holds the season
        if self.outdoor is not None:
            wanted, indoor = self._season_for(current, self.outdoor, self.reference, at_home)
        season = current
        if wanted == current:
            self.pending = None
        elif current is None or presence_changed or v["season_delay"] <= 0:
            season, self.pending = wanted, None  # start-up and presence act at once
        elif self.pending is None or self.pending[0] != wanted:
            self.pending = (wanted, now)
        elif now - self.pending[1] >= timedelta(minutes=v["season_delay"]):
            season, self.pending = wanted, None
        if season != current:
            # Remembered for the whole season: started by the indoor temperature, it runs
            # until the reference room reaches its target.
            self.indoor_started = indoor

        # Prepare house while away in heating weather: rooms in use pre-heat.
        heating_weather = self.outdoor is not None and self.outdoor < v["heat_season"]
        preheat = (
            prepare
            and not at_home
            and (season == SEASON_FROST or (season == SEASON_COMFORT and heating_weather))
        )

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
        return changed

    # ------------------------------------------------------------------
    # Heat pump (heating while heating is needed, off otherwise) and hot water (on at its
    # temperature while someone is home or the house is being prepared, off otherwise)
    # ------------------------------------------------------------------
    @property
    def heating_needed(self) -> bool:
        return self.read_season() in (SEASON_HEAT, SEASON_FROST) or self.preheat

    @callback
    def _check_heat_pump(self) -> None:
        opts = self.entry.options
        if self._running and (opts.get(CONF_HEAT_PUMP) or opts.get(CONF_HOT_WATER)):
            self.hass.async_create_task(self._async_check_kept())

    def _kept_wanted(self, key: str) -> tuple[bool, float | None] | None:
        """(on, temperature) the device should have, or None to leave it alone."""
        if key == CONF_HEAT_PUMP:
            if self.read_season() is None:
                return None  # season not known yet
            return self.heating_needed, None
        on = self.read_at_home() or self.read_prepare()
        return on, float(self.values["hot_water_temp"]) if on else None

    @staticmethod
    def _wanted_state(state: State, on: bool) -> str | None:
        """The state that shows the device followed; None: anything but off."""
        if not on:
            return "off"
        if state.domain == "climate" and "heat" in (state.attributes.get("hvac_modes") or []):
            return "heat"  # not just "on": turn_on could restore an old cool mode
        return None

    async def _async_check_kept(self) -> None:
        """Keep the heat pump and the hot water as they should be, checking they followed.

        Like the A/Cs, a command is sent once and given the heat pump response time to show
        up (LG polls its cloud and refuses commands while its app is in use). Then it is
        sent again; after AC_MODE_ATTEMPTS misses the notify services are told once. A
        change made by hand the other way is undone. On first, the temperature once the
        device reports on.
        """
        async with self._kept_lock:  # also keeps the two from sending at the same time
            for key, kept in self._kept.items():
                if entity_id := self.entry.options.get(key):
                    await self._async_keep(entity_id, kept, self._kept_wanted(key))

    async def _async_keep(
        self, entity_id: str, kept: _Kept, wanted: tuple[bool, float | None] | None
    ) -> None:
        if wanted is None:
            return
        on, temp = wanted
        if (on, temp) != (kept.on, kept.temp):
            kept.on, kept.temp = on, temp  # a new instruction: act at once
            kept.reset()
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        target = self._wanted_state(state, on)
        mode_ok = state.state == target or (target is None and state.state != "off")
        current = to_float(state.attributes.get("temperature"))
        temp_ok = temp is None or (current is not None and abs(current - temp) < 0.05)
        if mode_ok and temp_ok:
            kept.reset()
            self.driver.failure_cleared(entity_id)
            return

        now = dt_util.utcnow()
        on_arrived = mode_ok and kept.stage == "on" and kept.deadline is not None
        if kept.deadline is not None and now < kept.deadline and not on_arrived:
            return  # still waiting for it to report the change
        label = (target or "on") if temp is None else f"{target or 'on'} {temp:g} °C"
        if on_arrived:
            kept.attempt = 0  # on was reported: the temperature gets its own attempts
        elif kept.attempt >= AC_MODE_ATTEMPTS:
            await self.driver.async_report_failure(entity_id, label)
            kept.attempt = 0  # and keep trying
        elif kept.attempt:
            _LOGGER.warning(
                "%s: %s is %s %s, not %s (attempt %d)",
                self.entry.title,
                entity_id,
                state.state,
                current,
                label,
                kept.attempt,
            )
        kept.attempt += 1

        domain = state.domain
        if not mode_ok:
            kept.stage = "on"
            if target in ("heat", "off") and domain == "climate":
                service, data = "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": target}
            else:
                service, data = ("turn_on" if on else "turn_off"), {"entity_id": entity_id}
        else:
            kept.stage = "temp"
            service, data = "set_temperature", {"entity_id": entity_id, "temperature": temp}
        _LOGGER.info("%s: %s.%s %s", self.entry.title, domain, service, data)
        try:
            await async_queued_call(
                self.hass, entity_id, domain, service, data, float(self.values["command_pause"])
            )
        except Exception as err:  # a missing or broken device must not stop the house
            _LOGGER.warning("%s: %s.%s failed: %s", self.entry.title, domain, service, err)
        kept.deadline = now + timedelta(
            seconds=self.values.get("heat_pump_response", DEFAULT_HEAT_PUMP_RESPONSE_SECONDS)
        )
        # A fast device reports straight away: continue with the temperature now.
        if kept.stage == "on" and temp is not None:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in ("off", STATE_UNAVAILABLE, STATE_UNKNOWN):
                await self._async_keep(entity_id, kept, (on, temp))

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
                "started_by": (
                    None if self.season is None else "indoor" if self.indoor_started else "outdoor"
                ),
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
