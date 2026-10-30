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
class _PendingMode:
    """An A/C was asked for `mode` and has until `deadline` to report it."""

    mode: str
    deadline: datetime
    attempt: int


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
        self._pending: dict[str, _PendingMode] = {}
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

        if mode == "off" and group.ac and "off" in hvac_modes:
            await self._async_ac_mode(group, entity_id, "off", hvac_modes)
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
        if group.ac:
            switched = await self._async_ac_mode(group, entity_id, mode, hvac_modes)
            if switched is None:
                return  # not in the mode yet: temperature and fan wait for confirmation
            sent = switched
            attrs = self.hass.states.get(entity_id).attributes
        elif state.state != mode:
            if mode not in hvac_modes:
                _LOGGER.warning("%s: %s does not support hvac mode %s", self.name, entity_id, mode)
            else:
                sent = await self._async_set_mode(group, entity_id, mode)

        current = to_float(attrs.get("temperature"))
        if temp is not None and (current is None or abs(current - temp) >= 0.05 or sent):
            sent = (
                await self._async_call(
                    group, "set_temperature", {ATTR_ENTITY_ID: entity_id, "temperature": temp}
                )
                or sent
            )
        if group.ac and sent:
            await self._async_fan_auto(group, entity_id)

    async def _async_set_mode(self, group: DeviceGroup, entity_id: str, mode: str) -> bool:
        return await self._async_call(
            group, "set_hvac_mode", {ATTR_ENTITY_ID: entity_id, "hvac_mode": mode}
        )

    def is_pending(self, entity_id: str) -> bool:
        """An A/C was asked for a mode and has not reported it yet."""
        return entity_id in self._pending

    async def _async_ac_mode(
        self, group: DeviceGroup, entity_id: str, mode: str, hvac_modes: list[str]
    ) -> bool | None:
        """Put an A/C in `mode` without blocking while it is slow to report.

        Returns None while the A/C is not in the mode yet, True when it has just switched
        (temperature and fan follow now), False when it already was in the mode.
        """
        state = self.hass.states.get(entity_id)
        pending = self._pending.get(entity_id)
        if state is not None and state.state == mode:
            if pending is None:
                return False
            self._confirmed(group, entity_id)
            return True
        if mode not in hvac_modes:
            _LOGGER.warning("%s: %s does not support hvac mode %s", self.name, entity_id, mode)
            return None

        now = dt_util.utcnow()
        attempt = 1
        if pending is not None and pending.mode == mode:
            if now < pending.deadline:
                return None  # still waiting for the A/C to report it
            _LOGGER.warning(
                "%s: %s is %s %ss after asking for %s (attempt %d)",
                self.name,
                entity_id,
                state.state if state else None,
                round(self._ac_timeout()),
                mode,
                pending.attempt,
            )
            if pending.attempt >= AC_MODE_ATTEMPTS:
                await self._async_report_failure(entity_id, mode)  # then keep trying
            else:
                attempt = pending.attempt + 1

        await self._async_set_mode(group, entity_id, mode)
        state = self.hass.states.get(entity_id)
        if state is not None and state.state == mode:  # reported straight away
            self._confirmed(group, entity_id)
            return True
        self._cancel_timer(entity_id)
        timeout = self._ac_timeout()
        self._pending[entity_id] = _PendingMode(mode, now + timedelta(seconds=timeout), attempt)
        if self._wake is not None:
            self._timers[entity_id] = async_call_later(self.hass, timeout, self._on_timeout)
        return None

    def _confirmed(self, group: DeviceGroup, entity_id: str) -> None:
        self._pending.pop(entity_id, None)
        self._cancel_timer(entity_id)
        self._failed.discard(entity_id)
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

    async def _async_report_failure(self, entity_id: str, mode: str) -> None:
        """Tell the configured notify services, once per failure episode."""
        if entity_id in self._failed:
            return
        self._failed.add(entity_id)
        message = f"{self.name}: {entity_id} did not switch to {mode}."
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
