# Room Climate

A Home Assistant custom integration for a house with a heat pump, radiators with smart thermostatic heads (Tado X), floor (pump) thermostats, air conditioners, floor heating and bathrooms with electric floor heating.

It creates one **House** device and one device per **room** (plus a linked bathroom device). It follows the house HVAC season, heats and cools each room on its own schedule, boosts cold rooms, dries humid rooms with the A/C, keeps the floor thermostats running while any radiator on them needs heat, and handles changes people make on remotes and thermostats.

The House can also switch the heat pump and hot water (see [Heat pump](#heat-pump)).

## Install

**HACS (recommended):** HACS → ⋮ → Custom repositories → add `https://github.com/goran-mihelcic/room_climate` as *Integration*. Install **Room Climate** and restart HA.

**Manual:** copy `custom_components/room_climate/` into your HA `config/custom_components/` folder and restart HA.

Then go to **Settings → Devices & services → Add integration → Room Climate**. The first time this sets up the House; every time after that it adds a room.

> **Upgrading from 0.2.x:** rooms from 0.2 cannot be migrated and show *Migration error*. Delete them, add the House, then add the rooms again.

## Setup

### House (once)

| Field | Example | |
|---|---|---|
| Outdoor temperature sensor | `sensor.outdoor_temperature` | the HVAC season is computed from it (see [HVAC season](#hvac-season)) |
| Reference room temperature sensor | `sensor.living_room_temperature` | optional; an indoor temperature that can force heating or cooling |
| At home entity | `input_boolean.at_home` | off: nobody is home |
| Prepare house entity | `input_boolean.prepare_house` | optional; pre-heat or pre-cool before arrival |
| Heat pump | `climate.heat_pump` | optional; heating while any radiator should heat, off otherwise (see [Heat pump](#heat-pump)) |
| Hot water | `water_heater.hot_water` | optional; on at the hot water temperature while someone is home or *Prepare house* is on, off otherwise (see [Hot water](#hot-water)) |
| Notify services | `mobile_app_my_phone` or `notify.my_group` | optional; told when an A/C does not switch mode. A notify action (mobile app) or a notify entity (e.g. a notify group helper) |

### Room (once per room)

| Field | Example | |
|---|---|---|
| Radiators | `climate.smart_radiator_thermostat_lr`, `climate.smart_radiator_thermostat_dining` | required |
| Air conditioners | `climate.living_room_air` | optional |
| Floor heating | `climate.floor_heating_living_room` | optional; the room's own floor heating (see [Floor heating](#floor-heating)) |
| Floor thermostat (pump) | `climate.smart_thermostat_x` | optional; the thermostat that runs this floor's radiator pump |
| Schedule | `schedule.living_room` | optional [schedule helper](https://www.home-assistant.io/integrations/schedule/); without one the room is always on schedule |
| Room temperature sensor | `sensor.living_room_temperature` | optional; without it the radiators' own readings are used |
| Room humidity sensor | `sensor.living_room_humidity` | optional; without it the radiators' (then the A/C's) own readings are used |
| Manual changes | *Follow (within limits)*, *Block* or *Manual control* | |
| Manual limits | heating 16–24 °C, cooling 22–28 °C | |
| Room has a bathroom | | ticking it opens a second step |

**Bathroom step:** bathroom radiators, floor heating, and a bathroom schedule helper.

A new room's form starts with the manual-change policy and manual limits of the room added last; devices, schedules and the bathroom are always picked fresh. Several rooms can share one schedule helper.

Everything can be changed later with **Configure** on the entry. Create the schedule helpers under **Settings → Devices & services → Helpers → Schedule**; they can differ per weekday.

## HVAC season

The House works out the season from the outdoor temperature, the reference room temperature and presence, checked in this order:

| Season | When |
|---|---|
| **Cooling** | outdoor above *Cooling season temperature* (29 °C) **or** reference room above *Indoor cooling threshold* (29 °C) |
| **Heating** | someone at home **and** (outdoor below *Heating season temperature* (18 °C) **or** reference room below *Indoor heating threshold* (21 °C)) |
| **Frost protection** | nobody at home **and** outdoor at or below *Frost season temperature* (5 °C) |
| **Comfort** | anything else |

- **Hysteresis** (*Season hysteresis*, 1 °C): a season that is on keeps going until its temperature is that far back on the other side of its threshold. Heating that started below 18 °C outside lasts until it is 19 °C or warmer.
- **Started by the indoor temperature:** heating that the reference room's temperature started keeps going until that room reaches its **own target** (comfort on schedule, eco off schedule, or a manual or override target). Cooling started that way keeps going until the room is down to its cooling target. Either way it never ends before the threshold plus the hysteresis. The reference room is the room whose *Room temperature sensor* is the House's reference sensor; without such a room, threshold plus hysteresis applies. The *Season* sensor's `started_by` attribute shows `indoor` or `outdoor`.
- **Delay** (*Season change delay*, 30 min): a new season must be wanted for that long before it takes over; a shorter spell is ignored. Leaving or coming home acts at once.
- An unavailable outdoor sensor keeps the current season. Until the sensor has a first value, rooms wait. After a restart the last season carries on.
- **Heat pump:** heating while any room or bathroom has radiators set to heat, off otherwise (see [Heat pump](#heat-pump)).
- **Nobody home in heating weather** (outdoor between the frost and heating season temperatures) is Comfort: every radiator, bathroom radiator and floor thermostat is switched off.

All these temperatures are on the House device (Configuration). The *Season* sensor shows the outdoor and reference temperatures and, while a change is waiting out the delay, the next season and since when.

Houses set up before 0.4 read the season from an *HVAC season entity* until an outdoor sensor is chosen under **Configure**.

## What a room does

| HVAC season | Room in use | Room not in use |
|---|---|---|
| **Heating** | heats: comfort temperature on schedule, eco temperature off schedule; A/C off | frost protection |
| **Frost protection** | frost protection; **pre-heat temperature** while *Prepare house* is on | frost protection |
| **Comfort**, nobody home in heating weather, *Prepare house* on | **pre-heat temperature** | off |
| **Cooling** | A/C cools to the cooling temperature on schedule, off outside it. Needs someone home or *Prepare house* on. Radiators off. | off |
| **Comfort** | off | off |
| not known yet (outdoor sensor never read), or no House set up | devices held as they are (status *waiting*) | same |

The frost protection temperature is set on the House device and applies to every room. The pre-heat temperature is set on the House device too; a room can override it with its own *Pre-heat temperature* (0 = use the House's). Its bathroom follows the room.

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

In the heating season, when the room is more than *Boost threshold* below its target, boost starts. It ends when the room is within *Boost exit margin* of the target (default 0.3 °C). The **Boost method** select (Configuration on the room device; rooms with an A/C or floor heating) decides what the boost uses. It offers only what the room has:

- *Raise radiator setpoints* (default): radiators get target + boost offset, capped at each device's max.
- *A/C in heat mode:* the A/C heats to the target until boost ends, then switches off; its fan runs at *high*. Setpoint changes on the A/C during boost are reverted.
- *Floor heating:* the floor heating runs at its *Floor heating temperature* until boost ends. That is the room's own floor heating, or its bathroom's if the room has none.
- *Radiators and A/C*, *Radiators and floor heating*, *A/C and floor heating*, *Radiators, A/C and floor heating*.

Radiators not raised by the boost stay at the target.

### Floor heating

A room can have floor heating of its own (*Floor heating* in the room setup; the bathroom's floor heating is set in the bathroom step). It is off unless:

- a boost uses it (see *Boost method*), or
- its **Floor heating** switch (room device, off by default) is on: then it also heats whenever the room heats: comfort, eco and pre-heat. Not in frost protection, comfort season or cooling.

It is set to the room's *Floor heating temperature* (27 °C). A setpoint changed on its thermostat is kept until the next period change. It counts as heating for the heat pump.

### Drying

When a room's humidity is above the House's *Drying humidity threshold* and the room is neither heated nor cooled, its A/C runs in *dry* mode until humidity falls to threshold − *Drying hysteresis*. Frost protection does not stop drying; heating, pre-heat and cooling do. This applies whether or not the room is in use.

### A/C commands

Room Climate keeps what each A/C *should* be doing (mode and temperature) and checks that it follows. Every instruction goes in the same order:

1. Set the HVAC mode.
2. Once the A/C reports the new mode: set the temperature.
3. Set the fan to *auto*, or to *high* while the A/C heats as part of a boost.

Each step is sent **once** and then given the room's **A/C response time** (default 60 s, 3–600 s) to show up in the A/C's state. Nothing is re-sent meanwhile. That suits LG ThinQ, which reports through slow cloud polling and refuses commands while someone uses the LG app. If the A/C still hasn't followed by then, the step is sent again. After a second miss the House's notify services get a message (once, until it works again), and it keeps being retried every response time.

Waiting does not hold up the room: radiators, bathroom and floor thermostat are driven as usual meanwhile. A late report from an A/C that is still catching up is never taken for a manual change.

### Heat pump

Room Climate keeps the House's heat pump **heating while any radiator should heat** — any room or bathroom whose radiators are set to heat: heating, frost protection, pre-heat, a bathroom's *Heating on* in any season but cooling — and **off when none is**, whatever the season. At start-up it is left alone until the season is known and every room has reported. A `climate` heat pump is set with its HVAC mode, `heat` or `off`, and must report exactly that: `climate.turn_on` could bring back an old `cool` mode. Other heat pump entities (switch, input_boolean) are turned on or off.

Like the A/Cs, each command is sent once and given the **Heat pump response time** (House device, default 5 min) to show up in the heat pump's state, then sent again; after a second miss the notify services are told once. A change made by hand the other way is undone.

### Hot water

The House's hot water (an LG `water_heater`) is **on** at the *Hot water temperature* (House device, default 45 °C) while someone is home or *Prepare house* is on, so the water is warm on arrival, and **off** while nobody is home, all year round. It is switched on first and given the temperature once it reports on, and checked like the heat pump: same *Heat pump response time*, re-sent, one notification after two misses, changes by hand undone.

### Cloud command queue

Commands to devices of a **cloud** integration (LG ThinQ) go out **one at a time**, from every room and the House alike, with the House's *Cloud command pause* (default 5 s) between them. LG rejects a request while it is still handling another one, so A/Cs, heat pump and hot water no longer step on each other. Devices of local integrations (Matter, Zigbee) are not queued.

### Bathroom

| | Bathroom schedule on | Bathroom schedule off |
|---|---|---|
| Bathroom radiator(s) | bathroom comfort temperature | follow the room target |
| Floor heating | on, at floor heating temperature | off |

The bathroom schedule applies only while the room is heating. In frost protection the bathroom radiators hold the frost temperature, during pre-heat they follow the room, and the floor heating is off.

**Heating on** (switch on the bathroom device): heats the bathroom outside its schedule for the *Heating on duration* (default 60 min; 0 = until switched off), then it goes back to the schedule. The bathroom radiators go to the bathroom comfort temperature and the floor heating comes on, in any season but cooling (outside the heating season the heat pump is switched on for the radiators). *Resume automatic* also ends it; it is kept across restarts.

### Override

The *Override* switch applies the override temperature (heating or cooling) for the override duration (0 = until switched off). It survives restarts.

## Automatic control

The House device's **Automatic control** switch (on by default) turns Room Climate's management on and off for the whole house.

- **Off:** nothing is sent to any device: radiators, A/Cs, bathrooms, floor thermostats, heat pump and hot water all stay exactly as they are. Nothing is switched off or reset. Changes made by hand are not taken as manual changes, and commands still waiting for a slow A/C are dropped. Room and bathroom *Status* show **Paused**; the House *Season* keeps being worked out, so you can see what would happen.
- **On:** every room drops its manual targets and applies what the current situation calls for at once; the heat pump and hot water are checked afresh.

The switch keeps its state across restarts.

## Manual changes on devices

- **Follow:** a setpoint changed on a remote or radiator becomes the room's target, clamped to the configured limits (the A/C set to 18 °C becomes 22 °C), and is applied to all devices of the room. A radiator's correction (and, during a boost, the boost offset) is taken off first: turning a radiator showing 23 °C up to 24 °C raises the room target by 1 °C. A radiator that only took over another radiator's setpoint (Tado syncing a room) is not a manual change. The manual target is dropped at the next period change: schedule on ↔ off, a bathroom schedule change, season, presence, in use or override changes, or any setting change on the device. A device turned off by hand stays off until the next period change.
- **Block:** the planned temperature is restored straight away, and a device switched off by hand is switched back on. Choose it per room under **Configure → Manual changes** to keep a room out of manual mode entirely.
- **Manual control:** a device someone changes by hand (setpoint, or switched on or off with its own controls or remote) is left exactly as they set it: Room Climate sends it nothing until the next period change (room schedule on ↔ off; season, presence, *In use*, *Prepare house* or override changes) or until *Resume automatic* is pressed (a setting change on the room device also ends it). The room's other devices stay automatic. Bathroom devices follow the bathroom schedule instead of the room's, so the bathroom and the room go to manual control separately. Status shows **Manual control**, and the *Target temperature* sensor lists the devices in its `manual_control` attribute. A radiator or A/C switched on by hand counts for the pump thermostat and the heat pump with its own setpoint. In a Tado room the heads copy each other's setpoint, so both heads usually go to manual control together.
- **Resume automatic** (button on the room device): drops manual targets in the room and its bathroom, switches devices turned off by hand back on, ends an override and the bathroom's *Heating on*. The room follows its schedule again at once.
- **Wrong mode** (A/C switched to cool during heating, or turned on when it should be off) is always reverted.

Commands sent by the integration carry their own context and devices get a 45 s settling window, so a device reporting a command back is never mistaken for a manual change.

## Entities

**House device**
- *Season* sensor (`heating`, `cooling`, `comfort`, `frost_protection`, `waiting`); attributes show the outdoor and reference temperatures, the next season and since when, pre-heat, presence, prepare house and each floor thermostat's setpoint.
- *Automatic control* switch (see [Automatic control](#automatic-control)).
- *Debug log file* switch (see [Logging](#logging)).
- *Frost protection temperature* (12 °C, what rooms keep in frost protection), *Pre-heat temperature* (18 °C, for every room that has no pre-heat temperature of its own), *Drying humidity threshold* (65 %), *Drying hysteresis* (5 %).
- Season (see [HVAC season](#hvac-season)): *Heating season temperature* (18 °C), *Cooling season temperature* (29 °C), *Indoor heating threshold* (21 °C), *Indoor cooling threshold* (29 °C), *Frost season temperature* (5 °C), *Season hysteresis* (1 °C), *Season change delay* (30 min).
- *Heat pump response time* (5 min; with a heat pump or hot water), *Hot water temperature* (45 °C; with hot water), *Cloud command pause* (5 s).

**Room device**
- *Target temperature* sensor (all state as attributes: season, schedule, room temperature, humidity, boost, radiator corrections, drying, manual target, …) and *Status* sensor (`comfort`, `eco`, `boost`, `manual`, `override`, `preheat`, `frost_protection`, `drying`, `off`, `waiting`, `paused`).
- Controls: *Resume automatic* (button), *In use*, *Override*, *Floor heating* and *Floor heating temperature* (27 °C) (rooms with floor heating), *Comfort temperature* (21 °C), *Eco temperature* (19 °C), *Cooling temperature* (25 °C), *Override temperature* (22 °C), *Pre-heat temperature* (0 = the House's; any other value overrides it for this room).
- Configuration: *Boost method* (rooms with an A/C or floor heating), *A/C response time* (60 s) (rooms with an A/C), *Boost threshold* (1.5 °C), *Boost offset* (3 °C), *Boost exit margin* (0.3 °C), *Max radiator correction* (3 °C), *Override duration* (120 min).

**Bathroom device**
- *Target temperature*, *Status* (`bath_period`, `heating_on`, `follow_room`, `manual`, `frost_protection`, `off`, `waiting`, `paused`), *Comfort temperature* (24 °C), *Floor heating temperature* (27 °C), *Heating on* switch, *Heating on duration* (60 min).

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
