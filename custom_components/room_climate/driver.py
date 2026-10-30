"""Sending commands to climate devices without mistaking their echoes for manual changes."""

from __future__ import annotations

import logging
import time as monotonic_time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import DEFAULT_AC_RESPONSE_SECONDS

_LOGGER = logging.getLogger(__name__)

AC_MODE_ATTEMPTS = 2  # mode commands per failure report; retried after that, reported once

KIND_HEAT = "heat"
KIND_COOL = "cool"
KIND_FLOOR = "floor"


def to_float(value: Any) -> float | None:
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
    ac: bool = False  # A/C units get the verified mode / temperature / fan sequence
    manual_target: float | None = None
    manual_off: set[str] = field(default_factory=set)
    period_key: tuple | None = None
    desired: dict[str, tuple[str, float | None]] = field(default_factory=dict)
    sent_at: dict[str, float] = field(default_factory=dict)

    def reset_manual(self) -> None:
        self.manual_target = None
        self.manual_off.clear()


@dataclass
class _Pending:
    """An A/C was sent `want` and has until `deadline` to report following it."""

    want: tuple[str, float | None]  # (mode, temperature)
    stage: str  # "mode": waiting for the mode; "settings": for temperature (and fan)
    deadline: datetime
    attempt: int


def _describe(want: tuple[str, float | None]) -> str:
    mode, temp = want
    return mode if temp is None or mode == "off" else f"{mode} {temp:g} °C"


