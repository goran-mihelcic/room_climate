"""Sending commands to climate devices without mistaking their echoes for manual changes."""

from __future__ import annotations

import asyncio
import logging
import time as monotonic_time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

AC_MODE_CHECK_SECONDS = 3  # wait this long after set_hvac_mode before checking an A/C
AC_MODE_ATTEMPTS = 2

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


class ClimateDriver:
    """Drives climate entities, remembering which state changes it caused itself."""

    def __init__(
        self, hass: HomeAssistant, name: str, notify_targets: Callable[[], list[str]]
    ) -> None:
        self.hass = hass
        self.name = name
        self._notify_targets = notify_targets
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
                if group.ac:
                    return
            elif not await self._async_set_mode(group, entity_id, mode):
                return  # A/C did not switch: don't send a temperature for the wrong mode
            else:
                sent = True

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
        """Set the hvac mode. A/C units are checked after a pause and retried once."""
        data = {ATTR_ENTITY_ID: entity_id, "hvac_mode": mode}
        if not group.ac:
            return await self._async_call(group, "set_hvac_mode", data)
        for attempt in range(1, AC_MODE_ATTEMPTS + 1):
            await self._async_call(group, "set_hvac_mode", data)
            await asyncio.sleep(AC_MODE_CHECK_SECONDS)
            state = self.hass.states.get(entity_id)
            if state is not None and state.state == mode:
                self._failed.discard(entity_id)
                return True
            _LOGGER.warning(
                "%s: %s is %s after asking for %s (attempt %d)",
                self.name,
                entity_id,
                state.state if state else None,
                mode,
                attempt,
            )
        await self._async_report_failure(entity_id, mode)
        return False

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
            service = target.removeprefix("notify.")
            try:
                await self.hass.services.async_call(
                    "notify",
                    service,
                    {"title": "Room Climate", "message": message},
                    blocking=True,
                )
            except Exception as err:  # a broken notifier must not stop driving devices
                _LOGGER.warning("%s: notify.%s failed: %s", self.name, service, err)

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
