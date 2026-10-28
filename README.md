# Room Climate

A Home Assistant custom integration that turns each room into one device. It drives separate heating and cooling devices, follows the house status, handles a room's bathroom, and deals with manual changes made on remotes and thermostats.

## Install

**HACS (recommended):** push this folder to a GitHub repo, then in HACS → ⋮ → Custom repositories → add the repo URL as *Integration*. Install **Room Climate** and restart HA.

**Manual:** copy `custom_components/room_climate/` into your HA `config/custom_components/` folder and restart HA.

Then go to **Settings → Devices & services → Add integration → Room Climate**, once per room.

## Setup (per room)

| Field | Example |
|---|---|
| At home entity | `input_boolean.at_home` |
| House climate status entity | `sensor.home_climate_status` |
| Heating devices | `climate.smart_thermostat_x`, `climate.smart_radiator_thermostat_lr`, `climate.smart_radiator_thermostat_dining` |
| Cooling devices | `climate.living_room_air` |
| Room temperature sensor | optional; without it the average of the heaters' own readings is used |
| Manual changes | *Follow (within limits)* or *Block* |
| Manual limits | heating 16–24 °C, cooling 22–28 °C |
| Boost method | *Raise heating setpoints*, *Run the AC in heat mode*, or *Both* |
| Room has a bathroom | ticking it opens a second step for bathroom heaters and floor heating |

House entities are pre-filled from the first room you created. Everything can be changed later with **Configure** on the integration.

## What the room does

| House status | At home, room enabled | Away, or room disabled |
|---|---|---|
| Heating | heats to comfort/setback target; AC off | frost protection |
| Cooling | cools to comfort/setback target; heaters off | everything off |
| Frost protection | frost protection | frost protection |
| Comfort | everything off | everything off |
| unavailable/unknown | holds devices as they are (status *waiting*) | same |

- **Comfort period:** between *Comfort start* and *Comfort end* (time entities on the device). Outside it, the setback applies (heating: setpoint − setback, cooling: setpoint + setback).
- **Override:** the switch applies the override temperature for the override duration (0 = until switched off). Survives restarts.
- **Boost:** when heating and the room is more than the boost threshold below target, boost starts; it ends when the room is within 0.3 °C of target. The **boost method** (in Configure) decides how:
  - *Raise heating setpoints:* heaters get target + boost offset, capped at each device's max.
  - *Run the AC in heat mode:* heaters stay at the target, and the cooling device heats at the target until boost ends, then switches off. Setpoint changes on the AC during boost are reverted.
  - *Both:* raised heater setpoints plus the AC.

## Bathroom

| | Morning / evening period | Rest of the day |
|---|---|---|
| Bathroom radiator(s) | bathroom comfort temperature | follows the room target |
| Floor heating | on, at floor heating temperature | off |

Heating season only. In frost protection the radiators hold the frost temperature and the floor heating is off.

## Manual changes on devices

- **Follow:** a setpoint changed on a remote or thermostat becomes the room's target, clamped to the configured limits (the AC set to 18 °C becomes 22 °C), and is applied to all devices of the room. It's dropped at the next period change: comfort ↔ setback, a bathroom period start/end, house status, presence, enabled or override changes, or any setting change on the device. A device turned off by hand stays off until the next period change.
- **Block:** the planned temperature is restored straight away.
- **Wrong mode** (AC switched to cool during heating season, or turned on when it should be off) is always reverted.

Commands sent by the integration carry their own context and devices get a 45 s settling window, so a device reporting a command back is never mistaken for a manual change.

## Entities per room

- **Sensors:** *Target temperature* (with all state as attributes), *Status* (`comfort`, `setback`, `boost`, `manual`, `override`, `frost_protection`, `off`, `waiting`)
- **Controls:** *Enabled*, *Override*, *Heating setpoint*, *Cooling setpoint*, *Override temperature*
- **Configuration:** *Comfort start/end*, *Setback*, *Frost protection temperature*, *Boost threshold*, *Boost offset*, *Override duration*
- **Bathroom device:** *Target temperature*, *Status*, *Comfort temperature*, *Floor heating temperature*, morning and evening period start/end

## Debug logging

```yaml
logger:
  logs:
    custom_components.room_climate: debug
```

## Tests

```bash
pip install pytest-homeassistant-custom-component
pytest
```
