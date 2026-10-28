"""Constants for the Room Climate integration."""

from __future__ import annotations

from datetime import time
from typing import Final

DOMAIN: Final = "room_climate"

# ---- Config entry option keys -------------------------------------------
CONF_AT_HOME: Final = "at_home_entity"
CONF_HOUSE_STATUS: Final = "house_status_entity"
CONF_HEATERS: Final = "heaters"
CONF_COOLERS: Final = "coolers"
CONF_ROOM_SENSOR: Final = "room_sensor"
CONF_MANUAL_POLICY: Final = "manual_policy"
CONF_HEAT_MIN: Final = "manual_heat_min"
CONF_HEAT_MAX: Final = "manual_heat_max"
CONF_COOL_MIN: Final = "manual_cool_min"
CONF_COOL_MAX: Final = "manual_cool_max"
CONF_HAS_BATHROOM: Final = "has_bathroom"
CONF_BATH_HEATERS: Final = "bath_heaters"
CONF_BATH_FLOOR: Final = "bath_floor"

CONF_BOOST_METHOD: Final = "boost_method"
BOOST_HEATERS: Final = "heaters"  # raise heating setpoints by the boost offset
BOOST_COOLERS: Final = "coolers"  # run the cooling device (AC) in heat mode
BOOST_BOTH: Final = "both"

POLICY_FOLLOW: Final = "follow"
POLICY_BLOCK: Final = "block"

DEFAULT_HEAT_MIN: Final = 16.0
DEFAULT_HEAT_MAX: Final = 24.0
DEFAULT_COOL_MIN: Final = 22.0
DEFAULT_COOL_MAX: Final = 28.0

# ---- House status -------------------------------------------------------
HOUSE_HEAT: Final = "heat"
HOUSE_COOL: Final = "cool"
HOUSE_FROST: Final = "frost"
HOUSE_IDLE: Final = "idle"

# States of the house status entity (compared lower-case, stripped)
HOUSE_STATUS_MAP: Final = {
    "heating": HOUSE_HEAT,
    "cooling": HOUSE_COOL,
    "frost protection": HOUSE_FROST,
    "frost_protection": HOUSE_FROST,
    "comfort": HOUSE_IDLE,
    "off": HOUSE_IDLE,
    "idle": HOUSE_IDLE,
}

AT_HOME_STATES: Final = {"on", "home", "true"}
AWAY_STATES: Final = {"off", "not_home", "away", "false"}

# ---- Room regime (what the room is doing right now) ---------------------
REGIME_HEAT: Final = "heat"
REGIME_COOL: Final = "cool"
REGIME_FROST: Final = "frost"
REGIME_OFF: Final = "off"

# ---- Status sensor values -----------------------------------------------
ROOM_STATUSES: Final = [
    "waiting",
    "off",
    "frost_protection",
    "comfort",
    "setback",
    "override",
    "manual",
    "boost",
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
BOOST_EXIT_MARGIN: Final = 0.3  # °C below target at which boost ends
COMMAND_GRACE_SECONDS: Final = 45  # ignore device reports this long after we command it
UPDATE_INTERVAL_SECONDS: Final = 60

# ---- Entity defaults ----------------------------------------------------
# key: (name, min, max, step, default, unit)
ROOM_NUMBERS: Final = {
    "heat": ("Heating setpoint", 10.0, 28.0, 0.5, 21.0, "°C"),
    "cool": ("Cooling setpoint", 18.0, 32.0, 0.5, 25.0, "°C"),
    "setback": ("Setback", 0.0, 8.0, 0.5, 2.0, "°C"),
    "frost": ("Frost protection temperature", 5.0, 18.0, 0.5, 12.0, "°C"),
    "boost_delta": ("Boost threshold", 0.5, 6.0, 0.5, 1.5, "°C"),
    "boost_offset": ("Boost offset", 0.5, 8.0, 0.5, 3.0, "°C"),
    "override_temp": ("Override temperature", 10.0, 32.0, 0.5, 22.0, "°C"),
    "override_minutes": ("Override duration", 0.0, 720.0, 15.0, 120.0, "min"),
}
BATH_NUMBERS: Final = {
    "bath_comfort": ("Comfort temperature", 15.0, 30.0, 0.5, 24.0, "°C"),
    "bath_floor_temp": ("Floor heating temperature", 15.0, 35.0, 0.5, 27.0, "°C"),
}

# key: (name, default)
ROOM_TIMES: Final = {
    "comfort_start": ("Comfort start", time(6, 30)),
    "comfort_end": ("Comfort end", time(23, 0)),
}
BATH_TIMES: Final = {
    "bath_p1_start": ("Morning period start", time(6, 0)),
    "bath_p1_end": ("Morning period end", time(8, 30)),
    "bath_p2_start": ("Evening period start", time(19, 0)),
    "bath_p2_end": ("Evening period end", time(22, 30)),
}