class ClimateDriver:
    """Drives climate entities, remembering which state changes it caused itself."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        notify_targets: Callable[[], list[str]],
        ac_timeout: Callable[[], float] = lambda: DEFAULT_AC_RESPONSE_SECONDS,
        wake: Callable[[], None] | None = None,
    ) -> None:
        self.hass = hass
        self.name = name
        self._notify_targets = notify_targets
        self._ac_timeout = ac_timeout  # how long an A/C may take to report a new mode
        self._wake = wake  # asks the owner for an update cycle when that time is up
        self._pending: dict[str, _Pending] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self.driving: set[str] = set()
        self._own_contexts: deque[str] = deque(maxlen=200)
        self._failed: set[str] = set()
        self._erroring: set[tuple[str, str]] = set()  # (entity_id, service) raising errors

    def is_own(self, context: Context) -> bool:
        return context.id in self._own_contexts or (
            context.parent_id is not None and context.parent_id in self._own_contexts
        )

    async def async_drive(
        self, group: DeviceGroup, entity_id: str, mode: str, temp: float | None
    ) -> None:
        self.driving.add(entity_id)
        try:
            await self._async_drive(group, entity_id, mode, temp)
        finally:
            self.driving.discard(entity_id)

    async def _async_drive(
        self, group: DeviceGroup, entity_id: str, mode: str, temp: float | None
    ) -> None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        attrs = state.attributes
        hvac_modes = attrs.get("hvac_modes") or []

        if temp is not None:
            step = to_float(attrs.get("target_temp_step")) or 0.5
            low = to_float(attrs.get("min_temp"))
            high = to_float(attrs.get("max_temp"))
            if low is not None:
                temp = max(low, temp)
            if high is not None:
                temp = min(high, temp)
            temp = round(round(temp / step) * step, 2)

        group.desired[entity_id] = (mode, temp)

        if group.ac:
            await self._async_drive_ac(group, entity_id, mode, temp, hvac_modes)
            return
        if mode == "off":
            if state.state == "off":
                return
            if "off" in hvac_modes:
                await self._async_set_mode(group, entity_id, "off")
                return
            # No off mode: park the device at its minimum setpoint instead.
            floor_temp = to_float(attrs.get("min_temp"))
            group.desired[entity_id] = (state.state, floor_temp)
            if floor_temp is not None and to_float(attrs.get("temperature")) != floor_temp:
                await self._async_call(
                    group, "set_temperature", {ATTR_ENTITY_ID: entity_id, "temperature": floor_temp}
                )
            return

        sent = False
        if state.state != mode:
            if mode not in hvac_modes:
                _LOGGER.warning("%s: %s does not support hvac mode %s", self.name, entity_id, mode)
            else:
                sent = await self._async_set_mode(group, entity_id, mode)

        current = to_float(attrs.get("temperature"))
        if temp is not None and (current is None or abs(current - temp) >= 0.05 or sent):
            await self._async_call(
                group, "set_temperature", {ATTR_ENTITY_ID: entity_id, "temperature": temp}
            )

    async def _async_set_mode(self, group: DeviceGroup, entity_id: str, mode: str) -> bool:
        return await self._async_call(
            group, "set_hvac_mode", {ATTR_ENTITY_ID: entity_id, "hvac_mode": mode}
        )

    def is_pending(self, entity_id: str) -> bool:
        """An A/C was sent an instruction and has not reported following it yet."""
        return entity_id in self._pending

    def _ac_follows(self, entity_id: str, mode: str, temp: float | None) -> bool:
        state = self.hass.states.get(entity_id)
        if state is None or state.state != mode:
            return False
        if mode == "off" or temp is None:
            return True
        current = to_float(state.attributes.get("temperature"))
        return current is not None and abs(current - temp) < 0.05

    async def _async_drive_ac(
        self,
        group: DeviceGroup,
        entity_id: str,
        mode: str,
        temp: float | None,
        hvac_modes: list[str],
    ) -> None:
        """Keep an A/C on (mode, temperature) without flooding a slow cloud API.

        An instruction is sent once and then given the response time to show up in the
        A/C's state (LG polls its cloud; it also refuses commands while its app is in use).
        Only then is it sent again; after AC_MODE_ATTEMPTS misses the notify services are
        told once, and it keeps being retried. The order is always: mode, then (once the
        A/C reports the mode) temperature, then fan auto.
        """
        if mode not in hvac_modes:
            _LOGGER.warning("%s: %s does not support hvac mode %s", self.name, entity_id, mode)
            return
        want = (mode, temp)
        pending = self._pending.get(entity_id)
        if pending is not None and pending.want != want:
            self._clear_pending(entity_id)  # a new instruction replaces the old one: act now
            pending = None
        if self._ac_follows(entity_id, mode, temp):
            if pending is not None:
                self._confirmed(group, entity_id)
            return

        state = self.hass.states.get(entity_id)
        mode_arrived = pending is not None and pending.stage == "mode" and state.state == mode
        now = dt_util.utcnow()
        attempt = 1
        if pending is not None and not mode_arrived:
            if now < pending.deadline:
                return  # still waiting for the A/C to report it
            _LOGGER.warning(
                "%s: %s has not followed %s within %ss (it is %s %s, attempt %d)",
                self.name,
                entity_id,
                _describe(want),
                round(self._ac_timeout()),
                state.state,
                state.attributes.get("temperature"),
                pending.attempt,
            )
            if pending.attempt >= AC_MODE_ATTEMPTS:
                await self.async_report_failure(entity_id, _describe(want))  # keep trying
            else:
                attempt = pending.attempt + 1

        stage = await self._async_send_ac(group, entity_id, mode, temp, mode_arrived)
        if self._ac_follows(entity_id, mode, temp):
            self._confirmed(group, entity_id)
            return
        if mode_arrived:
            attempt = 1  # a new stage (temperature) gets its own attempts
        self._cancel_timer(entity_id)
        timeout = self._ac_timeout()
        self._pending[entity_id] = _Pending(want, stage, now + timedelta(seconds=timeout), attempt)
        if self._wake is not None:
            self._timers[entity_id] = async_call_later(self.hass, timeout, self._on_timeout)

    async def _async_send_ac(
        self,
        group: DeviceGroup,
        entity_id: str,
        mode: str,
        temp: float | None,
        mode_arrived: bool,
    ) -> str:
        """Send what differs. Returns the stage now waited for: "mode" or "settings"."""
        state = self.hass.states.get(entity_id)
        switched = mode_arrived
        if state.state != mode:
            await self._async_set_mode(group, entity_id, mode)
            state = self.hass.states.get(entity_id)
            if state is None or state.state != mode:
                return "mode"  # temperature and fan wait until the A/C reports the mode
            switched = True
        if mode == "off":
            return "mode"
        current = to_float(state.attributes.get("temperature"))
        sent = False
        if temp is not None and (current is None or abs(current - temp) >= 0.05 or switched):
            sent = await self._async_call(
                group, "set_temperature", {ATTR_ENTITY_ID: entity_id, "temperature": temp}
            )
        if switched or sent:
            await self._async_fan_auto(group, entity_id)
        return "settings"

    def _clear_pending(self, entity_id: str) -> None:
        self._pending.pop(entity_id, None)
        self._cancel_timer(entity_id)

    def _confirmed(self, group: DeviceGroup, entity_id: str) -> None:
        self._clear_pending(entity_id)
        self.failure_cleared(entity_id)
        # The settling window for its echoes starts when the A/C finally reports.
        group.sent_at[entity_id] = monotonic_time.monotonic()

    @callback
    def _on_timeout(self, _now: datetime) -> None:
        if self._wake is not None:
            self._wake()

    def _cancel_timer(self, entity_id: str) -> None:
        if (cancel := self._timers.pop(entity_id, None)) is not None:
            cancel()

    @callback
    def async_shutdown(self) -> None:
        for entity_id in list(self._timers):
            self._cancel_timer(entity_id)
        self._pending.clear()

    async def _async_fan_auto(self, group: DeviceGroup, entity_id: str) -> None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return
        auto = next(
            (m for m in state.attributes.get("fan_modes") or [] if str(m).lower() == "auto"), None
        )
        if auto is not None and state.attributes.get("fan_mode") != auto:
            await self._async_call(
                group, "set_fan_mode", {ATTR_ENTITY_ID: entity_id, "fan_mode": auto}
            )

    def failure_cleared(self, entity_id: str) -> None:
        """The device followed again: a later failure is reported again."""
        self._failed.discard(entity_id)

    async def async_report_failure(self, entity_id: str, wanted: str) -> None:
        """Tell the configured notify services, once per failure episode."""
        if entity_id in self._failed:
            return
        self._failed.add(entity_id)
        message = f"{self.name}: {entity_id} did not switch to {wanted}."
        _LOGGER.error("%s", message)
        for target in self._notify_targets():
            name = target.removeprefix("notify.")
            if self.hass.services.has_service("notify", name):
                # Classic notify action, e.g. notify.mobile_app_my_phone
                service, data = name, {"title": "Room Climate", "message": message}
            else:
                # Notify entity, e.g. a notify group helper: notify.send_message
                service = "send_message"
                data = {"entity_id": f"notify.{name}", "message": f"Room Climate: {message}"}
            try:
                await self.hass.services.async_call("notify", service, data, blocking=True)
            except Exception as err:  # a broken notifier must not stop driving devices
                _LOGGER.warning("%s: notify.%s failed: %s", self.name, name, err)

    async def _async_call(self, group: DeviceGroup, service: str, data: dict[str, Any]) -> bool:
        _LOGGER.debug("%s: climate.%s %s", self.name, service, data)
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
                "%s: climate.%s failed for %s: %s", self.name, service, data[ATTR_ENTITY_ID], err
            )
            return False
        except Exception:
            # A bug in the device's integration must not stop the rest of the room.
            # Retried every cycle, so the traceback is logged once until it works again.
            key = (data[ATTR_ENTITY_ID], service)
            if key in self._erroring:
                _LOGGER.debug("%s: climate.%s still failing for %s", self.name, *reversed(key))
            else:
                self._erroring.add(key)
                _LOGGER.exception(
                    "%s: climate.%s raised an error in %s's integration "
                    "(retried every minute, logged again only after it has worked)",
                    self.name,
                    service,
                    data[ATTR_ENTITY_ID],
                )
            return False
        self._erroring.discard((data[ATTR_ENTITY_ID], service))
        return True
