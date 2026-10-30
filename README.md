# Room Climate

A Home Assistant custom integration for a house with a heat pump, radiators with smart thermostatic heads (Tado X), floor (pump) thermostats, air conditioners and bathrooms with electric floor heating.

It creates one **House** device and one device per **room** (plus a linked bathroom device). It follows the house HVAC season, heats and cools each room on its own schedule, boosts cold rooms, dries humid rooms with the A/C, keeps the floor thermostats running while any radiator on them needs heat, and handles changes people make on remotes and thermostats.

The heat pump itself is not driven by this integration; switch it from the HVAC season with your own automation.

## Install

**HACS (recommended):** HACS → ⋮ → Custom repositories → add `https://github.com/goran-mihelcic/room_climate` as *Integration*. Install **Room Climate** and restart HA.

**Manual:** copy `custom_components/room_climate/` into your HA `config/custom_components/` folder and restart HA.

Then go to **Settings → Devices & services → Add integration → Room Climate**. The first time this sets up the House; every time after that it adds a room.

> **Upgrading from 0.2.x:** rooms from 0.2 cannot be migrated and show *Migration error*. Delete them, add the House, then add the rooms again.

## Setup

### House (once)

| Field | Example | |
|---|---|---|
| HVAC season entity | `input_select.hvac_season` | states *Heating*, *Cooling*, *Comfort*, *Frost protection* |
| At home entity | `input_boolean.at_home` | off: nobody is home |
| Prepare house entity | `input_boolean.prepare_house` | optional; pre-heat or pre-cool before arrival |
| Notify services | `mobile_app_my_phone` | optional; told when an A/C does not switch mode |

### Room (once per room)

| Field | Example | |
|---|---|---|
| Radiators | `climate.smart_radiator_thermostat_lr`, `climate.smart_radiator_thermostat_dining` | required |
| Air conditioners | `climate.living_room_air` | optional |
| Floor thermostat (pump) | `climate.smart_thermostat_x` | optional; the thermostat that runs this floor's radiator pump |
| Schedule | `schedule.living_room` | optional [schedule helper](https://www.home-assistant.io/integrations/schedule/); without one the room is always on schedule |
| Room temperature sensor | `sensor.living_room_temperature` | optional; without it the radiators' own readings are used |
| Room humidity sensor | `sensor.living_room_humidity` | optional; without it the radiators' (then the A/C's) own readings are used |
| Manual changes | *Follow (within limits)* or *Block* | |
| Manual limits | heating 16–24 °C, cooling 22–28 °C | |
| Room has a bathroom | | ticking it opens a second step |

**Bathroom step:** bathroom radiators, floor heating, and a bathroom schedule helper.

A new room's form starts with the manual-change policy and manual limits of the room added last; devices, schedules and the bathroom are always picked fresh. Several rooms can share one schedule helper.

Everything can be changed later with **Configure** on the entry. Create the schedule helpers under **Settings → Devices & services → Helpers → Schedule**; they can differ per weekday.

## What a room does

| HVAC season | Room in use | Room not in use |
|---|---|---|
| **Heating**, someone home | heats: comfort temperature on schedule, eco temperature off schedule; A/C off | frost protection |
| **Heating**, nobody home | frost protection (the season should already say *Frost protection*; this is a safety net) | frost protection |
| **Frost protection** | frost protection; **pre-heat temperature** while *Prepare house* is on | frost protection |
| **Cooling** | A/C cools to the cooling temperature on schedule, off outside it. Needs someone home or *Prepare house* on. Radiators off. | off |
| **Comfort** | off | off |
| unavailable / unknown, or no House set up | devices held as they are (status *waiting*) | same |

Frost protection and pre-heat temperatures are set on the House device and apply to every room.

### Radiator correction

Radiator heads sit next to the radiator and read higher than the room. When a room temperature sensor is configured, each radiator's setpoint is raised by how much higher it reads than the sensor:

> room target 21 °C, sensor 20.5 °C, radiator reads 22.5 °C → radiator set to 23 °C

The radiator then closes when the *room* reaches the target. The correction is the difference rounded **down** to whole 0.5 °C steps, so it is never more than the difference: 0.4 °C → none, 0.8 °C → 0.5 °C, 2.3 °C → 2 °C. It never exceeds *Max radiator correction* (default 3 °C).

**All radiators of a room get one correction and exactly the same setpoint.** Tado X keeps a single setpoint per room and copies it between the heads, so different setpoints per head would fight each other. The correction is the average of how much each radiator reads above the room sensor; a radiator reading the same as or lower than the room counts as 0. The shared setpoint is capped at the lowest maximum among the room's radiators.

An A/C heating during a boost gets its own correction the same way. Cooling is never corrected.

### Floor thermostat

Every room on the same floor thermostat reports its highest radiator setpoint (bathroom radiators included, corrections and boost included). The thermostat is set to the highest of them, so the pump runs while any radiator on that floor wants heat:

> Bedroom 1 at 19 °C, Bedroom 2 at 12 °C, Bedroom 3 at 20 °C → thermostat 20 °C

With no radiator heating (cooling or comfort season) the thermostat is switched off. Changes made on the thermostat itself are reverted within a minute.

### Boost

In the heating season, when the room is more than *Boost threshold* below its target, boost starts. It ends when the room is within *Boost exit margin* of the target (default 0.3 °C). The **Boost method** selector (rooms with an A/C) decides how:

