"""End-to-end tests of Room Climate against a real Home Assistant core."""

from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.room_climate import controller as ctl_mod
from custom_components.room_climate.const import DOMAIN

TRV_LR = "climate.smart_radiator_thermostat_lr"
TRV_DIN = "climate.smart_radiator_thermostat_dining"
TRV_BED = "climate.smart_radiator_thermostat_bedroom"
THERMO = "climate.smart_thermostat_x"
AC = "climate.living_room_air"
BATH_TRV = "climate.bath3_radiator"
FLOOR = "climate.floor_heating_bth3"

SEASON = "input_select.hvac_season"
AT_HOME = "input_boolean.at_home"
PREPARE = "input_boolean.prepare_house"
SCHEDULE = "schedule.living_room"
BATH_SCHEDULE = "schedule.living_room_bathroom"
TEMP = "sensor.living_temp"
HUMIDITY = "sensor.living_humidity"

HOUSE_OPTIONS = {
    "season_entity": SEASON,
    "at_home_entity": AT_HOME,
    "prepare_entity": PREPARE,
    "notify_services": ["mobile_app_phone"],
}
ROOM_OPTIONS = {
    "heaters": [TRV_LR, TRV_DIN],
    "coolers": [AC],
    "floor_thermostat": THERMO,
    "schedule": SCHEDULE,
    "temperature_sensor": TEMP,
    "humidity_sensor": HUMIDITY,
    "manual_policy": "follow",
    "manual_heat_min": 16.0,
    "manual_heat_max": 24.0,
    "manual_cool_min": 22.0,
    "manual_cool_max": 28.0,
    "has_bathroom": True,
    "bath_heaters": [BATH_TRV],
    "bath_floor": FLOOR,
    "bath_schedule": BATH_SCHEDULE,
}
BATH_KEYS = ("bath_heaters", "bath_floor", "bath_schedule")


def _climate(hass, entity_id, mode, temp, modes=("off", "heat"), **extra):
    hass.states.async_set(
        entity_id,
        mode,
        {
            "hvac_modes": list(modes),
            "temperature": temp,
            "min_temp": 5,
            "max_temp": 30,
            "target_temp_step": 0.5,
            # below the room sensor, so radiators get no correction unless a test sets one
            "current_temperature": 15,
            **extra,
        },
    )


@pytest.fixture
async def house(hass: HomeAssistant, freezer):
    """Fake devices that obey climate service calls, plus house entities."""
    await hass.config.async_set_time_zone("Europe/Zagreb")
    freezer.move_to("2026-11-03 10:00:00+01:00")
    ctl_mod.COMMAND_GRACE_SECONDS = 0

    for e in (THERMO, TRV_LR, TRV_DIN, TRV_BED, BATH_TRV, FLOOR):
        _climate(hass, e, "off", 15)
    _climate(
        hass,
        AC,
        "off",
        24,
        modes=("off", "cool", "heat", "dry"),
        fan_modes=["auto", "low", "high"],
        fan_mode="low",
    )
    hass.states.async_set(SEASON, "Heating")
    hass.states.async_set(AT_HOME, "on")
    hass.states.async_set(PREPARE, "off")
    hass.states.async_set(SCHEDULE, "on")
    hass.states.async_set(BATH_SCHEDULE, "off")
    hass.states.async_set(TEMP, "20.5", {"device_class": "temperature"})
    hass.states.async_set(HUMIDITY, "50", {"device_class": "humidity"})

    # stuck: hvac mode never changes; broken: the device's integration raises a bug;
    # linked: {a: b} copies a setpoint sent to a onto b, like heads of one Tado room
    fake = SimpleNamespace(calls=[], notes=[], stuck=set(), broken=set(), linked={})

    async def set_hvac_mode(call):
        fake.calls.append(("set_hvac_mode", dict(call.data)))
        if call.data["entity_id"] in fake.stuck:
            return
        st = hass.states.get(call.data["entity_id"])
        hass.states.async_set(
            call.data["entity_id"], call.data["hvac_mode"], st.attributes, context=call.context
        )

    def _setter(service, attribute):
        async def handler(call):
            fake.calls.append((service, dict(call.data)))
            if call.data["entity_id"] in fake.broken:
                raise TypeError("'>=' not supported between instances of 'NoneType' and 'int'")
            st = hass.states.get(call.data["entity_id"])
            hass.states.async_set(
                call.data["entity_id"],
                st.state,
                {**st.attributes, attribute: call.data[attribute]},
                context=call.context,
            )
            if (partner := fake.linked.get(call.data["entity_id"])) and attribute == "temperature":
                # A Tado room has one mode and one setpoint: the device copies both, not
                # with our context.
                other = hass.states.get(partner)
                hass.states.async_set(
                    partner, st.state, {**other.attributes, attribute: call.data[attribute]}
                )

        return handler

    async def notify(call):
        fake.notes.append(dict(call.data))

    hass.services.async_register("climate", "set_hvac_mode", set_hvac_mode)
    hass.services.async_register(
        "climate", "set_temperature", _setter("set_temperature", "temperature")
    )
    hass.services.async_register("climate", "set_fan_mode", _setter("set_fan_mode", "fan_mode"))
    hass.services.async_register("notify", "mobile_app_phone", notify)
    return fake


