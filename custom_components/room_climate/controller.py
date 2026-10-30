"""Room logic: decides what every managed climate device should do."""

from __future__ import annotations

import logging
import math
import time as monotonic_time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started

from .const import (
    AC_NUMBERS,
    BATH_NUMBERS,
    BOOST_BOTH,
    BOOST_COOLERS,
    BOOST_HEATERS,
    COMMAND_GRACE_SECONDS,
    CONF_BATH_FLOOR,
    CONF_BATH_HEATERS,
    CONF_BATH_SCHEDULE,
    CONF_COOL_MAX,
    CONF_COOL_MIN,
    CONF_COOLERS,
    CONF_FLOOR_THERMOSTAT,
    CONF_HAS_BATHROOM,
    CONF_HEAT_MAX,
    CONF_HEAT_MIN,
    CONF_HEATERS,
    CONF_HUMIDITY_SENSOR,
    CONF_MANUAL_POLICY,
    CONF_SCHEDULE,
    CONF_TEMP_SENSOR,
    CORRECTION_STEP,
    DEFAULT_AC_RESPONSE_SECONDS,
    DEFAULT_COMMAND_PAUSE_SECONDS,
    DEFAULT_COOL_MAX,
    DEFAULT_COOL_MIN,
    DEFAULT_HEAT_MAX,
    DEFAULT_HEAT_MIN,
    DOMAIN,
    ON_STATES,
    POLICY_FOLLOW,
    REGIME_COOL,
    REGIME_FROST,
    REGIME_HEAT,
    REGIME_OFF,
    REGIME_PREHEAT,
    ROOM_NUMBERS,
    SEASON_COOL,
    SEASON_FROST,
    SEASON_HEAT,
    UPDATE_INTERVAL_SECONDS,
)
from .driver import KIND_COOL, KIND_FLOOR, KIND_HEAT, ClimateDriver, DeviceGroup, to_float
from .house import HouseController, get_data

_LOGGER = logging.getLogger(__name__)


