"""Room logic: decides what every managed climate device should do."""

from __future__ import annotations

import logging
import time as monotonic_time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    AWAY_STATES,
    BATH_NUMBERS,
    BATH_TIMES,
    BOOST_BOTH,
    BOOST_COOLERS,
    BOOST_EXIT_MARGIN,
    BOOST_HEATERS,
    COMMAND_GRACE_SECONDS,
    CONF_AT_HOME,
    CONF_BATH_FLOOR,
    CONF_BATH_HEATERS,
    CONF_BOOST_METHOD,
    CONF_COOL_MAX,
    CONF_COOL_MIN,
    CONF_COOLERS,
    CONF_HAS_BATHROOM,
    CONF_HEAT_MAX,
    CONF_HEAT_MIN,
    CONF_HEATERS,
    CONF_HOUSE_STATUS,
    CONF_MANUAL_POLICY,
    CONF_ROOM_SENSOR,
    DEFAULT_COOL_MAX,
    DEFAULT_COOL_MIN,
    DEFAULT_HEAT_MAX,
    DEFAULT_HEAT_MIN,
    DOMAIN,
    HOUSE_COOL,
    HOUSE_FROST,
    HOUSE_HEAT,
    HOUSE_STATUS_MAP,
    POLICY_FOLLOW,
    REGIME_COOL,
    REGIME_FROST,
    REGIME_HEAT,
    REGIME_OFF,
    ROOM_NUMBERS,
    ROOM_TIMES,
    UPDATE_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

KIND_HEAT = "heat"
KIND_COOL = "cool"
KIND_FLOOR = "floor"


def in_window(now: time, start: time, end: time) -> bool:
    """Return True if `now` is inside [start, end); handles windows past midnight."""
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class DeviceGroup:
    """A set of climate entities that are driven together."""

    key: str
    kind: str
    entities: list[str]
    manual_target: float | None = None
    manual_off: set[str] = field(default_factory=set)
    period_key: tuple | None = None
    desired: dict[str, tuple[str, float | None]] = field(default_factory=dict)
    sent_at: dict[str, float] = field(default_factory=dict)

    def reset_manual(self) -> None:
        self.manual_target = None
        self.manual_off.clear()