async def _settle(hass, freezer, seconds=3):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def _add(hass, title, entry_type, options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title=title, version=2, data={"type": entry_type}, options=options
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _setup(hass, freezer, with_house=True, **overrides) -> MockConfigEntry:
    if with_house:
        await _add(hass, "House", "house", HOUSE_OPTIONS)
    entry = await _add(hass, "Living Room", "room", {**ROOM_OPTIONS, **overrides})
    await _settle(hass, freezer)
    return entry


async def _set(hass, freezer, entity_id, state=None, **attrs):
    """Change a state as a person or another integration would (no context of ours)."""
    st = hass.states.get(entity_id)
    hass.states.async_set(
        entity_id, st.state if state is None else state, {**st.attributes, **attrs}
    )
    await _settle(hass, freezer)


async def _service(hass, freezer, domain, service, entity_id, **data):
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)
    await _settle(hass, freezer)


def _st(hass, entity_id):
    st = hass.states.get(entity_id)
    return st.state, st.attributes.get("temperature")


def _status(hass, entity_id="sensor.living_room_status"):
    return hass.states.get(entity_id).state


def _attr(hass, name):
    return hass.states.get("sensor.living_room_target_temperature").attributes[name]


# ---- setup dialogs ---------------------------------------------------------


async def test_config_flow_house_then_room_with_bathroom(hass: HomeAssistant, house) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "house"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "House", **HOUSE_OPTIONS, "notify_services": ["notify.mobile_app_phone"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"type": "house"}
    assert result["options"]["notify_services"] == ["mobile_app_phone"]

    # with a house in place, the next one is a room
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "room"
    room = {k: v for k, v in ROOM_OPTIONS.items() if k not in BATH_KEYS}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Living Room", **room}
    )
    assert result["step_id"] == "bathroom"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {k: ROOM_OPTIONS[k] for k in BATH_KEYS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room"
    assert result["data"] == {"type": "room"}
    assert result["options"]["bath_schedule"] == BATH_SCHEDULE


async def test_config_flow_errors(hass: HomeAssistant, house) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "House", **HOUSE_OPTIONS, "notify_services": ["nope"]}
    )
    assert result["errors"] == {"notify_services": "unknown_notify"}

    MockConfigEntry(domain=DOMAIN, version=2, data={"type": "house"}).add_to_hass(hass)
    room = {k: v for k, v in ROOM_OPTIONS.items() if k not in BATH_KEYS}
    for change, error in (
        ({"coolers": [TRV_LR]}, {"coolers": "overlap"}),
        ({"floor_thermostat": TRV_DIN}, {"floor_thermostat": "thermostat_overlap"}),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "X", **room, **change}
        )
        assert result["errors"] == error


async def test_new_room_form_takes_settings_from_last_room(hass: HomeAssistant, house) -> None:
    MockConfigEntry(domain=DOMAIN, version=2, data={"type": "house"}).add_to_hass(hass)
    for policy, heat_min in (("follow", 16.0), ("block", 17.5)):
        options = {**ROOM_OPTIONS, "manual_policy": policy, "manual_heat_min": heat_min}
        MockConfigEntry(
            domain=DOMAIN, version=2, data={"type": "room"}, options=options
        ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "room"
    suggested = {
        str(k): k.description.get("suggested_value")
        for k in result["data_schema"].schema
        if k.description
    }
    assert suggested["manual_policy"] == "block"  # from the room added last
    assert suggested["manual_heat_min"] == 17.5
    assert suggested["manual_cool_max"] == 28.0
    # devices and the bathroom are never copied
    for key in ("heaters", "coolers", "floor_thermostat", "schedule", "has_bathroom"):
        assert suggested.get(key) is None


async def test_devices_are_registered(hass: HomeAssistant, house, freezer) -> None:
    entry = await _setup(hass, freezer)
    devices = dr.async_get(hass)

    def device(entry_id, suffix=""):
        found = dr.async_entries_for_config_entry(devices, entry_id)
        return next(d for d in found if (DOMAIN, entry_id + suffix) in d.identifiers)

    room = device(entry.entry_id)
    bath = device(entry.entry_id, "_bath")
    assert room.name == "Living Room"
    assert bath.via_device_id == room.id
    house_entry = next(
        e for e in hass.config_entries.async_entries(DOMAIN) if e.data["type"] == "house"
    )
    assert device(house_entry.entry_id).name == "House"
    entities = er.async_get(hass)
    assert entities.async_get("switch.living_room_in_use").device_id == room.id
    assert entities.async_get("sensor.living_room_bathroom_status").device_id == bath.id


async def test_old_entry_must_be_re_added(hass: HomeAssistant, house) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Old", version=1, options=ROOM_OPTIONS)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_options_flows(hass, house, freezer) -> None:
    entry = await _setup(hass, freezer)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "room"
    room = {k: v for k, v in ROOM_OPTIONS.items() if k not in BATH_KEYS}
    room["has_bathroom"] = False
    room["manual_policy"] = "block"
    result = await hass.config_entries.options.async_configure(result["flow_id"], room)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["manual_policy"] == "block"
    assert "bath_floor" not in entry.options
    bath = hass.states.get("sensor.living_room_bathroom_status")
    assert bath is None or bath.state == "unavailable"
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.name for d in devices] == ["Living Room"]  # bathroom device removed

    house_entry = next(
        e for e in hass.config_entries.async_entries(DOMAIN) if e.data["type"] == "house"
    )
    result = await hass.config_entries.options.async_init(house_entry.entry_id)
    assert result["step_id"] == "house"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**HOUSE_OPTIONS, "notify_services": []}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert house_entry.options["notify_services"] == []
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 21.0)  # room picks the reloaded house up again