- *Raise radiator setpoints:* radiators get target + boost offset, capped at each device's max.
- *A/C in heat mode:* radiators stay at the target and the A/C heats to the target until boost ends, then switches off. Setpoint changes on the A/C during boost are reverted.
- *Both.*

### Drying

When a room's humidity is above the House's *Drying humidity threshold* and the room is neither heated nor cooled, its A/C runs in *dry* mode until humidity falls to threshold − *Drying hysteresis*. Frost protection does not stop drying; heating, pre-heat and cooling do. This applies whether or not the room is in use.

### A/C commands

Every A/C command follows the same order:

1. Set the HVAC mode and wait for the A/C to report it, up to the room's **A/C response time** (default 60 s, 3–600 s). If it hasn't reported by then, send the mode once more. If that fails too, the House's notify services get a message (once, until it works again) and the mode keeps being retried every response time.
2. Once the A/C reports the new mode: set the temperature.
3. Set the fan to *auto*.

Waiting does not hold up the room: radiators, bathroom and floor thermostat are driven as usual meanwhile. A late report from an A/C that is still catching up is never taken for a manual change. Slow units (LG ThinQ can take minutes) just need a longer response time.

### Bathroom

| | Bathroom schedule on | Bathroom schedule off |
|---|---|---|
| Bathroom radiator(s) | bathroom comfort temperature | follow the room target |
| Floor heating | on, at floor heating temperature | off |

The bathroom schedule applies only while the room is heating. In frost protection the bathroom radiators hold the frost temperature, during pre-heat they follow the room, and the floor heating is off.

### Override

The *Override* switch applies the override temperature (heating or cooling) for the override duration (0 = until switched off). It survives restarts.

## Manual changes on devices

- **Follow:** a setpoint changed on a remote or radiator becomes the room's target, clamped to the configured limits (the A/C set to 18 °C becomes 22 °C), and is applied to all devices of the room. A radiator's correction (and, during a boost, the boost offset) is taken off first: turning a radiator showing 23 °C up to 24 °C raises the room target by 1 °C. A radiator that only took over another radiator's setpoint (Tado syncing a room) is not a manual change. The manual target is dropped at the next period change: schedule on ↔ off, a bathroom schedule change, season, presence, in use or override changes, or any setting change on the device. A device turned off by hand stays off until the next period change.
- **Block:** the planned temperature is restored straight away.
- **Wrong mode** (A/C switched to cool during heating, or turned on when it should be off) is always reverted.

Commands sent by the integration carry their own context and devices get a 45 s settling window, so a device reporting a command back is never mistaken for a manual change.

## Entities

**House device**
- *Season* sensor (`heating`, `cooling`, `comfort`, `frost_protection`, `waiting`); attributes show the season entity and its raw state, presence, prepare house and each floor thermostat's setpoint.
- *Debug log file* switch (see [Logging](#logging)).
- *Frost protection temperature* (12 °C), *Pre-heat temperature* (18 °C), *Drying humidity threshold* (65 %), *Drying hysteresis* (5 %).

**Room device**
- *Target temperature* sensor (all state as attributes: season, schedule, room temperature, humidity, boost, radiator corrections, drying, manual target, …) and *Status* sensor (`comfort`, `eco`, `boost`, `manual`, `override`, `preheat`, `frost_protection`, `drying`, `off`, `waiting`).
- Controls: *In use*, *Override*, *Comfort temperature* (21 °C), *Eco temperature* (19 °C), *Cooling temperature* (25 °C), *Override temperature* (22 °C).
- Configuration: *Boost method* and *A/C response time* (60 s) (rooms with an A/C), *Boost threshold* (1.5 °C), *Boost offset* (3 °C), *Boost exit margin* (0.3 °C), *Max radiator correction* (3 °C), *Override duration* (120 min).

**Bathroom device**
- *Target temperature*, *Status*, *Comfort temperature* (24 °C), *Floor heating temperature* (27 °C).

## Logging

Room Climate logs to Home Assistant's own log (`/config/home-assistant.log`; warnings and errors also under **Settings → System → Logs**). By default that is only warnings and errors: a device that rejected a command, an A/C that did not switch, an error inside another integration.

**Debug log file (recommended for debugging):** turn on **Debug log file** on the House device. Everything Room Climate does is then written at debug level to `/config/room_climate.log` (5 MB, 3 older files kept as `room_climate.log.1` …), while Home Assistant's log keeps getting only what it got before. Each room writes one line whenever its decision changes:

```
2026-10-30 07:00:02 DEBUG   [custom_components.room_climate.controller] Bedroom 1: comfort (heat, heating) target=21.0 room=19.8 humidity=58.0 season=heating at_home=True prepare=False in_schedule=True boost=False drying=False corrections={'climate.bedroom_1_trv': 1.5} bath=follow_room/21.0 devices={'climate.bedroom_1_trv': 'heat 22.5', 'climate.bedroom_1_air': 'off'}
```

plus every command sent to a device, house input changes and floor thermostat setpoints. The switch survives restarts; turn it off when done. Read the file with the File editor or Studio Code Server add-on, or over Samba/SSH.

**Into Home Assistant's log instead:** **⋮ → Enable debug logging** on the integration page (a file is downloaded when you turn it off), or permanently:

```yaml
logger:
  logs:
    custom_components.room_climate: debug
```

## Tests

See [TESTING.md](TESTING.md).