class RoomController:
    """Owns the state of one room (and its optional bathroom)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        opts = entry.options

        self.policy: str = opts.get(CONF_MANUAL_POLICY, POLICY_FOLLOW)
        method = opts.get(CONF_BOOST_METHOD, BOOST_HEATERS)
        if method != BOOST_HEATERS and not opts.get(CONF_COOLERS):
            method = BOOST_HEATERS  # nothing to boost with but the heaters
        self.boost_method: str = method
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

        self.heaters = DeviceGroup("heaters", KIND_HEAT, list(opts.get(CONF_HEATERS, [])))
        self.coolers = DeviceGroup("coolers", KIND_COOL, list(opts.get(CONF_COOLERS, [])))
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
        self.values.update({k: v[1] for k, v in ROOM_TIMES.items()})
        self.values.update({"enabled": True, "override": False, "override_until": None})
        if self.has_bathroom:
            self.values.update({k: v[4] for k, v in BATH_NUMBERS.items()})
            self.values.update({k: v[1] for k, v in BATH_TIMES.items()})

        # Computed state, read by the sensors.
        self.house: str | None = None
        self.at_home: bool = True
        self.regime: str = REGIME_OFF
        self.reason: str = "waiting"
        self.in_comfort: bool = False
        self.in_bath_period: bool = False
        self.room_temperature: float | None = None
        self.boost: bool = False
        self.cooler_boost: bool = False
        self.room_target: float | None = None
        self.heater_setpoint: float | None = None
        self.room_status: str = "waiting"
        self.bath_target: float | None = None
        self.bath_status: str = "waiting"

        self.room_device_info = DeviceInfo(
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
            via_device=(DOMAIN, entry.entry_id),
        )

        self._listeners: list[CALLBACK_TYPE] = []
        self._driving: set[str] = set()
        self._own_contexts: deque[str] = deque(maxlen=200)
        self._running = False
        self._debouncer = Debouncer(
            hass, _LOGGER, cooldown=2.0, immediate=False, function=self._async_apply
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        """Begin watching inputs once Home Assistant has fully started."""
        watched = [e for e in self._entity_group]
        for key in (CONF_AT_HOME, CONF_HOUSE_STATUS, CONF_ROOM_SENSOR):
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

    # ------------------------------------------------------------------
    # Manual change detection
    # ------------------------------------------------------------------
    @callback
    def _on_state_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        group = self._entity_group.get(entity_id)
        if group is not None and not self._is_own(event.context):
            self._check_manual_change(group, entity_id, event.data["new_state"])
        self.request_update()

    def _is_own(self, context: Context) -> bool:
        return context.id in self._own_contexts or (
            context.parent_id is not None and context.parent_id in self._own_contexts
        )

    @callback
    def _check_manual_change(self, group: DeviceGroup, entity_id: str, new_state) -> None:
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if entity_id in self._driving:
            return  # echo of a command we are sending right now
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

        temp = _float(new_state.attributes.get("temperature"))
        if temp is None or want_temp is None or abs(temp - want_temp) < 0.05:
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
        kind = KIND_HEAT if want_mode == "heat" and group.kind != KIND_FLOOR else group.kind
        low, high = self.limits.get(kind, (None, None))
        adopted = temp
        if low is not None:
            adopted = max(low, adopted)
        if high is not None:
            adopted = min(high, adopted)
        if group is self.heaters and self.boost:
            # The user changed a boosted setpoint: take it as the new room target.
            self.boost = False
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
    def _read_house(self) -> str | None:
        entity_id = self.entry.options.get(CONF_HOUSE_STATUS)
        if not entity_id:
            return HOUSE_HEAT
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        mapped = HOUSE_STATUS_MAP.get(state.state.strip().lower())
        if mapped is None:
            _LOGGER.warning("%s: unknown house status '%s'", self.entry.title, state.state)
        return mapped

    def _read_at_home(self) -> bool:
        entity_id = self.entry.options.get(CONF_AT_HOME)
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        if state is None:
            return True
        value = state.state.lower()
        if value in AWAY_STATES:
            return False
        return True  # home, or unknown/unavailable -> assume someone is home

    def _read_room_temperature(self) -> float | None:
        entity_id = self.entry.options.get(CONF_ROOM_SENSOR)
        if entity_id:
            state = self.hass.states.get(entity_id)
            return _float(state.state) if state else None
        readings = []
        for heater in self.heaters.entities:
            state = self.hass.states.get(heater)
            if state and (value := _float(state.attributes.get("current_temperature"))) is not None:
                readings.append(value)
        return round(sum(readings) / len(readings), 1) if readings else None

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------
    def _compute(self, now: datetime) -> None:
        v = self.values
        local_time = dt_util.as_local(now).time().replace(microsecond=0)
        self.house = self._read_house()
        self.at_home = self._read_at_home()
        self.room_temperature = self._read_room_temperature()
        self.in_comfort = in_window(local_time, v["comfort_start"], v["comfort_end"])
        self.in_bath_period = self.has_bathroom and (
            in_window(local_time, v["bath_p1_start"], v["bath_p1_end"])
            or in_window(local_time, v["bath_p2_start"], v["bath_p2_end"])
        )

        # ---- regime --------------------------------------------------
        house = self.house
        if house is None:
            self.regime, self.reason = REGIME_OFF, "waiting"
        elif not v["enabled"]:
            self.regime = REGIME_FROST if house in (HOUSE_HEAT, HOUSE_FROST) else REGIME_OFF
            self.reason = "disabled"
        elif house == HOUSE_FROST:
            self.regime, self.reason = REGIME_FROST, "house_frost_protection"
        elif not self.at_home:
            self.regime = REGIME_FROST if house == HOUSE_HEAT else REGIME_OFF
            self.reason = "away"
        elif house == HOUSE_HEAT:
            self.regime, self.reason = REGIME_HEAT, "heating"
        elif house == HOUSE_COOL:
            self.regime, self.reason = REGIME_COOL, "cooling"
        else:
            self.regime, self.reason = REGIME_OFF, "comfort_season"

        # ---- period keys: a change resets manual targets ----------------
        room_key = (self.regime, self.in_comfort, v["override"])
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

        # ---- room target ------------------------------------------------
        target: float | None = None
        status = "off"
        active_group: DeviceGroup | None = None
        if self.reason == "waiting":
            status = "waiting"
        elif self.regime == REGIME_FROST:
            target, status = v["frost"], "frost_protection"
        elif self.regime in (REGIME_HEAT, REGIME_COOL):
            heating = self.regime == REGIME_HEAT
            active_group = self.heaters if heating else self.coolers
            base = v["heat"] if heating else v["cool"]
            if self.in_comfort:
                target, status = base, "comfort"
            else:
                target = base - v["setback"] if heating else base + v["setback"]
                status = "setback"
            if v["override"]:
                target, status = v["override_temp"], "override"
            elif active_group.manual_target is not None:
                target, status = active_group.manual_target, "manual"
        self.room_target = target

        # ---- boost (heating only, with hysteresis) ----------------------
        if self.regime == REGIME_HEAT and target is not None and self.room_temperature is not None:
            delta = target - self.room_temperature
            if not self.boost and delta > v["boost_delta"]:
                self.boost = True
            elif self.boost and delta <= BOOST_EXIT_MARGIN:
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
        self.room_status = status

        # ---- bathroom ---------------------------------------------------
        if not self.has_bathroom:
            self.bath_target, self.bath_status = None, "off"
        elif self.reason == "waiting":
            self.bath_target, self.bath_status = None, "waiting"
        elif self.regime == REGIME_FROST:
            self.bath_target, self.bath_status = v["frost"], "frost_protection"
        elif self.regime == REGIME_HEAT:
            if self.in_bath_period:
                self.bath_target, self.bath_status = v["bath_comfort"], "bath_period"
            else:
                self.bath_target, self.bath_status = target, "follow_room"
            if self.bath_heaters.manual_target is not None:
                self.bath_target, self.bath_status = self.bath_heaters.manual_target, "manual"
        else:
            self.bath_target, self.bath_status = None, "off"

    def _plan(self) -> dict[str, tuple[DeviceGroup, str, float | None]]:
        """Desired (mode, setpoint) for every managed entity."""
        plan: dict[str, tuple[DeviceGroup, str, float | None]] = {}

        def put(group: DeviceGroup, mode: str, temp: float | None) -> None:
            for entity_id in group.entities:
                if mode != "off" and entity_id in group.manual_off:
                    plan[entity_id] = (group, "off", None)
                else:
                    plan[entity_id] = (group, mode, temp)

        if self.reason == "waiting":
            return plan  # hold everything as it is
        if self.regime == REGIME_HEAT:
            put(self.heaters, "heat", self.heater_setpoint)
            if self.cooler_boost:
                put(self.coolers, "heat", self.room_target)
            else:
                put(self.coolers, "off", None)
        elif self.regime == REGIME_COOL:
            put(self.heaters, "off", None)
            put(self.coolers, "cool", self.room_target)
        elif self.regime == REGIME_FROST:
            put(self.heaters, "heat", self.room_target)
            put(self.coolers, "off", None)
        else:
            put(self.heaters, "off", None)
            put(self.coolers, "off", None)

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

    # ------------------------------------------------------------------
    # Applying to devices
    # ------------------------------------------------------------------
    async def _async_apply(self) -> None:
        self._compute(dt_util.utcnow())
        for entity_id, (group, mode, temp) in self._plan().items():
            await self._async_drive(group, entity_id, mode, temp)
        self._notify()

    async def _async_drive(
        self, group: DeviceGroup, entity_id: str, mode: str, temp: float | None
    ) -> None:
        self._driving.add(entity_id)
        try:
            await self._async_drive_inner(group, entity_id, mode, temp)
        finally:
            self._driving.discard(entity_id)

    async def _async_drive_inner(
        self, group: DeviceGroup, entity_id: str, mode: str, temp: float | None
    ) -> None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        attrs = state.attributes

        if temp is not None:
            step = _float(attrs.get("target_temp_step")) or 0.5
            low = _float(attrs.get("min_temp"))
            high = _float(attrs.get("max_temp"))
            if low is not None:
                temp = max(low, temp)
            if high is not None:
                temp = min(high, temp)
            temp = round(round(temp / step) * step, 2)

        group.desired[entity_id] = (mode, temp)
        sent = False

        if mode == "off":
            if state.state != "off":
                hvac_modes = attrs.get("hvac_modes") or []
                if "off" in hvac_modes:
                    sent = await self._async_call(
                        group, "set_hvac_mode", {ATTR_ENTITY_ID: entity_id, "hvac_mode": "off"}
                    )
                else:
                    floor_temp = _float(attrs.get("min_temp"))
                    if floor_temp is not None and _float(attrs.get("temperature")) != floor_temp:
                        group.desired[entity_id] = (state.state, floor_temp)
                        sent = await self._async_call(
                            group,
                            "set_temperature",
                            {ATTR_ENTITY_ID: entity_id, "temperature": floor_temp},
                        )
                    else:
                        group.desired[entity_id] = (state.state, floor_temp)
        else:
            if state.state != mode:
                if mode in (attrs.get("hvac_modes") or []):
                    sent = await self._async_call(
                        group, "set_hvac_mode", {ATTR_ENTITY_ID: entity_id, "hvac_mode": mode}
                    )
                else:
                    _LOGGER.warning(
                        "%s: %s does not support hvac mode %s", self.entry.title, entity_id, mode
                    )
            current = _float(attrs.get("temperature"))
            if temp is not None and (current is None or abs(current - temp) >= 0.05 or sent):
                sent = (
                    await self._async_call(
                        group, "set_temperature", {ATTR_ENTITY_ID: entity_id, "temperature": temp}
                    )
                    or sent
                )

    async def _async_call(self, group: DeviceGroup, service: str, data: dict[str, Any]) -> bool:
        _LOGGER.debug("%s: climate.%s %s", self.entry.title, service, data)
        # Stamp before sending so the device's echo is never taken for a manual change.
        group.sent_at[data[ATTR_ENTITY_ID]] = monotonic_time.monotonic()
        context = Context()
        self._own_contexts.append(context.id)
        try:
            await self.hass.services.async_call(
                "climate", service, data, blocking=True, context=context
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "%s: climate.%s failed for %s: %s",
                self.entry.title,
                service,
                data[ATTR_ENTITY_ID],
                err,
            )
            return False
        return True

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
            "house_status": self.house,
            "at_home": self.at_home,
            "enabled": self.values["enabled"],
            "in_comfort_period": self.in_comfort,
            "room_temperature": self.room_temperature,
            "boost": self.boost,
            "boost_method": self.boost_method,
            "heater_setpoint": self.heater_setpoint,
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