# ---- heating season --------------------------------------------------------


async def test_heating_on_schedule(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    for e in (TRV_LR, TRV_DIN):
        assert _st(hass, e) == ("heat", 21.0)
    assert _st(hass, AC)[0] == "off"
    # bathroom schedule is off -> bathroom follows the room, floor off
    assert _st(hass, BATH_TRV) == ("heat", 21.0)
    assert _st(hass, FLOOR)[0] == "off"
    assert _st(hass, THERMO) == ("heat", 21.0)
    assert _status(hass) == "comfort"
    assert _status(hass, "sensor.living_room_bathroom_status") == "follow_room"
    season = hass.states.get("sensor.house_season")
    assert season.state == "heating"
    assert season.attributes["floor_thermostats"] == {THERMO: 21.0}


async def test_season_sensor_shows_source(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Frost Protection")
    await _setup(hass, freezer)
    season = hass.states.get("sensor.house_season")
    assert season.state == "frost_protection"
    assert season.attributes["season_entity"] == SEASON
    assert season.attributes["season_entity_state"] == "Frost Protection"


async def test_device_integration_error_does_not_stop_the_room(hass, house, freezer) -> None:
    house.broken.add(TRV_LR)
    await _setup(hass, freezer)
    assert _st(hass, TRV_LR)[1] == 15  # its integration crashed on set_temperature
    # everything after it in the room is still driven
    assert _st(hass, TRV_DIN) == ("heat", 21.0)
    assert _st(hass, BATH_TRV) == ("heat", 21.0)
    assert _st(hass, THERMO) == ("heat", 21.0)


async def test_debug_log_file(hass, house, freezer, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    await _setup(hass, freezer)
    switch = "switch.house_debug_log_file"
    assert hass.states.get(switch).state == "off"
    await _service(hass, freezer, "switch", "turn_on", switch)
    assert hass.states.get(switch).state == "on"
    assert hass.states.get(switch).attributes["path"] == str(tmp_path / "room_climate.log")
    await _set(hass, freezer, SCHEDULE, "off")
    await _service(hass, freezer, "switch", "turn_off", switch)
    text = await hass.async_add_executor_job((tmp_path / "room_climate.log").read_text)
    assert "Debug log file started" in text
    assert "Living Room: eco (heat, heating) target=19.0" in text
    assert "climate.set_temperature" in text
    logger = logging.getLogger("custom_components.room_climate")
    assert logger.propagate and not logger.handlers  # Home Assistant's log is back as it was


async def test_heating_off_schedule_uses_eco(hass, house, freezer) -> None:
    hass.states.async_set(SCHEDULE, "off")
    await _setup(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 19.0)
    assert _st(hass, THERMO) == ("heat", 19.0)
    assert _status(hass) == "eco"
    await _service(
        hass, freezer, "number", "set_value", "number.living_room_eco_temperature", value=18.5
    )
    assert _st(hass, TRV_LR) == ("heat", 18.5)
    await _set(hass, freezer, SCHEDULE, "on")
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_room_without_schedule_is_always_on_schedule(hass, house, freezer) -> None:
    await _setup(hass, freezer, schedule=None)
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_comfort_temperature_number(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "21.5", {"device_class": "temperature"})
    await _setup(hass, freezer)
    await _service(
        hass, freezer, "number", "set_value", "number.living_room_comfort_temperature", value=22.5
    )
    assert _st(hass, TRV_LR) == ("heat", 22.5)


async def test_bath_schedule(hass, house, freezer) -> None:
    hass.states.async_set(BATH_SCHEDULE, "on")
    await _setup(hass, freezer)
    assert _st(hass, BATH_TRV) == ("heat", 24.0)
    assert _st(hass, FLOOR) == ("heat", 27.0)
    assert _status(hass, "sensor.living_room_bathroom_status") == "bath_period"
    # bathroom radiators are on the same floor: the pump follows them too
    assert _st(hass, THERMO) == ("heat", 24.0)
    await _set(hass, freezer, BATH_SCHEDULE, "off")
    assert _st(hass, BATH_TRV) == ("heat", 21.0)
    assert _st(hass, FLOOR)[0] == "off"
    assert _st(hass, THERMO) == ("heat", 21.0)


async def test_room_not_in_use_holds_frost(hass, house, freezer) -> None:
    hass.states.async_set(BATH_SCHEDULE, "on")
    await _setup(hass, freezer)
    await _service(hass, freezer, "switch", "turn_off", "switch.living_room_in_use")
    for e in (TRV_LR, TRV_DIN, BATH_TRV):
        assert _st(hass, e) == ("heat", 12.0)
    assert _st(hass, FLOOR)[0] == "off"
    assert _st(hass, AC)[0] == "off"
    assert _st(hass, THERMO) == ("heat", 12.0)
    assert _status(hass) == "frost_protection"


async def test_heating_but_nobody_home_is_frost(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    await _set(hass, freezer, AT_HOME, "off")
    assert _st(hass, TRV_LR) == ("heat", 12.0)
    assert _status(hass) == "frost_protection"


async def test_floor_thermostat_takes_highest_radiator_of_all_rooms(hass, house, freezer) -> None:
    _climate(hass, TRV_BED, "off", 15, current_temperature=22.5)  # warm: no boost
    await _setup(hass, freezer)
    await _add(hass, "Bedroom", "room", {"heaters": [TRV_BED], "floor_thermostat": THERMO})
    await _settle(hass, freezer)
    await _service(
        hass, freezer, "number", "set_value", "number.bedroom_comfort_temperature", value=23
    )
    assert _st(hass, TRV_BED) == ("heat", 23.0)
    assert _st(hass, TRV_LR) == ("heat", 21.0)
    assert _st(hass, THERMO) == ("heat", 23.0)
    await _service(hass, freezer, "switch", "turn_off", "switch.bedroom_in_use")
    assert _st(hass, TRV_BED) == ("heat", 12.0)
    assert _st(hass, THERMO) == ("heat", 21.0)


# ---- radiator correction -----------------------------------------------------


async def test_radiator_reading_high_is_corrected(hass, house, freezer) -> None:
    _climate(hass, TRV_LR, "off", 15, current_temperature=22.5)
    _climate(hass, TRV_DIN, "off", 15, current_temperature=22.5)
    await _setup(hass, freezer)
    # radiators read 2 °C above the room sensor -> setpoint 21 + 2
    assert _st(hass, TRV_LR) == ("heat", 23.0)
    assert _st(hass, TRV_DIN) == ("heat", 23.0)
    assert _attr(hass, "radiator_corrections") == {TRV_LR: 2.0, TRV_DIN: 2.0}
    assert _st(hass, THERMO) == ("heat", 23.0)
    # whole 0.5 °C steps, rounded down: never more than the difference
    for reading, setpoint in (
        (22.8, 23.0),  # 2.3 -> 2.0
        (21.3, 21.5),  # 0.8 -> 0.5
        (21.0, 21.5),  # exactly 0.5 -> 0.5
        (20.9, 21.0),  # 0.4: below one step, none
        (20.0, 21.0),  # room warmer than the radiators: none
        (27.0, 24.0),  # 6.5 -> capped at max correction 3
    ):
        for trv in (TRV_LR, TRV_DIN):
            st = hass.states.get(trv)
            hass.states.async_set(trv, st.state, {**st.attributes, "current_temperature": reading})
        await _settle(hass, freezer)
        assert _st(hass, TRV_LR) == ("heat", setpoint), reading
        assert _st(hass, TRV_DIN) == ("heat", setpoint), reading


async def test_radiators_of_a_room_share_one_correction(hass, house, freezer) -> None:
    # LR 2.5 °C above the room, dining 1.0 °C below (counts as 0): average 1.25 -> 1.0
    _climate(hass, TRV_LR, "off", 15, current_temperature=23.0)
    _climate(hass, TRV_DIN, "off", 15, current_temperature=19.5)
    await _setup(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 22.0)
    assert _st(hass, TRV_DIN) == ("heat", 22.0)


async def test_radiators_syncing_their_setpoint_do_not_ratchet(hass, house, freezer) -> None:
    """Tado X copies a setpoint between the heads of one room (the 29 °C bug)."""
    house.linked.update({TRV_LR: TRV_DIN, TRV_DIN: TRV_LR})
    _climate(hass, TRV_LR, "off", 15, current_temperature=22.5)
    _climate(hass, TRV_DIN, "off", 15, current_temperature=23.0, max_temp=22.5)
    await _setup(hass, freezer)
    # one correction (average 1.75 -> 1.5), capped at the lower head's max: both 22.5
    assert _st(hass, TRV_LR) == ("heat", 22.5)
    assert _st(hass, TRV_DIN) == ("heat", 22.5)
    sent = len(house.calls)
    for _ in range(5):
        await _settle(hass, freezer, seconds=61)
    assert hass.states.get("sensor.living_room_target_temperature").state == "21.0"
    assert _status(hass) == "comfort"
    assert _attr(hass, "manual_target") is None
    assert house.calls[sent:] == []  # no ping-pong between the heads


async def test_setpoint_copied_from_another_head_is_not_manual(hass, house, freezer) -> None:
    # correction 1.5 for both; dining only takes whole degrees, so 22.5 becomes 22
    _climate(hass, TRV_LR, "off", 15, current_temperature=21.5)
    _climate(hass, TRV_DIN, "off", 15, current_temperature=22.5, target_temp_step=1.0)
    await _setup(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 22.5)
    assert _st(hass, TRV_DIN) == ("heat", 22.0)
    # dining reports the living room head's setpoint: a copy, not a person
    await _set(hass, freezer, TRV_DIN, temperature=22.5)
    assert _attr(hass, "manual_target") is None
    assert _status(hass) == "comfort"


async def test_manual_change_on_corrected_radiator(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "21.5", {"device_class": "temperature"})
    _climate(hass, TRV_LR, "off", 15, current_temperature=23.5)
    _climate(hass, TRV_DIN, "off", 15, current_temperature=23.5)
    await _setup(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 23.0)
    # someone turns the radiator up by 1 °C: the room target goes up by 1 °C
    await _set(hass, freezer, TRV_LR, temperature=24)
    assert hass.states.get("sensor.living_room_target_temperature").state == "22.0"
    assert _st(hass, TRV_DIN) == ("heat", 24.0)
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    assert _status(hass) == "manual"


async def test_manual_change_on_boosted_radiator(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "18.0", {"device_class": "temperature"})
    await _setup(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 24.0)  # 21 + boost offset 3
    # turned up by 2 °C during boost: the room target goes up by 2 °C, boost goes on
    await _set(hass, freezer, TRV_LR, temperature=26)
    assert hass.states.get("sensor.living_room_target_temperature").state == "23.0"
    assert _st(hass, TRV_DIN) == ("heat", 26.0)
    assert _status(hass) == "boost"


async def test_no_correction_without_room_sensor(hass, house, freezer) -> None:
    _climate(hass, TRV_LR, "off", 15, current_temperature=23)
    _climate(hass, TRV_DIN, "off", 15, current_temperature=19)
    await _setup(hass, freezer, temperature_sensor=None)
    assert _attr(hass, "room_temperature") == 21.0
    for e in (TRV_LR, TRV_DIN):
        assert _st(hass, e) == ("heat", 21.0)


# ---- boost -------------------------------------------------------------------


async def test_boost_raises_radiators(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "18.0", {"device_class": "temperature"})
    await _setup(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    assert _st(hass, THERMO) == ("heat", 24.0)
    assert _status(hass) == "boost"
    # still boosting at 20.0 (hysteresis), ends within the exit margin (0.3)
    await _set(hass, freezer, TEMP, "20.0")
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    await _set(hass, freezer, TEMP, "20.8")
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_boost_exit_margin_is_configurable(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "18.0", {"device_class": "temperature"})
    await _setup(hass, freezer)
    await _service(
        hass, freezer, "number", "set_value", "number.living_room_boost_exit_margin", value=1.0
    )
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    await _set(hass, freezer, TEMP, "20.0")
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_boost_with_ac(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "16.2", {"device_class": "temperature"})
    await _setup(hass, freezer, has_bathroom=False)
    sel = "select.living_room_boost_method"
    assert hass.states.get(sel).state == "heaters"
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    await _service(hass, freezer, "select", "select_option", sel, option="coolers")
    for e in (TRV_LR, TRV_DIN):
        assert _st(hass, e) == ("heat", 21.0)
    assert _st(hass, AC) == ("heat", 21.0)
    assert hass.states.get(AC).attributes["fan_mode"] == "auto"
    assert _status(hass) == "boost"

    # kid sets the AC to 30 during boost -> reverted, not adopted
    await _set(hass, freezer, AC, temperature=30)
    assert _st(hass, AC) == ("heat", 21.0)
    assert _st(hass, TRV_LR) == ("heat", 21.0)

    # room reaches target -> AC off, radiators unchanged
    await _set(hass, freezer, TEMP, "20.8")
    assert _st(hass, AC)[0] == "off"
    assert _st(hass, TRV_LR) == ("heat", 21.0)
    assert _status(hass) == "comfort"


async def test_boost_both(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "16.2", {"device_class": "temperature"})
    await _setup(hass, freezer, has_bathroom=False)
    await _service(
        hass, freezer, "select", "select_option", "select.living_room_boost_method", option="both"
    )
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    assert _st(hass, AC) == ("heat", 21.0)


async def test_boost_both_when_ac_integration_crashes(hass, house, freezer, caplog) -> None:
    """LG ThinQ raises TypeError on set_temperature: radiators still boost, boost still ends."""
    hass.states.async_set(TEMP, "16.2", {"device_class": "temperature"})
    house.broken.add(AC)
    await _setup(hass, freezer, has_bathroom=False)
    await _service(
        hass, freezer, "select", "select_option", "select.living_room_boost_method", option="both"
    )
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    assert _st(hass, AC) == ("heat", 24)  # mode set, its own setpoint kept: set_temperature failed
    assert _st(hass, THERMO) == ("heat", 24.0)
    assert _status(hass) == "boost"
    await _settle(hass, freezer, seconds=61)  # retried, but the traceback is logged once
    assert caplog.text.count("set_temperature raised an error in climate.living_room_air") == 1

    await _set(hass, freezer, TEMP, "20.8")
    assert _st(hass, TRV_LR) == ("heat", 21.0)
    assert _st(hass, AC)[0] == "off"
    assert _st(hass, THERMO) == ("heat", 21.0)
    assert _status(hass) == "comfort"


async def test_no_boost_select_without_ac(hass, house, freezer) -> None:
    await _setup(hass, freezer, coolers=[], has_bathroom=False)
    assert hass.states.get("select.living_room_boost_method") is None


async def test_manual_raise_far_above_room_triggers_boost(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    await _set(hass, freezer, TRV_LR, temperature=23)
    assert hass.states.get("sensor.living_room_target_temperature").state == "23.0"
    assert _st(hass, TRV_DIN) == ("heat", 26.0)


# ---- frost protection season and pre-heat ------------------------------------


async def test_frost_season_and_prepare_house(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Frost protection")
    hass.states.async_set(AT_HOME, "off")
    await _setup(hass, freezer)
    for e in (TRV_LR, TRV_DIN, BATH_TRV):
        assert _st(hass, e) == ("heat", 12.0)
    assert _st(hass, THERMO) == ("heat", 12.0)

    await _set(hass, freezer, PREPARE, "on")
    for e in (TRV_LR, TRV_DIN, BATH_TRV):
        assert _st(hass, e) == ("heat", 18.0)
    assert _status(hass) == "preheat"
    assert _st(hass, THERMO) == ("heat", 18.0)

    await _service(
        hass, freezer, "number", "set_value", "number.house_pre_heat_temperature", value=19
    )
    assert _st(hass, TRV_LR) == ("heat", 19.0)

    # rooms not in use are not pre-heated
    await _service(hass, freezer, "switch", "turn_off", "switch.living_room_in_use")
    assert _st(hass, TRV_LR) == ("heat", 12.0)
    assert _status(hass) == "frost_protection"


async def test_frost_temperature_is_set_on_the_house(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Frost protection")
    await _setup(hass, freezer)
    await _service(
        hass,
        freezer,
        "number",
        "set_value",
        "number.house_frost_protection_temperature",
        value=10,
    )
    assert _st(hass, TRV_LR) == ("heat", 10.0)


async def test_comfort_season_everything_off(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    await _set(hass, freezer, SEASON, "Comfort")
    for e in (TRV_LR, TRV_DIN, AC, BATH_TRV, FLOOR, THERMO):
        assert _st(hass, e)[0] == "off"
    assert _status(hass) == "off"


async def test_waiting_when_season_unavailable(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "unavailable")
    await _setup(hass, freezer)
    assert house.calls == []  # nothing commanded
    assert _status(hass) == "waiting"
    assert hass.states.get("sensor.house_season").state == "waiting"


async def test_waiting_without_house(hass, house, freezer) -> None:
    await _setup(hass, freezer, with_house=False)
    assert house.calls == []
    assert _status(hass) == "waiting"
    await _add(hass, "House", "house", HOUSE_OPTIONS)
    await _settle(hass, freezer)
    assert _st(hass, TRV_LR) == ("heat", 21.0)


# ---- cooling -----------------------------------------------------------------


async def test_cooling_follows_schedule(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Cooling")
    await _setup(hass, freezer)
    assert _st(hass, AC) == ("cool", 25.0)
    for e in (TRV_LR, TRV_DIN, BATH_TRV, FLOOR, THERMO):
        assert _st(hass, e)[0] == "off"
    await _set(hass, freezer, SCHEDULE, "off")
    assert _st(hass, AC)[0] == "off"
    assert _status(hass) == "off"


async def test_ac_command_order(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Cooling")
    await _setup(hass, freezer)
    ac_calls = [(name, data) for name, data in house.calls if data["entity_id"] == AC]
    assert ac_calls == [
        ("set_hvac_mode", {"entity_id": AC, "hvac_mode": "cool"}),
        ("set_temperature", {"entity_id": AC, "temperature": 25.0}),
        ("set_fan_mode", {"entity_id": AC, "fan_mode": "auto"}),
    ]


async def _later(hass, freezer, seconds):
    """Let `seconds` pass in one step, then let the update cycle run."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await _settle(hass, freezer)


def _ac_calls(house):
    return [name for name, data in house.calls if data["entity_id"] == AC]


async def test_ac_that_does_not_switch_is_retried_and_reported(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Cooling")
    house.stuck.add(AC)
    await _setup(hass, freezer)
    response = hass.states.get("number.living_room_a_c_response_time")
    assert response.state == "60.0"
    assert _ac_calls(house) == ["set_hvac_mode"]  # no temperature for the wrong mode

    await _later(hass, freezer, 61)  # no report within the response time: second attempt
    assert _ac_calls(house) == ["set_hvac_mode"] * 2
    assert house.notes == []
    await _later(hass, freezer, 61)  # second attempt failed too: reported, and tried again
    assert _ac_calls(house) == ["set_hvac_mode"] * 3
    assert len(house.notes) == 1
    assert AC in house.notes[0]["message"]
    await _later(hass, freezer, 61)
    await _later(hass, freezer, 61)
    assert len(house.notes) == 1  # reported once per failure

    house.stuck.clear()
    await _later(hass, freezer, 61)
    assert _st(hass, AC) == ("cool", 25.0)
    assert hass.states.get(AC).attributes["fan_mode"] == "auto"


async def test_failure_reported_to_notify_entity(hass, house, freezer) -> None:
    """notify.igoran is an entity (a notify group helper), not a notify action."""
    sent = []

    async def send_message(call):
        sent.append(dict(call.data))

    hass.services.async_register("notify", "send_message", send_message)
    hass.states.async_set("notify.igoran", "unknown")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "House", **HOUSE_OPTIONS, "notify_services": ["notify.igoran"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["notify_services"] == ["igoran"]

    hass.states.async_set(SEASON, "Cooling")
    house.stuck.add(AC)
    await _setup(hass, freezer, with_house=False)
    await _later(hass, freezer, 61)
    await _later(hass, freezer, 61)
    assert len(sent) == 1
    assert sent[0]["entity_id"] == "notify.igoran"
    assert AC in sent[0]["message"]


async def test_slow_ac_does_not_hold_up_the_room(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "16.2", {"device_class": "temperature"})
    house.stuck.add(AC)  # it switches, but reports that only later
    await _setup(hass, freezer, has_bathroom=False)
    await _service(
        hass, freezer, "select", "select_option", "select.living_room_boost_method", option="both"
    )
    # radiators boosted at once; the A/C got its mode command and is given time
    assert _st(hass, TRV_LR) == ("heat", 24.0)
    assert _ac_calls(house) == ["set_hvac_mode"]

    # 40 s later the A/C reports heat, still with its old setpoint: not a manual change
    await _later(hass, freezer, 40)
    await _set(hass, freezer, AC, "heat")
    assert _ac_calls(house) == ["set_hvac_mode", "set_temperature", "set_fan_mode"]
    assert _st(hass, AC) == ("heat", 21.0)
    assert _attr(hass, "manual_target") is None
    assert house.notes == []


async def test_cooling_needs_room_in_use_and_someone_home(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Cooling")
    await _setup(hass, freezer)
    await _service(hass, freezer, "switch", "turn_off", "switch.living_room_in_use")
    assert _st(hass, AC)[0] == "off"
    await _service(hass, freezer, "switch", "turn_on", "switch.living_room_in_use")
    await _set(hass, freezer, AT_HOME, "off")
    assert _st(hass, AC)[0] == "off"
    # pre-cool before arrival
    await _set(hass, freezer, PREPARE, "on")
    assert _st(hass, AC) == ("cool", 25.0)


async def test_kid_sets_ac_to_18(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Cooling")
    await _setup(hass, freezer)
    # 18 °C on the remote -> clamped to 22
    await _set(hass, freezer, AC, temperature=18)
    assert _st(hass, AC) == ("cool", 22.0)
    assert _status(hass) == "manual"
    # a reasonable change is adopted as-is
    await _set(hass, freezer, AC, temperature=23.5)
    assert _st(hass, AC) == ("cool", 23.5)
    # schedule ends -> A/C off; next period starts without the manual target
    await _set(hass, freezer, SCHEDULE, "off")
    assert _st(hass, AC)[0] == "off"
    await _set(hass, freezer, SCHEDULE, "on")
    assert _st(hass, AC) == ("cool", 25.0)


async def test_ac_turned_on_in_heating_is_reverted(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    await _set(hass, freezer, AC, "cool", temperature=18)
    assert _st(hass, AC)[0] == "off"


# ---- drying ------------------------------------------------------------------


async def test_drying_in_comfort_season(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Comfort")
    hass.states.async_set(HUMIDITY, "70", {"device_class": "humidity"})
    await _setup(hass, freezer)
    assert _st(hass, AC)[0] == "dry"
    assert hass.states.get(AC).attributes["fan_mode"] == "auto"
    assert _status(hass) == "drying"
    # keeps drying until humidity is below threshold - hysteresis (65 - 5)
    await _set(hass, freezer, HUMIDITY, "62")
    assert _st(hass, AC)[0] == "dry"
    await _set(hass, freezer, HUMIDITY, "60")
    assert _st(hass, AC)[0] == "off"
    assert _status(hass) == "off"


async def test_drying_allowed_in_frost_protection_but_not_while_heating(
    hass, house, freezer
) -> None:
    hass.states.async_set(HUMIDITY, "70", {"device_class": "humidity"})
    await _setup(hass, freezer)
    assert _st(hass, AC)[0] == "off"
    await _service(hass, freezer, "switch", "turn_off", "switch.living_room_in_use")
    assert _st(hass, AC)[0] == "dry"
    assert _st(hass, TRV_LR) == ("heat", 12.0)
    assert _status(hass) == "frost_protection"
    assert _attr(hass, "drying") is True


async def test_no_drying_while_cooling(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Cooling")
    hass.states.async_set(HUMIDITY, "80", {"device_class": "humidity"})
    await _setup(hass, freezer)
    assert _st(hass, AC) == ("cool", 25.0)
    await _set(hass, freezer, SCHEDULE, "off")
    assert _st(hass, AC)[0] == "dry"


async def test_drying_threshold_on_house_and_humidity_from_devices(hass, house, freezer) -> None:
    hass.states.async_set(SEASON, "Comfort")
    _climate(hass, TRV_LR, "off", 15, current_humidity=58)
    _climate(hass, TRV_DIN, "off", 15, current_humidity=62)
    await _setup(hass, freezer, humidity_sensor=None)
    assert _attr(hass, "humidity") == 60.0
    assert _st(hass, AC)[0] == "off"
    await _service(
        hass, freezer, "number", "set_value", "number.house_drying_humidity_threshold", value=55
    )
    assert _st(hass, AC)[0] == "dry"


# ---- manual changes, override, restart ---------------------------------------


async def test_block_policy(hass, house, freezer) -> None:
    await _setup(hass, freezer, manual_policy="block")
    await _set(hass, freezer, TRV_LR, "heat", temperature=26)
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_follow_heating_applies_to_whole_room(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "22.5", {"device_class": "temperature"})
    await _setup(hass, freezer)
    await _set(hass, freezer, TRV_LR, temperature=23)
    for e in (TRV_LR, TRV_DIN):
        assert _st(hass, e) == ("heat", 23.0)
    # bathroom follows the room target outside its schedule
    assert _st(hass, BATH_TRV) == ("heat", 23.0)


async def test_manual_target_dropped_when_schedule_ends(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    await _set(hass, freezer, TRV_LR, temperature=22)
    assert _st(hass, TRV_DIN) == ("heat", 22.0)
    # straight to eco on the same update, not one cycle later
    await _set(hass, freezer, SCHEDULE, "off")
    for e in (TRV_LR, TRV_DIN):
        assert _st(hass, e) == ("heat", 19.0)
    assert _status(hass) == "eco"


async def test_resume_automatic_button(hass, house, freezer) -> None:
    hass.states.async_set(TEMP, "22.5", {"device_class": "temperature"})
    await _setup(hass, freezer)
    button = "button.living_room_resume_automatic"
    await _set(hass, freezer, TRV_LR, temperature=23)
    await _set(hass, freezer, TRV_DIN, "off")
    assert _status(hass) == "manual"
    assert _st(hass, TRV_DIN)[0] == "off"

    await _service(hass, freezer, "button", "press", button)
    for e in (TRV_LR, TRV_DIN):
        assert _st(hass, e) == ("heat", 21.0)
    assert _status(hass) == "comfort"

    # ends an override as well
    await _service(hass, freezer, "switch", "turn_on", "switch.living_room_override")
    assert _st(hass, TRV_LR) == ("heat", 22.0)
    await _service(hass, freezer, "button", "press", button)
    assert hass.states.get("switch.living_room_override").state == "off"
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_radiator_turned_off_manually_is_respected(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    await _set(hass, freezer, TRV_DIN, "off")
    assert _st(hass, TRV_DIN)[0] == "off"
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_override_expires(hass, house, freezer) -> None:
    await _setup(hass, freezer)
    await _service(
        hass, freezer, "number", "set_value", "number.living_room_override_duration", value=30
    )
    await _service(hass, freezer, "switch", "turn_on", "switch.living_room_override")
    assert _st(hass, TRV_LR) == ("heat", 22.0)
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await _settle(hass, freezer)
    assert hass.states.get("switch.living_room_override").state == "off"
    assert _st(hass, TRV_LR) == ("heat", 21.0)


async def test_own_command_echo_is_not_manual(hass, house, freezer) -> None:
    """A device reporting our command back must never become a manual target."""
    await _setup(hass, freezer)
    assert _attr(hass, "manual_target") is None


async def test_restore_after_restart(hass, house, freezer) -> None:
    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (State("switch.living_room_override", "on", {"until": future}), {}),
            (State("switch.living_room_in_use", "on"), {}),
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
    hass.states.async_set(TEMP, "23.0", {"device_class": "temperature"})
    await _setup(hass, freezer)
    assert hass.states.get("switch.living_room_override").state == "on"
    assert _st(hass, TRV_LR) == ("heat", 23.5)
