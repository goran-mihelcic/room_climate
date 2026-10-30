"""Constants for the Room Climate integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "room_climate"

# ---- Config entry types (stored in entry.data) --------------------------
CONF_ENTRY_TYPE: Final = "type"
TYPE_HOUSE: Final = "house"
TYPE_ROOM: Final = "room"

# ---- House options ------------------------------------------------------
CONF_AT_HOME: Final = "at_home_entity"
CONF_OUTDOOR: Final = "outdoor_entity"  # outdoor temperature: the season is computed from it
CONF_REFERENCE: Final = "reference_entity"  # indoor temperature that can force heat/cool
CONF_HEAT_PUMP: Final = "heat_pump_entity"  # turned on when heating starts
CONF_PREPARE: Final = "prepare_entity"
CONF_NOTIFY: Final = "notify_services"
# Before 0.4: the season came from this entity. Still read while no outdoor sensor is set.
CONF_SEASON: Final = "season_entity"

# ---- Room options -------------------------------------------------------
CONF_HEATERS: Final = "heaters"
CONF_COOLERS: Final = "coolers"
CONF_FLOOR_THERMOSTAT: Final = "floor_thermostat"
CONF_SCHEDULE: Final = "schedule"
CONF_TEMP_SENSOR: Final = "temperature_sensor"
CONF_HUMIDITY_SENSOR: Final = "humidity_sensor"
CONF_MANUAL_POLICY: Final = "manual_policy"
CONF_HEAT_MIN: Final = "manual_heat_min"
CONF_HEAT_MAX: Final = "manual_heat_max"
CONF_COOL_MIN: Final = "manual_cool_min"
CONF_COOL_MAX: Final = "manual_cool_max"
CONF_HAS_BATHROOM: Final = "has_bathroom"
CONF_BATH_HEATERS: Final = "bath_heaters"
CONF_BATH_FLOOR: Final = "bath_floor"
CONF_BATH_SCHEDULE: Final = "bath_schedule"

BOOST_HEATERS: Final = "heaters"  # raise radiator setpoints by the boost offset
BOOST_COOLERS: Final = "coolers"  # run the A/C in heat mode
BOOST_BOTH: Final = "both"

POLICY_FOLLOW: Final = "follow"
POLICY_BLOCK: Final = "block"

DEFAULT_HEAT_MIN: Final = 16.0
DEFAULT_HEAT_MAX: Final = 24.0
DEFAULT_COOL_MIN: Final = 22.0
DEFAULT_COOL_MAX: Final = 28.0

# Entity domains accepted as a schedule: state "on" means inside a period.
SCHEDULE_DOMAINS: Final = ["schedule", "input_boolean", "binary_sensor"]

# ---- HVAC season --------------------------------------------------------
SEASON_HEAT: Final = "heating"
SEASON_COOL: Final = "cooling"
SEASON_FROST: Final = "frost_protection"
SEASON_COMFORT: Final = "comfort"

# States of the season entity (compared lower-case, stripped)
SEASON_MAP: Final = {
    "heating": SEASON_HEAT,
    "cooling": SEASON_COOL,
    "frost protection": SEASON_FROST,
    "frost_protection": SEASON_FROST,
    "comfort": SEASON_COMFORT,
}

ON_STATES: Final = {"on", "home", "true"}
AWAY_STATES: Final = {"off", "not_home", "away", "false"}

# ---- Room regime (what the room is doing right now) ---------------------
REGIME_HEAT: Final = "heat"
REGIME_COOL: Final = "cool"
REGIME_FROST: Final = "frost"
REGIME_PREHEAT: Final = "preheat"
REGIME_OFF: Final = "off"

# ---- Status sensor values -----------------------------------------------
HOUSE_STATUSES: Final = [
    "waiting",
    SEASON_HEAT,
    SEASON_COOL,
    SEASON_FROST,
    SEASON_COMFORT,
]
ROOM_STATUSES: Final = [
    "waiting",
    "off",
    "frost_protection",
    "preheat",
    "comfort",
    "eco",
    "override",
    "manual",
    "boost",
    "drying",
]
BATH_STATUSES: Final = [
    "waiting",
    "off",
    "frost_protection",
    "bath_period",
    "follow_room",
    "manual",
]

# ---- Behaviour tuning ---------------------------------------------------
COMMAND_GRACE_SECONDS: Final = 45  # ignore device reports this long after we command it
UPDATE_INTERVAL_SECONDS: Final = 60
CORRECTION_STEP: Final = 0.5  # radiator corrections are whole steps of this, rounded down

# ---- Entity defaults ----------------------------------------------------
# key: (name, min, max, step, default, unit)
HOUSE_NUMBERS: Final = {
    "frost": ("Frost protection temperature", 5.0, 18.0, 0.5, 12.0, "°C"),
    "preheat": ("Pre-heat temperature", 10.0, 25.0, 0.5, 18.0, "°C"),
    "dry_humidity": ("Drying humidity threshold", 40.0, 90.0, 1.0, 65.0, "%"),
    "dry_hysteresis": ("Drying hysteresis", 1.0, 20.0, 1.0, 5.0, "%"),
    # Season, computed from the outdoor and reference (indoor) temperatures
    "heat_season": ("Heating season temperature", 5.0, 30.0, 0.5, 18.0, "°C"),
    "cool_season": ("Cooling season temperature", 15.0, 40.0, 0.5, 29.0, "°C"),
    "heat_indoor": ("Indoor heating threshold", 10.0, 28.0, 0.5, 21.0, "°C"),
    "cool_indoor": ("Indoor cooling threshold", 20.0, 35.0, 0.5, 29.0, "°C"),
    "frost_season": ("Frost season temperature", -20.0, 15.0, 0.5, 5.0, "°C"),
    "season_hysteresis": ("Season hysteresis", 0.0, 5.0, 0.5, 1.0, "°C"),
    "season_delay": ("Season change delay", 0.0, 240.0, 5.0, 30.0, "min"),
}
ROOM_NUMBERS: Final = {
    "heat_comfort": ("Comfort temperature", 10.0, 28.0, 0.5, 21.0, "°C"),
    "heat_eco": ("Eco temperature", 10.0, 28.0, 0.5, 19.0, "°C"),
    "cool": ("Cooling temperature", 18.0, 32.0, 0.5, 25.0, "°C"),
    "boost_delta": ("Boost threshold", 0.5, 6.0, 0.5, 1.5, "°C"),
    "boost_offset": ("Boost offset", 0.5, 8.0, 0.5, 3.0, "°C"),
    "boost_margin": ("Boost exit margin", 0.0, 2.0, 0.1, 0.3, "°C"),
    "correction_max": ("Max radiator correction", 0.0, 6.0, 0.5, 3.0, "°C"),
    "override_temp": ("Override temperature", 10.0, 32.0, 0.5, 22.0, "°C"),
    "override_minutes": ("Override duration", 0.0, 720.0, 15.0, 120.0, "min"),
}
DEFAULT_HEAT_PUMP_RESPONSE_SECONDS: Final = 300.0
# Only for a House with a heat pump.
HEAT_PUMP_NUMBERS: Final = {
    "heat_pump_response": (
        "Heat pump response time",
        30.0,
        1800.0,
        10.0,
        DEFAULT_HEAT_PUMP_RESPONSE_SECONDS,
        "s",
    ),
}
DEFAULT_AC_RESPONSE_SECONDS: Final = 60.0
# Only for rooms with an A/C.
AC_NUMBERS: Final = {
    "ac_response": ("A/C response time", 3.0, 600.0, 1.0, DEFAULT_AC_RESPONSE_SECONDS, "s"),
}
BATH_NUMBERS: Final = {
    "bath_comfort": ("Comfort temperature", 15.0, 30.0, 0.5, 24.0, "°C"),
    "bath_floor_temp": ("Floor heating temperature", 15.0, 35.0, 0.5, 27.0, "°C"),
}
# Shown under "Configuration" on the device page rather than "Controls".
CONFIG_NUMBERS: Final = {
    "dry_hysteresis",
    "heat_season",
    "cool_season",
    "heat_indoor",
    "cool_indoor",
    "frost_season",
    "season_hysteresis",
    "season_delay",
    "boost_delta",
    "boost_offset",
    "boost_margin",
    "correction_max",
    "override_minutes",
    "ac_response",
    "heat_pump_response",
}