class RoomController:
    """Owns the state of one room (and its optional bathroom)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        opts = entry.options

        self.policy: str = opts.get(CONF_MANUAL_POLICY, POLICY_FOLLOW)
        self.limits = {
            KIND_HEAT: (
                opts.get(CONF_HEAT_MIN, DEFAULT_HEAT_MIN),
                opts.get(CONF_HEAT_MAX, DEFAULT_HEAT_MAX),
            ),
            KIND_COOL: (
                opts.get(CONF_COOL_MIN, DEFAULT_COOL_MIN),
                opts.get(CONF_COOL_MAX, DEFAULT_COOL_MAX),
            ),
        }
        self.has_bathroom: bool = bool(opts.get(CONF_HAS_BATHROOM)) and bool(
            opts.get(CONF_BATH_HEATERS) or opts.get(CONF_BATH_FLOOR)
        )
        self.thermostat: str | None = opts.get(CONF_FLOOR_THERMOSTAT) or None

        self.heaters = DeviceGroup("heaters", KIND_HEAT, list(opts.get(CONF_HEATERS, [])))
        self.coolers = DeviceGroup("coolers", KIND_COOL, list(opts.get(CONF_COOLERS, [])), ac=True)
        self.bath_heaters = DeviceGroup(
            "bath_heaters",
            KIND_HEAT,
            list(opts.get(CONF_BATH_HEATERS, []) if self.has_bathroom else []),
        )
        floor = opts.get(CONF_BATH_FLOOR) if self.has_bathroom else None
        self.floor = DeviceGroup("floor", KIND_FLOOR, [floor] if floor else [])
        self.groups = [self.heaters, self.coolers, self.bath_heaters, self.floor]
        self._entity_group: dict[str, DeviceGroup] = {}
        for group in self.groups:
            for entity_id in group.entities:
                self._entity_group[entity_id] = group

        # Values owned by this integration's own entities (restored on start).
        self.values: dict[str, Any] = {k: v[4] for k, v in ROOM_NUMBERS.items()}
        self.values.update({"in_use": True, "override": False, "override_until": None})
        self.values["boost_method"] = BOOST_HEATERS
        if self.has_bathroom:
            self.values.update({k: v[4] for k, v in BATH_NUMBERS.items()})
        if self.coolers.entities:
            self.values.update({k: v[4] for k, v in AC_NUMBERS.items()})

        # Computed state, read by the sensors.
        self.season: str | None = None
        self.at_home: bool = True
        self.prepare: bool = False
        self.regime: str = REGIME_OFF
        self.reason: str = "waiting"
        self.in_schedule: bool = False
        self.in_bath_period: bool = False
        self.room_temperature: float | None = None
        self.humidity: float | None = None
        self.corrections: dict[str, float] = {}
        self.boost: bool = False
        self.cooler_boost: bool = False
        self.drying: bool = False
        self.room_target: float | None = None
        self.heater_setpoint: float | None = None
        self.room_status: str = "waiting"
        self.bath_target: float | None = None
        self.bath_status: str = "waiting"

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Room Climate",
            model="Room controller",
        )
        self.bath_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_bath")},
            name=f"{entry.title} Bathroom",
            manufacturer="Room Climate",
            model="Bathroom controller",
        )  # linked to the room device in __init__._async_register_devices

        self.driver = ClimateDriver(
            hass,
            entry.title,
            self._notify_targets,
            ac_timeout=lambda: float(self.values.get("ac_response", DEFAULT_AC_RESPONSE_SECONDS)),
            wake=self.request_update,
            command_pause=self._command_pause,
        )
        self._listeners: list[CALLBACK_TYPE] = []
        self.end_override: Callable[[], None] | None = None  # set by the Override switch
        self._last_decision: str | None = None
        self._running = False
        self._debouncer = Debouncer(
            hass, _LOGGER, cooldown=2.0, immediate=False, function=self._async_apply
        )

    @property
    def boost_method(self) -> str:
        method = self.values["boost_method"]
        return method if self.coolers.entities else BOOST_HEATERS

    def _house(self) -> HouseController | None:
        return get_data(self.hass).house

    def _command_pause(self) -> float:
        house = self._house()
        return float(house.values["command_pause"]) if house else DEFAULT_COMMAND_PAUSE_SECONDS

    def _notify_targets(self) -> list[str]:
        house = self._house()
        return house.notify_targets() if house else []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        """Begin watching inputs once Home Assistant has fully started."""
        data = get_data(self.hass)
        data.rooms[self.entry.entry_id] = self
        watched = list(self._entity_group)
        for key in (CONF_TEMP_SENSOR, CONF_HUMIDITY_SENSOR, CONF_SCHEDULE, CONF_BATH_SCHEDULE):
            if entity_id := self.entry.options.get(key):
                watched.append(entity_id)

        @callback
        def _tick(_now: datetime) -> None:
            self.request_update()

        @callback
        def _started(_hass: HomeAssistant) -> None:
            self._running = True
            self.entry.async_on_unload(
                async_track_state_change_event(self.hass, watched, self._on_state_change)
            )
            self.entry.async_on_unload(
                async_track_time_interval(
                    self.hass, _tick, timedelta(seconds=UPDATE_INTERVAL_SECONDS)
                )
            )
            self.request_update()

        @callback
        def _stop() -> None:
            self._running = False
            self._debouncer.async_shutdown()
            self.driver.async_shutdown()
            data.rooms.pop(self.entry.entry_id, None)
            if house := self._house():
                house.set_pump_demand(self.entry.entry_id, None)

        self.entry.async_on_unload(async_at_started(self.hass, _started))
        self.entry.async_on_unload(_stop)

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        """Register an entity to be told when computed state changes."""
        self._listeners.append(update_callback)

        def _remove() -> None:
            self._listeners.remove(update_callback)

        return _remove

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    @callback
    def request_update(self) -> None:
        if self._running:
            self._debouncer.async_schedule_call()

    @callback
    def settings_changed(self) -> None:
        """A setting on the room device changed: drop manual targets and re-apply."""
        for group in self.groups:
            group.reset_manual()
        self.request_update()

    @callback
    def resume_automatic(self) -> None:
        """Back to the schedule: drop manual targets and devices turned off, end override."""
        _LOGGER.info("%s: back to automatic control", self.entry.title)
        if self.values["override"] and self.end_override is not None:
            self.end_override()  # also calls settings_changed
        self.settings_changed()

    # ------------------------------------------------------------------
    # Manual change detection
    # ------------------------------------------------------------------
    @callback
    def _on_state_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        group = self._entity_group.get(entity_id)
        if group is not None and not self.driver.is_own(event.context):
            self._check_manual_change(group, entity_id, event.data["new_state"])
        self.request_update()

    @callback
    def _check_manual_change(self, group: DeviceGroup, entity_id: str, new_state) -> None:
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if entity_id in self.driver.driving:
            return  # echo of a command we are sending right now
        if self.driver.is_pending(entity_id):
            return  # a slow A/C still catching up with our mode command
        want = group.desired.get(entity_id)
        if want is None:
            return
        if monotonic_time.monotonic() - group.sent_at.get(entity_id, -1e9) < COMMAND_GRACE_SECONDS:
            return  # device is still settling after our own command

        want_mode, want_temp = want
        mode = new_state.state
        follow = self.policy == POLICY_FOLLOW

        if want_mode == "off":
            if mode != "off" and entity_id in group.manual_off and follow:
                _LOGGER.info("%s: %s switched back on manually", self.entry.title, entity_id)
                group.manual_off.discard(entity_id)
            elif mode != "off":
                _LOGGER.info("%s: %s turned on manually, reverting", self.entry.title, entity_id)
            return

        if mode != want_mode:
            if mode == "off" and follow:
                _LOGGER.info(
                    "%s: %s turned off manually, respecting until next period",
                    self.entry.title,
                    entity_id,
                )
                group.manual_off.add(entity_id)
            else:
                _LOGGER.info(
                    "%s: %s mode changed to %s, reverting", self.entry.title, entity_id, mode
                )
            return

        temp = to_float(new_state.attributes.get("temperature"))
        if temp is None or want_temp is None or abs(temp - want_temp) < 0.05:
            return
        if any(
            abs(temp - other) < 0.05
            for other_id, (_mode, other) in group.desired.items()
            if other_id != entity_id and other is not None
        ):
            # Devices that share a setpoint (radiators of one Tado room) copy each other's
            # setpoint. That is the device syncing, not a person; it is re-driven as usual.
            _LOGGER.debug(
                "%s: %s took over another device's setpoint %.1f", self.entry.title, entity_id, temp
            )
            return
        if not follow:
            _LOGGER.info(
                "%s: manual setpoint %.1f on %s blocked", self.entry.title, temp, entity_id
            )
            return
        if group is self.coolers and want_mode == "heat":
            _LOGGER.info(
                "%s: setpoint %.1f on %s during boost reverted", self.entry.title, temp, entity_id
            )
            return
        adopted = temp
        if group is self.heaters:
            # A radiator shows target + correction (+ boost offset while boosting);
            # the person meant the room target, so turning it up 1 °C raises that by 1 °C.
            adopted -= self.corrections.get(entity_id, 0.0)
            if self.heater_setpoint is not None and self.room_target is not None:
                adopted -= self.heater_setpoint - self.room_target
        kind = KIND_HEAT if want_mode == "heat" and group.kind != KIND_FLOOR else group.kind
        low, high = self.limits.get(kind, (None, None))
        if low is not None:
            adopted = max(low, adopted)
        if high is not None:
            adopted = min(high, adopted)
        if group is self.heaters and self.room_target is not None:
            if abs(adopted - self.room_target) < 0.05:
                return  # same room target as now: nothing to adopt
        _LOGGER.info(
            "%s: manual setpoint %.1f on %s adopted as %.1f until next period",
            self.entry.title,
            temp,
            entity_id,
            adopted,
        )
        group.manual_target = adopted

    # ------------------------------------------------------------------
    # Reading inputs
    # ------------------------------------------------------------------
    def _read_sensor_or_devices(self, sensor_key: str, attribute: str) -> float | None:
        """A dedicated sensor if configured, else the average reading of the room's devices."""
        if entity_id := self.entry.options.get(sensor_key):
            state = self.hass.states.get(entity_id)
            return to_float(state.state) if state else None
        for group in (self.heaters, self.coolers):
            readings = []
            for device in group.entities:
                state = self.hass.states.get(device)
                if state and (value := to_float(state.attributes.get(attribute))) is not None:
                    readings.append(value)
            if readings:
                return round(sum(readings) / len(readings), 1)
        return None

    def _read_schedule(self, key: str) -> bool:
        """True inside a schedule period. A room without a schedule is always inside."""
        entity_id = self.entry.options.get(key)
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        return state is not None and state.state.lower() in ON_STATES

    def _device_reading(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        return to_float(state.attributes.get("current_temperature")) if state else None

    def _update_corrections(self) -> None:
        """How much higher than the room sensor the radiators (and each A/C) read."""
        self.corrections.clear()
        room = self.room_temperature
        if not self.entry.options.get(CONF_TEMP_SENSOR) or room is None:
            return
        cap = self.values["correction_max"]

        def whole_steps(excess: float) -> float:
            # Rounded down: never more than the difference, nothing below one step.
            steps = math.floor(excess / CORRECTION_STEP + 1e-6)
            return min(steps * CORRECTION_STEP, cap) if steps > 0 else 0.0

        # One correction for all radiators of the room: Tado X keeps a single setpoint per
        # room and copies it between heads, so different setpoints would fight each other.
        # A head reading lower than the room counts as 0, it never cancels another's excess.
        readings = [self._device_reading(e) for e in self.heaters.entities]
        excess = [max(r - room, 0.0) for r in readings if r is not None]
        if excess and (shared := whole_steps(sum(excess) / len(excess))):
            for entity_id in self.heaters.entities:
                self.corrections[entity_id] = shared
        for entity_id in self.coolers.entities:
            reading = self._device_reading(entity_id)
            if reading is not None and (correction := whole_steps(reading - room)):
                self.corrections[entity_id] = correction

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------
    def _compute(self) -> None:
        v = self.values
        house = self._house()
        self.season = house.read_season() if house else None
        self.at_home = house.read_at_home() if house else True
        self.prepare = house.read_prepare() if house else False
        self.room_temperature = self._read_sensor_or_devices(
            CONF_TEMP_SENSOR, "current_temperature"
        )
        self.humidity = self._read_sensor_or_devices(CONF_HUMIDITY_SENSOR, "current_humidity")
        self.in_schedule = self._read_schedule(CONF_SCHEDULE)
        self.in_bath_period = self.has_bathroom and self._read_schedule(CONF_BATH_SCHEDULE)
        self._update_corrections()

        # ---- regime --------------------------------------------------
        season = self.season
        if season == SEASON_HEAT and not self.at_home:
            season = SEASON_FROST  # the season entity should say so already; be sure
        in_use = v["in_use"]
        if house is None or season is None:
            self.regime, self.reason = REGIME_OFF, "waiting"
        elif season == SEASON_FROST:
            if in_use and self.prepare:
                self.regime, self.reason = REGIME_PREHEAT, "prepare_house"
            else:
                self.regime, self.reason = REGIME_FROST, "house_frost_protection"
        elif season == SEASON_HEAT:
            if in_use:
                self.regime, self.reason = REGIME_HEAT, "heating"
            else:
                self.regime, self.reason = REGIME_FROST, "not_in_use"
        elif season == SEASON_COOL:
            if not in_use:
                self.regime, self.reason = REGIME_OFF, "not_in_use"
            elif not (self.at_home or self.prepare):
                self.regime, self.reason = REGIME_OFF, "away"
            else:
                self.regime, self.reason = REGIME_COOL, "cooling"
        elif in_use and house.preheat:
            # Away in heating weather with Prepare house on (computed season only)
            self.regime, self.reason = REGIME_PREHEAT, "prepare_house"
        else:
            self.regime, self.reason = REGIME_OFF, "comfort_season"

        # ---- period keys: a change resets manual targets ----------------
        room_key = (self.regime, self.in_schedule, v["override"])
        bath_key = (self.regime, self.in_bath_period, v["override"])
        for group, key in (
            (self.heaters, room_key),
            (self.coolers, room_key),
            (self.bath_heaters, bath_key),
            (self.floor, bath_key),
        ):
            if group.period_key is not None and group.period_key != key:
                group.reset_manual()
            group.period_key = key

        # ---- target ---------------------------------------------------
        target: float | None = None
        status = "off"
        if self.reason == "waiting":
            status = "waiting"
        elif self.regime == REGIME_FROST:
            target, status = house.values["frost"], "frost_protection"
        elif self.regime == REGIME_PREHEAT:
            target, status = house.values["preheat"], "preheat"
        elif self.regime == REGIME_HEAT:
            if self.in_schedule:
                target, status = v["heat_comfort"], "comfort"
            else:
                target, status = v["heat_eco"], "eco"
        elif self.regime == REGIME_COOL and self.in_schedule:
            target, status = v["cool"], "comfort"
        if self.regime in (REGIME_HEAT, REGIME_COOL):
            active = self.heaters if self.regime == REGIME_HEAT else self.coolers
            if v["override"]:
                target, status = v["override_temp"], "override"
            elif target is not None and active.manual_target is not None:
                target, status = active.manual_target, "manual"
        self.room_target = target

        # ---- boost (heating only, with hysteresis) ----------------------
        if self.regime == REGIME_HEAT and target is not None and self.room_temperature is not None:
            delta = target - self.room_temperature
            if not self.boost and delta > v["boost_delta"]:
                self.boost = True
            elif self.boost and delta <= v["boost_margin"]:
                self.boost = False
        else:
            self.boost = False
        raise_heaters = self.boost and self.boost_method in (BOOST_HEATERS, BOOST_BOTH)
        self.cooler_boost = self.boost and self.boost_method in (BOOST_COOLERS, BOOST_BOTH)
        self.heater_setpoint = (
            target + v["boost_offset"] if (raise_heaters and target is not None) else target
        )
        if self.boost:
            status = "boost"

        # ---- drying: A/C dries when the room is neither heated nor cooled ---
        # Frost protection still counts as "no heating" for drying.
        can_dry = self.reason != "waiting" and (target is None or self.regime == REGIME_FROST)
        dry_capable = any(self._supports(e, "dry") for e in self.coolers.entities)
        was_drying = self.drying
        if house is not None and can_dry and dry_capable and self.humidity is not None:
            if not self.drying and self.humidity > house.values["dry_humidity"]:
                self.drying = True
            elif self.drying and self.humidity <= (
                house.values["dry_humidity"] - house.values["dry_hysteresis"]
            ):
                self.drying = False
        else:
            self.drying = False
        if self.drying != was_drying:
            self.coolers.reset_manual()  # an A/C switched off by hand is reconsidered
        if self.drying and status == "off":
            status = "drying"
        self.room_status = status

        # ---- bathroom ---------------------------------------------------
        if not self.has_bathroom:
            self.bath_target, self.bath_status = None, "off"
        elif self.reason == "waiting":
            self.bath_target, self.bath_status = None, "waiting"
        elif self.regime == REGIME_FROST:
            self.bath_target, self.bath_status = target, "frost_protection"
        elif self.regime == REGIME_PREHEAT:
            self.bath_target, self.bath_status = target, "follow_room"
        elif self.regime == REGIME_HEAT:
            if self.in_bath_period:
                self.bath_target, self.bath_status = v["bath_comfort"], "bath_period"
            else:
                self.bath_target, self.bath_status = target, "follow_room"
            if self.bath_heaters.manual_target is not None:
                self.bath_target, self.bath_status = self.bath_heaters.manual_target, "manual"
        else:
            self.bath_target, self.bath_status = None, "off"

    def _supports(self, entity_id: str, mode: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and mode in (state.attributes.get("hvac_modes") or [])

    def _plan(self) -> dict[str, tuple[DeviceGroup, str, float | None]]:
        """Desired (mode, setpoint) for every managed entity."""
        plan: dict[str, tuple[DeviceGroup, str, float | None]] = {}
        # Radiators of a room get exactly the same setpoint (see _update_corrections), so
        # they are capped at the lowest maximum among them rather than each at its own.
        radiator_max = min(
            (
                high
                for e in self.heaters.entities
                if (st := self.hass.states.get(e))
                and (high := to_float(st.attributes.get("max_temp"))) is not None
            ),
            default=None,
        )

        def put(group: DeviceGroup, mode: str, temp: float | None, corrected: bool = False):
            for entity_id in group.entities:
                if mode != "off" and entity_id in group.manual_off:
                    plan[entity_id] = (group, "off", None)
                elif corrected and temp is not None:
                    value = temp + self.corrections.get(entity_id, 0.0)
                    if group is self.heaters and radiator_max is not None:
                        value = min(value, radiator_max)
                    plan[entity_id] = (group, mode, value)
                else:
                    plan[entity_id] = (group, mode, temp)

        def coolers_idle() -> None:
            if not self.drying:
                put(self.coolers, "off", None)
                return
            for entity_id in self.coolers.entities:
                if self._supports(entity_id, "dry") and entity_id not in self.coolers.manual_off:
                    plan[entity_id] = (self.coolers, "dry", None)
                else:
                    plan[entity_id] = (self.coolers, "off", None)

        if self.reason == "waiting":
            return plan  # hold everything as it is
        if self.regime == REGIME_HEAT:
            put(self.heaters, "heat", self.heater_setpoint, corrected=True)
            if self.cooler_boost:
                put(self.coolers, "heat", self.room_target, corrected=True)
            else:
                coolers_idle()
        elif self.regime == REGIME_COOL and self.room_target is not None:
            put(self.heaters, "off", None)
            put(self.coolers, "cool", self.room_target)
        elif self.regime in (REGIME_FROST, REGIME_PREHEAT):
            put(self.heaters, "heat", self.room_target, corrected=True)
            coolers_idle()
        else:
            put(self.heaters, "off", None)
            coolers_idle()

        if self.has_bathroom:
            if self.bath_target is not None:
                put(self.bath_heaters, "heat", self.bath_target)
            else:
                put(self.bath_heaters, "off", None)
            if self.regime == REGIME_HEAT and self.in_bath_period:
                floor_temp = self.floor.manual_target
                put(
                    self.floor,
                    "heat",
                    floor_temp if floor_temp is not None else self.values["bath_floor_temp"],
                )
            else:
                put(self.floor, "off", None)
        return plan

    def _pump_demand(
        self, plan: dict[str, tuple[DeviceGroup, str, float | None]]
    ) -> dict[str, float | None] | None:
        """Highest setpoint among this room's heating radiators, for its floor thermostat."""
        if not self.thermostat or self.reason == "waiting":
            return None
        setpoints = [
            temp
            for group, mode, temp in plan.values()
            if group in (self.heaters, self.bath_heaters) and mode == "heat" and temp is not None
        ]
        return {self.thermostat: max(setpoints) if setpoints else None}

    # ------------------------------------------------------------------
    # Applying to devices
    # ------------------------------------------------------------------
    async def _async_apply(self) -> None:
        self._compute()
        plan = self._plan()
        self._log_decision(plan)
        try:
            for entity_id, (group, mode, temp) in plan.items():
                await self.driver.async_drive(group, entity_id, mode, temp)
        finally:
            # Even if a device failed: the floor thermostat and the sensors stay current.
            if house := self._house():
                house.set_pump_demand(self.entry.entry_id, self._pump_demand(plan))
            self._notify()

    def _log_decision(self, plan: dict[str, tuple[DeviceGroup, str, float | None]]) -> None:
        """One debug line per change of decision: why the room does what it does."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        devices = {
            entity_id: mode if temp is None else f"{mode} {temp:.1f}"
            for entity_id, (_group, mode, temp) in plan.items()
        }
        decision = (
            f"{self.room_status} ({self.regime}, {self.reason}) target={self.room_target} "
            f"room={self.room_temperature} humidity={self.humidity} "
            f"season={self.season} at_home={self.at_home} prepare={self.prepare} "
            f"in_schedule={self.in_schedule} boost={self.boost} drying={self.drying} "
            f"corrections={self.corrections} bath={self.bath_status}/{self.bath_target} "
            f"devices={devices}"
        )
        if decision != self._last_decision:
            self._last_decision = decision
            _LOGGER.debug("%s: %s", self.entry.title, decision)

    # ------------------------------------------------------------------
    # Attributes for sensors
    # ------------------------------------------------------------------
    def room_attributes(self) -> dict[str, Any]:
        manual = (
            self.heaters.manual_target if self.regime != REGIME_COOL else self.coolers.manual_target
        )
        return {
            "status": self.room_status,
            "regime": self.regime,
            "reason": self.reason,
            "season": self.season,
            "at_home": self.at_home,
            "prepare_house": self.prepare,
            "in_use": self.values["in_use"],
            "in_schedule": self.in_schedule,
            "room_temperature": self.room_temperature,
            "humidity": self.humidity,
            "boost": self.boost,
            "boost_method": self.boost_method,
            "heater_setpoint": self.heater_setpoint,
            "radiator_corrections": dict(self.corrections),
            "drying": self.drying,
            "floor_thermostat": self.thermostat,
            "manual_target": manual,
            "manually_off": sorted(self.heaters.manual_off | self.coolers.manual_off),
            "override": self.values["override"],
            "override_until": self.values["override_until"],
        }

    def bath_attributes(self) -> dict[str, Any]:
        return {
            "status": self.bath_status,
            "in_bath_period": self.in_bath_period,
            "floor_heating": self.regime == REGIME_HEAT and self.in_bath_period,
            "manual_target": self.bath_heaters.manual_target,
            "manually_off": sorted(self.bath_heaters.manual_off | self.floor.manual_off),
        }
