"""End-to-end tests of Room Climate against a real Home Assistant core."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.room_climate import controller as ctl_mod
from custom_components.room_climate.const import DOMAIN

TRV_LR = "climate.smart_radiator_thermostat_lr"
TRV_DIN = "climate.smart_radiator_thermostat_dining"
THERMO = "climate.smart_thermostat_x"
AC = "climate.living_room_air"
BATH_TRV = "climate.bath3_radiator"
FLOOR = "climate.floor_heating_bth3"

OPTIONS = {
    "at_home_entity": "input_boolean.at_home",
    "house_status_entity": "sensor.home_climate_status",
    "heaters": [THERMO, TRV_LR, TRV_DIN],
    "coolers": [AC],
    "room_sensor": "sensor.living_temp",
    "manual_policy": "follow",
    "manual_heat_min": 16.0,
    "manual_heat_max": 24.0,
    "manual_cool_min": 22.0,
    "manual_cool_max": 28.0,
    "has_bathroom": True,
    "bath_heaters": [BATH_TRV],
    "bath_floor": FLOOR,
}


def _climate(hass, entity_id, mode, temp, modes=("off", "heat")):
    hass.states.async_set(
        entity_id,
        mode,
        {
            "hvac_modes": list(modes),
            "temperature": temp,
            "min_temp": 5,
            "max_temp": 30,
            "target_temp_step": 0.5,
            "current_temperature": 20,
        },
    )


@pytest.fixture
async def house(hass: HomeAssistant, freezer):
    """Fake devices that obey climate service calls, plus house entities."""
    await hass.config.async_set_time_zone("Europe/Zagreb")
    freezer.move_to("2026-11-03 10:00:00+01:00")
    ctl_mod.COMMAND_GRACE_SECONDS = 0

    for e in (THERMO, TRV_LR, TRV_DIN, BATH_TRV, FLOOR):
        _climate(hass, e, "off", 15)
    _climate(hass, AC, "off", 24, modes=("off", "cool", "heat", "dry"))
    hass.states.async_set("input_boolean.at_home", "on")
    hass.states.async_set("sensor.home_climate_status", "Heating")
    hass.states.async_set("sensor.living_temp", "20.5", {"device_class": "temperature"})

    calls: list[tuple[str, dict]] = []

    async def set_hvac_mode(call):
        calls.append(("set_hvac_mode", dict(call.data)))
        st = hass.states.get(call.data["entity_id"])
        hass.states.async_set(
            call.data["entity_id"], call.data["hvac_mode"], st.attributes, context=call.context
        )

    async def set_temperature(call):
        calls.append(("set_temperature", dict(call.data)))
        st = hass.states.get(call.data["entity_id"])
        hass.states.async_set(
            call.data["entity_id"],
            st.state,
            {**st.attributes, "temperature": call.data["temperature"]},
            context=call.context,
        )

    hass.services.async_register("climate", "set_hvac_mode", set_hvac_mode)
    hass.services.async_register("climate", "set_temperature", set_temperature)
    return calls


async def _settle(hass, freezer, seconds=3):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _setup(hass, freezer, **overrides) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Living Room", data={}, options={**OPTIONS, **overrides}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _settle(hass, freezer)
    return entry


def _st(hass, entity_id):
    st = hass.states.get(entity_id)
    return st.state, st.attributes.get("temperature")


# ---------------------------------------------------------------------------


async def test_config_flow_with_bathroom(hass: HomeAssistant, house) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    room = {k: v for k, v in OPTIONS.items() if k not in ("bath_heaters", "bath_floor")}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Living Room", **room}
    )
    assert result["step_id"] == "bathroom"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"bath_heaters": [BATH_TRV], "bath_floor": FLOOR}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room"
    assert result["options"]["bath_floor"] == FLOOR

    # second room gets house entities pre-filled
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    schema = result["data_schema"].schema
    suggested = {str(k): k.description.get("suggested_value") for k in schema if k.description}
    assert suggested["at_home_entity"] == "input_boolean.at_home"


async def test_config_flow_rejects_overlap(hass: HomeAssistant, house) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    room = {k: v for k, v in OPTIONS.items() if k not in ("bath_heaters", "bath_floor")}
    room["coolers"] = [THERMO]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "X", **room}
    )
    assert result["errors"] == {"coolers": "overlap"}


async def test_heating_comfort_and_bathroom_follow(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    for e in (THERMO, TRV_LR, TRV_DIN):
        assert _st(hass, e) == ("heat", 21.0)
    assert _st(hass, AC)[0] == "off"
    # 10:00 is outside the bath periods -> bathroom follows room, floor off
    assert _st(hass, BATH_TRV) == ("heat", 21.0)
    assert _st(hass, FLOOR)[0] == "off"
    assert hass.states.get("sensor.living_room_status").state == "comfort"
    assert hass.states.get("sensor.living_room_bathroom_status").state == "follow_room"


async def test_bath_period(hass, house, freezer) -> None:
    freezer.move_to("2026-11-03 07:00:00+01:00")
    await _setup(hass, freezer)
    assert _st(hass, BATH_TRV) == ("heat", 24.0)
    assert _st(hass, FLOOR) == ("heat", 27.0)
    assert hass.states.get("sensor.living_room_bathroom_status").state == "bath_period"
    # period ends at 08:30 -> follows room again, floor off
    freezer.move_to("2026-11-03 08:31:00+01:00")
    await _settle(hass, freezer, seconds=61)
    assert _st(hass, BATH_TRV) == ("heat", 21.0)
    assert _st(hass, FLOOR)[0] == "off"


async def test_setback_at_night(hass, house, freezer) -> None:
    freezer.move_to("2026-11-03 02:00:00+01:00")
    await _setup(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 19.0)
    assert hass.states.get("sensor.living_room_status").state == "setback"


async def test_away_frost_protection(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    hass.states.async_set("input_boolean.at_home", "off")
    await _settle(hass, freezer)
    for e in (THERMO, TRV_LR, TRV_DIN, BATH_TRV):
        assert _st(hass, e) == ("heat", 12.0)
    assert _st(hass, FLOOR)[0] == "off"
    assert hass.states.get("sensor.living_room_status").state == "frost_protection"


async def test_house_frost_and_comfort_states(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    hass.states.async_set("sensor.home_climate_status", "Frost protection")
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 12.0)
    hass.states.async_set("sensor.home_climate_status", "Comfort")
    await _settle(hass, freezer)
    for e in (THERMO, TRV_LR, TRV_DIN, AC, BATH_TRV, FLOOR):
        assert _st(hass, e)[0] == "off"


async def test_disabled_room(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.living_room_enabled"}, blocking=True
    )
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 12.0)


async def test_cooling_and_kid_sets_ac_to_18(hass, house, freezer) -> None:
    hass.states.async_set("sensor.home_climate_status", "Cooling")
    await _setup(hass, freezer)
    assert _st(hass, AC) == ("cool", 25.0)
    for e in (THERMO, TRV_LR, TRV_DIN, BATH_TRV, FLOOR):
        assert _st(hass, e)[0] == "off"

    # someone uses the AC remote: 18 °C -> clamped to 22
    st = hass.states.get(AC)
    hass.states.async_set(AC, "cool", {**st.attributes, "temperature": 18})
    await _settle(hass, freezer)
    assert _st(hass, AC) == ("cool", 22.0)
    assert hass.states.get("sensor.living_room_status").state == "manual"

    # reasonable change is adopted as-is
    st = hass.states.get(AC)
    hass.states.async_set(AC, "cool", {**st.attributes, "temperature": 23.5})
    await _settle(hass, freezer)
    assert _st(hass, AC) == ("cool", 23.5)

    # comfort period ends at 23:00 -> manual target dropped, setback applies
    freezer.move_to("2026-11-03 23:01:00+01:00")
    await _settle(hass, freezer, seconds=61)
    assert _st(hass, AC) == ("cool", 27.0)


async def test_ac_turned_on_in_heating_is_reverted(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    st = hass.states.get(AC)
    hass.states.async_set(AC, "cool", {**st.attributes, "temperature": 18})
    await _settle(hass, freezer)
    assert _st(hass, AC)[0] == "off"


async def test_block_policy(hass, house, freezer) -> None:
    await _setup(hass, freezer, manual_policy="block")
    st = hass.states.get(TRV_LR)
    hass.states.async_set(TRV_LR, "heat", {**st.attributes, "temperature": 26})
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_follow_heating_applies_to_whole_room(hass, house, freezer) -> None:
    hass.states.async_set("sensor.living_temp", "22.5", {"device_class": "temperature"})
    await _setup(hass, freezer)
    st = hass.states.get(TRV_LR)
    hass.states.async_set(TRV_LR, "heat", {**st.attributes, "temperature": 23})
    await _settle(hass, freezer)
    for e in (THERMO, TRV_LR, TRV_DIN):
        assert _st(hass, e) == ("heat", 23.0)
    # bathroom follows the room target outside its periods
    assert _st(hass, BATH_TRV) == ("heat", 23.0)


async def test_radiator_turned_off_manually_is_respected(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    st = hass.states.get(TRV_DIN)
    hass.states.async_set(TRV_DIN, "off", st.attributes)
    await _settle(hass, freezer)
    assert _st(hass, TRV_DIN)[0] == "off"
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_boost(hass, house, freezer) -> None:
    hass.states.async_set("sensor.living_temp", "18.0", {"device_class": "temperature"})
    await _setup(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    assert hass.states.get("sensor.living_room_status").state == "boost"
    # still below target but inside exit margin? no: 20.0 -> still boosting (hysteresis)
    hass.states.async_set("sensor.living_temp", "20.0", {"device_class": "temperature"})
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    hass.states.async_set("sensor.living_temp", "20.8", {"device_class": "temperature"})
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_override_expires(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.living_room_override_duration", "value": 30},
        blocking=True,
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.living_room_override"}, blocking=True
    )
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 22.0)
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await _settle(hass, freezer)
    assert hass.states.get("switch.living_room_override").state == "off"
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_setpoint_change_and_time_entity(hass, house, freezer) -> None:
    hass.states.async_set("sensor.living_temp", "21.5", {"device_class": "temperature"})
    await _setup(hass, freezer)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.living_room_heating_setpoint", "value": 22.5},
        blocking=True,
    )
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 22.5)
    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": "time.living_room_comfort_start", "time": "11:00:00"},
        blocking=True,
    )
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 20.5)


async def test_waiting_when_status_unavailable(hass, house, freezer) -> None:
    hass.states.async_set("sensor.home_climate_status", "unavailable")
    await _setup(hass, freezer)
    assert house == []  # nothing commanded
    assert hass.states.get("sensor.living_room_status").state == "waiting"


async def test_options_flow_reload(hass, house, freezer) -> None:
    entry = await _setup(hass, freezer)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    room = {k: v for k, v in OPTIONS.items() if k not in ("bath_heaters", "bath_floor")}
    room["has_bathroom"] = False
    room["manual_policy"] = "block"
    result = await hass.config_entries.options.async_configure(result["flow_id"], room)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["manual_policy"] == "block"
    assert "bath_floor" not in entry.options
    assert (
        hass.states.get("sensor.living_room_bathroom_status") is None
        or hass.states.get("sensor.living_room_bathroom_status").state == "unavailable"
    )


async def test_manual_raise_far_above_room_triggers_boost(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    st = hass.states.get(TRV_LR)
    hass.states.async_set(TRV_LR, "heat", {**st.attributes, "temperature": 23})
    await _settle(hass, freezer)
    assert hass.states.get("sensor.living_room_target_temperature").state == "23.0"
    assert _st(hass, TRV_DIN) == ("heat", 26.0)


async def test_own_command_echo_is_not_manual(hass, house, freezer) -> None:
    """A device reporting our command back must never become a manual target."""
    await _setup(hass, freezer)
    assert (
        hass.states.get("sensor.living_room_target_temperature").attributes["manual_target"] is None
    )


async def test_restore_after_restart(hass, house, freezer) -> None:
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (State("switch.living_room_override", "on", {"until": future}), {}),
            (State("switch.living_room_enabled", "on"), {}),
            (State("time.living_room_comfort_start", "05:00:00"), {}),
            (
                State("number.living_room_override_temperature", "23.5"),
                {
                    "native_value": 23.5,
                    "native_min_value": 10,
                    "native_max_value": 32,
                    "native_step": 0.5,
                    "native_unit_of_measurement": "°C",
                },
            ),
        ],
    )
    hass.states.async_set("sensor.living_temp", "23.0", {"device_class": "temperature"})
    await _setup(hass, freezer)
    assert hass.states.get("switch.living_room_override").state == "on"
    assert hass.states.get("time.living_room_comfort_start").state == "05:00:00"
    assert _st(hass, TRV_LR) == ("heat", 23.5)


async def test_boost_with_ac_keeps_radiators_at_target(hass, house, freezer) -> None:
    hass.states.async_set("sensor.living_temp", "16.2", {"device_class": "temperature"})
    await _setup(hass, freezer, boost_method="coolers", has_bathroom=False)
    for e in (THERMO, TRV_LR, TRV_DIN):
        assert _st(hass, e) == ("heat", 21.0)
    assert _st(hass, AC) == ("heat", 21.0)
    assert hass.states.get("sensor.living_room_status").state == "boost"

    # kid sets the AC to 30 during boost -> reverted, not adopted
    st = hass.states.get(AC)
    hass.states.async_set(AC, "heat", {**st.attributes, "temperature": 30})
    await _settle(hass, freezer)
    assert _st(hass, AC) == ("heat", 21.0)
    assert _st(hass, TRV_LR) == ("heat", 21.0)

    # room reaches target -> AC off, radiators unchanged
    hass.states.async_set("sensor.living_temp", "20.8", {"device_class": "temperature"})
    await _settle(hass, freezer)
    assert _st(hass, AC)[0] == "off"
    assert _st(hass, TRV_LR) == ("heat", 21.0)
    assert hass.states.get("sensor.living_room_status").state == "comfort"


async def test_boost_both(hass, house, freezer) -> None:
    hass.states.async_set("sensor.living_temp", "16.2", {"device_class": "temperature"})
    await _setup(hass, freezer, boost_method="both", has_bathroom=False)
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    assert _st(hass, AC) == ("heat", 21.0)


async def test_boost_method_select_on_device(hass, house, freezer) -> None:
    hass.states.async_set("sensor.living_temp", "16.2", {"device_class": "temperature"})
    await _setup(hass, freezer, has_bathroom=False)
    sel = "select.living_room_boost_method"
    assert hass.states.get(sel).state == "heaters"
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": sel, "option": "coolers"}, blocking=True
    )
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 21.0)
    assert _st(hass, AC) == ("heat", 21.0)


async def test_no_boost_select_without_cooler(hass, house, freezer) -> None:
    await _setup(hass, freezer, coolers=[], has_bathroom=False)
    assert hass.states.get("select.living_room_boost_method") is None
