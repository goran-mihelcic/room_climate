"""UI setup and reconfiguration for Room Climate."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .const import (
    BOOST_BOTH,
    BOOST_COOLERS,
    BOOST_HEATERS,
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
    POLICY_BLOCK,
    POLICY_FOLLOW,
)

BATH_KEYS = (CONF_BATH_HEATERS, CONF_BATH_FLOOR)


def _temp_selector() -> sel.NumberSelector:
    return sel.NumberSelector(
        sel.NumberSelectorConfig(
            min=5, max=35, step=0.5, unit_of_measurement="°C", mode=sel.NumberSelectorMode.BOX
        )
    )


def _climate(multiple: bool) -> sel.EntitySelector:
    return sel.EntitySelector(sel.EntitySelectorConfig(domain="climate", multiple=multiple))


ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AT_HOME): sel.EntitySelector(
            sel.EntitySelectorConfig(domain=["input_boolean", "binary_sensor", "person", "group"])
        ),
        vol.Required(CONF_HOUSE_STATUS): sel.EntitySelector(
            sel.EntitySelectorConfig(domain=["sensor", "input_select", "select"])
        ),
        vol.Required(CONF_HEATERS): _climate(True),
        vol.Optional(CONF_COOLERS): _climate(True),
        vol.Optional(CONF_ROOM_SENSOR): sel.EntitySelector(
            sel.EntitySelectorConfig(domain="sensor", device_class="temperature")
        ),
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
        vol.Required(CONF_BOOST_METHOD, default=BOOST_HEATERS): sel.SelectSelector(
            sel.SelectSelectorConfig(
                options=[BOOST_HEATERS, BOOST_COOLERS, BOOST_BOTH],
                translation_key="boost_method",
                mode=sel.SelectSelectorMode.LIST,
            )
        ),
        vol.Required(CONF_HAS_BATHROOM, default=False): sel.BooleanSelector(),
    }
)

BATH_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_BATH_HEATERS): _climate(True),
        vol.Optional(CONF_BATH_FLOOR): _climate(False),
    }
)


def _validate_room(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not user_input.get(CONF_HEATERS):
        errors[CONF_HEATERS] = "no_heaters"
    if user_input[CONF_HEAT_MIN] > user_input[CONF_HEAT_MAX]:
        errors[CONF_HEAT_MAX] = "bad_range"
    if user_input[CONF_COOL_MIN] > user_input[CONF_COOL_MAX]:
        errors[CONF_COOL_MAX] = "bad_range"
    if user_input.get(CONF_BOOST_METHOD, BOOST_HEATERS) != BOOST_HEATERS and not user_input.get(
        CONF_COOLERS
    ):
        errors[CONF_BOOST_METHOD] = "boost_needs_cooler"
    managed = set(user_input.get(CONF_HEATERS, [])) & set(user_input.get(CONF_COOLERS, []))
    if managed:
        errors[CONF_COOLERS] = "overlap"
    return errors


def _validate_bath(room: dict[str, Any], user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not user_input.get(CONF_BATH_HEATERS) and not user_input.get(CONF_BATH_FLOOR):
        errors["base"] = "no_bath_devices"
    room_devices = set(room.get(CONF_HEATERS, [])) | set(room.get(CONF_COOLERS, []))
    bath_devices = set(user_input.get(CONF_BATH_HEATERS, []))
    if floor := user_input.get(CONF_BATH_FLOOR):
        bath_devices.add(floor)
    if room_devices & bath_devices:
        errors["base"] = "bath_overlap"
    return errors


class RoomClimateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a room."""

    VERSION = 1

    def __init__(self) -> None:
        self._name = ""
        self._room: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_room(user_input)
            if not errors:
                self._name = user_input.pop(CONF_NAME)
                self._room = user_input
                if user_input.get(CONF_HAS_BATHROOM):
                    return await self.async_step_bathroom()
                return self.async_create_entry(title=self._name, data={}, options=self._room)

        # Pre-fill the house entities from rooms that already exist.
        suggested: dict[str, Any] = dict(user_input or {})
        for entry in self._async_current_entries():
            for key in (CONF_AT_HOME, CONF_HOUSE_STATUS):
                if key not in suggested and entry.options.get(key):
                    suggested[key] = entry.options[key]

        schema = vol.Schema({vol.Required(CONF_NAME): sel.TextSelector()}).extend(
            ROOM_SCHEMA.schema
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_bathroom(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_bath(self._room, user_input)
            if not errors:
                return self.async_create_entry(
                    title=self._name, data={}, options={**self._room, **user_input}
                )
        return self.async_show_form(
            step_id="bathroom",
            data_schema=self.add_suggested_values_to_schema(BATH_SCHEMA, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RoomClimateOptionsFlow()


class RoomClimateOptionsFlow(OptionsFlow):
    """Change devices and settings of an existing room."""

    def __init__(self) -> None:
        self._room: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_room(user_input)
            if not errors:
                self._room = user_input
                if user_input.get(CONF_HAS_BATHROOM):
                    return await self.async_step_bathroom()
                return self.async_create_entry(data=self._room)
        return self.async_show_form(
            step_id="init",
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
