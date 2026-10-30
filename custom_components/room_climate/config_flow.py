"""UI setup and reconfiguration for Room Climate."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector as sel

from .const import (
    CONF_AT_HOME,
    CONF_BATH_FLOOR,
    CONF_BATH_HEATERS,
    CONF_BATH_SCHEDULE,
    CONF_COOL_MAX,
    CONF_COOL_MIN,
    CONF_COOLERS,
    CONF_ENTRY_TYPE,
    CONF_FLOOR_THERMOSTAT,
    CONF_HAS_BATHROOM,
    CONF_HEAT_MAX,
    CONF_HEAT_MIN,
    CONF_HEAT_PUMP,
    CONF_HEATERS,
    CONF_HOT_WATER,
    CONF_HUMIDITY_SENSOR,
    CONF_MANUAL_POLICY,
    CONF_NOTIFY,
    CONF_OUTDOOR,
    CONF_PREPARE,
    CONF_REFERENCE,
    CONF_SCHEDULE,
    CONF_TEMP_SENSOR,
    DEFAULT_COOL_MAX,
    DEFAULT_COOL_MIN,
    DEFAULT_HEAT_MAX,
    DEFAULT_HEAT_MIN,
    DOMAIN,
    POLICY_BLOCK,
    POLICY_FOLLOW,
    SCHEDULE_DOMAINS,
    TYPE_HOUSE,
    TYPE_ROOM,
)

BATH_KEYS = (CONF_BATH_HEATERS, CONF_BATH_FLOOR, CONF_BATH_SCHEDULE)
# Settings (not devices) a new room takes over from the room added before it.
COPIED_ROOM_KEYS = (CONF_MANUAL_POLICY, CONF_HEAT_MIN, CONF_HEAT_MAX, CONF_COOL_MIN, CONF_COOL_MAX)


def _temp_selector() -> sel.NumberSelector:
    return sel.NumberSelector(
        sel.NumberSelectorConfig(
            min=5, max=35, step=0.5, unit_of_measurement="°C", mode=sel.NumberSelectorMode.BOX
        )
    )


def _climate(multiple: bool) -> sel.EntitySelector:
    return sel.EntitySelector(sel.EntitySelectorConfig(domain="climate", multiple=multiple))


def _entity(domain: str | list[str], device_class: str | None = None) -> sel.EntitySelector:
    config = sel.EntitySelectorConfig(domain=domain)
    if device_class:
        config["device_class"] = device_class
    return sel.EntitySelector(config)


HOUSE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OUTDOOR): _entity("sensor", "temperature"),
        vol.Optional(CONF_REFERENCE): _entity("sensor", "temperature"),
        vol.Required(CONF_AT_HOME): _entity(["input_boolean", "binary_sensor", "person", "group"]),
        vol.Optional(CONF_PREPARE): _entity(["input_boolean", "binary_sensor"]),
        vol.Optional(CONF_HEAT_PUMP): _entity(
            ["switch", "input_boolean", "climate", "water_heater"]
        ),
        vol.Optional(CONF_HOT_WATER): _entity(["water_heater", "switch", "input_boolean"]),
        vol.Optional(CONF_NOTIFY): sel.TextSelector(sel.TextSelectorConfig(multiple=True)),
    }
)

ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HEATERS): _climate(True),
        vol.Optional(CONF_COOLERS): _climate(True),
        vol.Optional(CONF_FLOOR_THERMOSTAT): _climate(False),
        vol.Optional(CONF_SCHEDULE): _entity(SCHEDULE_DOMAINS),
        vol.Optional(CONF_TEMP_SENSOR): _entity("sensor", "temperature"),
        vol.Optional(CONF_HUMIDITY_SENSOR): _entity("sensor", "humidity"),
        vol.Required(CONF_MANUAL_POLICY, default=POLICY_FOLLOW): sel.SelectSelector(
            sel.SelectSelectorConfig(
                options=[POLICY_FOLLOW, POLICY_BLOCK],
                translation_key="manual_policy",
                mode=sel.SelectSelectorMode.LIST,
            )
        ),
        vol.Required(CONF_HEAT_MIN, default=DEFAULT_HEAT_MIN): _temp_selector(),
        vol.Required(CONF_HEAT_MAX, default=DEFAULT_HEAT_MAX): _temp_selector(),
        vol.Required(CONF_COOL_MIN, default=DEFAULT_COOL_MIN): _temp_selector(),
        vol.Required(CONF_COOL_MAX, default=DEFAULT_COOL_MAX): _temp_selector(),
        vol.Required(CONF_HAS_BATHROOM, default=False): sel.BooleanSelector(),
    }
)

BATH_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_BATH_HEATERS): _climate(True),
        vol.Optional(CONF_BATH_FLOOR): _climate(False),
        vol.Optional(CONF_BATH_SCHEDULE): _entity(SCHEDULE_DOMAINS),
    }
)


def _validate_house(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    targets = [t.strip().removeprefix("notify.") for t in user_input.get(CONF_NOTIFY) or []]
    user_input[CONF_NOTIFY] = [t for t in targets if t]
    for target in user_input[CONF_NOTIFY]:
        # A classic notify action (notify.mobile_app_x) or a notify entity (a notify group)
        if not hass.services.has_service("notify", target) and (
            hass.states.get(f"notify.{target}") is None
        ):
            return {CONF_NOTIFY: "unknown_notify"}
    return {}


def _validate_room(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not user_input.get(CONF_HEATERS):
        errors[CONF_HEATERS] = "no_heaters"
    if user_input[CONF_HEAT_MIN] > user_input[CONF_HEAT_MAX]:
        errors[CONF_HEAT_MAX] = "bad_range"
    if user_input[CONF_COOL_MIN] > user_input[CONF_COOL_MAX]:
        errors[CONF_COOL_MAX] = "bad_range"
    room_devices = set(user_input.get(CONF_HEATERS, [])) | set(user_input.get(CONF_COOLERS, []))
    if set(user_input.get(CONF_HEATERS, [])) & set(user_input.get(CONF_COOLERS, [])):
        errors[CONF_COOLERS] = "overlap"
    if user_input.get(CONF_FLOOR_THERMOSTAT) in room_devices:
        errors[CONF_FLOOR_THERMOSTAT] = "thermostat_overlap"
    return errors


def _validate_bath(room: dict[str, Any], user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not user_input.get(CONF_BATH_HEATERS) and not user_input.get(CONF_BATH_FLOOR):
        errors["base"] = "no_bath_devices"
    room_devices = set(room.get(CONF_HEATERS, [])) | set(room.get(CONF_COOLERS, []))
    if thermostat := room.get(CONF_FLOOR_THERMOSTAT):
        room_devices.add(thermostat)
    bath_devices = set(user_input.get(CONF_BATH_HEATERS, []))
    if floor := user_input.get(CONF_BATH_FLOOR):
        bath_devices.add(floor)
    if room_devices & bath_devices:
        errors["base"] = "bath_overlap"
    return errors


class RoomClimateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add the house (first time) or a room."""

    VERSION = 2

    def __init__(self) -> None:
        self._name = ""
        self._room: dict[str, Any] = {}

    def _house_exists(self) -> bool:
        return any(e.data.get(CONF_ENTRY_TYPE) == TYPE_HOUSE for e in self._async_current_entries())

    def _last_room_settings(self) -> dict[str, Any]:
        rooms = [
            e for e in self._async_current_entries() if e.data.get(CONF_ENTRY_TYPE) == TYPE_ROOM
        ]
        if not rooms:
            return {}
        return {k: rooms[-1].options[k] for k in COPIED_ROOM_KEYS if k in rooms[-1].options}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._house_exists():
            return await self.async_step_room()
        return await self.async_step_house()

    async def async_step_house(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._house_exists():
            return self.async_abort(reason="house_exists")
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_house(self.hass, user_input)
            if not errors:
                name = user_input.pop(CONF_NAME)
                return self.async_create_entry(
                    title=name, data={CONF_ENTRY_TYPE: TYPE_HOUSE}, options=user_input
                )
        schema = vol.Schema({vol.Required(CONF_NAME, default="House"): sel.TextSelector()}).extend(
            HOUSE_SCHEMA.schema
        )
        return self.async_show_form(
            step_id="house",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_room(user_input)
            if not errors:
                self._name = user_input.pop(CONF_NAME)
                self._room = user_input
                if user_input.get(CONF_HAS_BATHROOM):
                    return await self.async_step_bathroom()
                return self._create_room(self._room)
        schema = vol.Schema({vol.Required(CONF_NAME): sel.TextSelector()}).extend(
            ROOM_SCHEMA.schema
        )
        return self.async_show_form(
            step_id="room",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or self._last_room_settings()
            ),
            errors=errors,
        )

    async def async_step_bathroom(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_bath(self._room, user_input)
            if not errors:
                return self._create_room({**self._room, **user_input})
        return self.async_show_form(
            step_id="bathroom",
            data_schema=self.add_suggested_values_to_schema(BATH_SCHEMA, user_input or {}),
            errors=errors,
        )

    def _create_room(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._name, data={CONF_ENTRY_TYPE: TYPE_ROOM}, options=options
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RoomClimateOptionsFlow()


class RoomClimateOptionsFlow(OptionsFlow):
    """Change the house inputs, or the devices and settings of a room."""

    def __init__(self) -> None:
        self._room: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self.config_entry.data.get(CONF_ENTRY_TYPE) == TYPE_HOUSE:
            return await self.async_step_house()
        return await self.async_step_room()

    async def async_step_house(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_house(self.hass, user_input)
            if not errors:
                return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="house",
            data_schema=self.add_suggested_values_to_schema(
                HOUSE_SCHEMA, user_input or dict(self.config_entry.options)
            ),
            errors=errors,
        )

    async def async_step_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_room(user_input)
            if not errors:
                self._room = user_input
                if user_input.get(CONF_HAS_BATHROOM):
                    return await self.async_step_bathroom()
                return self.async_create_entry(data=self._room)
        return self.async_show_form(
            step_id="room",
            data_schema=self.add_suggested_values_to_schema(
                ROOM_SCHEMA, user_input or dict(self.config_entry.options)
            ),
            errors=errors,
        )

    async def async_step_bathroom(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_bath(self._room, user_input)
            if not errors:
                return self.async_create_entry(data={**self._room, **user_input})
        current = {k: v for k, v in self.config_entry.options.items() if k in BATH_KEYS}
        return self.async_show_form(
            step_id="bathroom",
            data_schema=self.add_suggested_values_to_schema(BATH_SCHEMA, user_input or current),
            errors=errors,
        )
