# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**Room Climate** is a Home Assistant custom integration (domain `room_climate`), distributed through HACS from `goran-mihelcic/room_climate`. There is one **House** config entry and one config entry per **room**. The house reads the HVAC season, presence and "prepare house" inputs, holds the settings shared by all rooms, and drives the floor (pump) thermostats. Each room creates a room device (and an optional linked bathroom device) and drives existing `climate.*` entities: radiator heads, A/C units, bathroom radiators and floor heating.

It runs in a real house (Krk, Croatia) with a heat pump, Tado X devices over Matter, Zigbee devices and an LG A/C. Treat behaviour changes as changes to someone's heating: be conservative and keep the tests green.

## Layout

```
custom_components/room_climate/
  __init__.py      setup/unload (house or room by entry.data["type"]), PLATFORMS, migration guard
  config_flow.py   UI setup: house step (first entry), room step + optional bathroom step; options flow
  const.py         option keys, season mapping, entity specs and defaults
  house.py         HouseController (inputs, shared numbers, floor thermostats) and DomainData
  controller.py    RoomController: ALL room decision logic
  driver.py        DeviceGroup and ClimateDriver: sending commands, own-command guards, A/C sequence
  debug_log.py     DebugFileLog: optional rotating /config/room_climate.log via a QueueListener thread
  entity.py        RoomClimateMixin: device_info, unique_id, has_entity_name
  number.py        setpoints and tuning values (RestoreNumber), house and room
  select.py        Boost method (only for rooms with an A/C)
  switch.py        In use, Override (with expiry timer); House: Debug log file
  button.py        Resume automatic (room): drop manual state, end override
  sensor.py        House Season; room/bathroom Target temperature (all state as attributes) and Status
  strings.json     UI text; keep translations/en.json an identical copy
tests/
  conftest.py      enables custom integrations
  test_room.py     end-to-end tests against a real HA core
```

## Architecture

- **House and rooms.** `hass.data[DOMAIN]` is a `DomainData` with `house` (the `HouseController` or `None`) and `rooms` (entry_id → `RoomController`). Rooms read the house live (`read_season()`, `read_at_home()`, `read_prepare()`, `values`) on every update. The house watches its input entities and calls `request_update()` on every room when they or its numbers change. Without a house, or while the season is unavailable, rooms are `waiting` and leave devices alone.
- **Season** (`HouseController._evaluate_season`, from 0.4): computed when the House has an outdoor sensor (`CONF_OUTDOOR`); otherwise the pre-0.4 `season_entity` is still read (`computes_season`). `_season_for` checks in order: Cooling (outdoor > `cool_season` or reference > `cool_indoor`) · Heating (at home and (outdoor < `heat_season` or reference < `heat_indoor`)) · Frost protection (away and outdoor ≤ `frost_season`) · Comfort. The current season gets `season_hysteresis` in its favour; a different season must be wanted for `season_delay` minutes (`pending`), except at start-up (season None) and when presence changes. Unavailable outdoor holds the season; unavailable reference simply doesn't count. The Season sensor restores the last computed season so hysteresis survives restarts. `preheat` = prepare house, away, and (Frost protection or Comfort with outdoor < `heat_season`); rooms in use then use the `preheat` regime in a Comfort season too. The heat pump (`CONF_HEAT_PUMP`, `<domain>.turn_on`) is turned on whenever heating starts (Heating, Frost or pre-heat) and never off (hot water). Evaluated on input changes, House number changes and the 60 s tick. "Nobody home in heating weather" needs no extra code: it is Comfort, and rooms switch everything off.
- **Devices** are registered in `__init__._async_register_devices` before the platforms load; the bathroom device is linked to the room with `async_update_device(via_device_id=...)`. Don't put `via_device` in `DeviceInfo` (deprecated from HA 2026.8, removed in 2027.8), and don't use `device_registry.async_get_device` (identifiers are no longer unique across entries from 2026.10); use `dr.async_entries_for_config_entry`.
- **One brain per room.** `RoomController` owns all room state. Entities are thin: they restore their value into `ctl.values[key]` in `async_added_to_hass`, write into it on change, and call `ctl.settings_changed()`. Sensors read computed fields (`room_target`, `room_status`, `bath_target`, ...) and subscribe via `ctl.async_add_listener`. The house works the same way for its numbers and Season sensor.
- **Update cycle.** `request_update()` → `Debouncer` (2 s cooldown) → `_async_apply()` = `_compute()` → `_plan()` → `driver.async_drive()` per entity → report the floor thermostat demand to the house. Triggers: state changes of the room's devices, sensors and schedules, a 60 s interval, house changes and setting changes. Nothing runs until HA has fully started (`async_at_started`).
- **Regime** (`_compute`): season unavailable or no house → `waiting` · Heating while nobody is home is treated as Frost protection · Frost protection → `preheat` if *prepare house* is on and the room is in use, else `frost` · Heating → `heat` if in use, else `frost` · Cooling → `cool` if in use and (at home or prepare house), else `off` · Comfort → `preheat` if in use and the house says `preheat`, else `off`.
- **Room target:** frost and pre-heat temperatures come from the house. Heating: comfort temperature while the room's schedule entity is on, eco temperature while it is off. Cooling: cooling temperature on schedule, no target (A/C off) off schedule. No schedule entity = always on schedule. Override beats a manual target, which beats the schedule.
- **Radiator correction** (`_update_corrections`): only with a room temperature sensor. `reading − room` rounded **down** to whole `CORRECTION_STEP`s (0.5 °C), capped at `correction_max`; none below one step. Recomputed every cycle, never more than the actual difference (the owner's rule: no hysteresis that could hold a larger value). **Radiators share one correction** (average of each head's `max(reading − room, 0)`) and `_plan` caps them at the lowest `max_temp` among them, so all heads of a room always get the identical setpoint: Tado X copies a room's setpoint between heads, and differing setpoints made them ratchet the manual target up/down by the correction difference (29 °C / 16 °C incident, 2026-10-30) and ping-pong commands every few seconds. Each A/C keeps its own correction. Added to radiator setpoints in heat, frost and pre-heat, and to the A/C during a heat boost. Never in cooling, never on bathroom radiators.
- **Boost** (heating only, hysteresis): starts when `target − room > boost_delta`, ends when `target − room ≤ boost_margin`. The boost method decides whether radiators get `target + boost_offset`, the A/C runs in `heat` at the target, or both.
- **Drying:** allowed when the room has no target (off, or cooling off schedule) or is in frost protection. Starts above the house `dry_humidity`, stops at `dry_humidity − dry_hysteresis`. Only A/C units that list `dry` in `hvac_modes`.
- **Floor thermostats** (`HouseController.set_pump_demand`): each room reports `{thermostat: highest heating setpoint of its radiators and bathroom radiators, or None}`, and withdraws its demand while waiting or on unload. The house drives each thermostat to the max over all rooms, or off when none is heating, through its own debouncer and every 60 s.
- **Bathroom:** while the room is heating and the bathroom schedule is on, the radiators use the bathroom comfort temperature and floor heating is on; otherwise radiators follow the room target and the floor is off.
- **Device groups** (`DeviceGroup`): heaters, coolers (`ac=True`), bath_heaters, floor; the house has a `pumps` group. Each tracks `desired` (mode, temp), `sent_at`, `manual_target`, `manual_off`, and a `period_key`. When the period key changes (regime, schedule, override, and drying for coolers), manual state is reset.
- **Driving devices** (`ClimateDriver`): only send `set_hvac_mode` / `set_temperature` when the state differs (battery TRVs). Clamp to the device's `min_temp`/`max_temp` and round to `target_temp_step`. Devices without an `off` mode are set to `min_temp` instead. A/C units (`_async_ac_mode`, non-blocking): send the mode; if the A/C reports it straight away, temperature and fan `auto` follow in the same cycle. Otherwise it is *pending* until `ac_response` seconds (number on rooms with an A/C, default 60): nothing else is sent to it, an `async_call_later` wakes the room at the deadline, then the mode is re-sent (`AC_MODE_ATTEMPTS` = 2 per report); after that the notify services are told once per failure episode and it keeps retrying. A notify target is a classic action (`notify.<name>`, title + message) if that service exists, otherwise a notify entity (`notify.send_message` with `entity_id: notify.<name>`, message only); the House options flow accepts either. When it reports the mode, temperature and fan are sent. Never `sleep` in the update cycle: one slow device would freeze the whole room. `_async_call` catches *any* exception from a service call (device integrations have bugs: HA's LG ThinQ raises `TypeError` in `set_temperature` when `data.step` is None, leaving the A/C at its own 30 °C heat default) so one device can't stop the rest of the room; the traceback is logged once per (entity, service) until a call succeeds. `_async_apply` reports the floor thermostat demand and notifies the sensors in a `finally`.
- **Logging:** warnings for things the owner should see, `info` for manual changes, `debug` for every command and for one decision line per room (`_log_decision`, only when it changes). The House **Debug log file** switch sends the `custom_components.room_climate` logger at debug level to its own file, turns propagation off and forwards records at the previous level to the root handlers, so HA's log is unchanged. Keep debug output useful in that file: state *why*, not just *what*.

## Manual-change detection (the subtle part)

Someone changing a setpoint on a remote or thermostat is detected in `_check_manual_change`. Three guards stop the integration's own commands being mistaken for manual changes; keep all of them:

1. Every service call uses a fresh `Context` whose id is stored in the driver's `_own_contexts`; events with that context (or parent) are ignored.
2. `driver.driving` holds entities currently being commanded, and `driver.is_pending()` A/Cs that have not reported a mode command yet (their late reports are never manual; the settling window starts when they confirm).
3. `sent_at` is stamped **before** the call; reports within `COMMAND_GRACE_SECONDS` (45 s) are ignored. Slow Zigbee/Matter devices echo late.

A reported setpoint equal to another device's desired setpoint in the same group is a device copying it (Tado room sync), never a manual change. Policy `follow` adopts the new setpoint for the whole room (minus the radiator's correction and, while radiators are boosted, the boost offset), clamped to the configured manual limits, until the next period change; a result equal to the current room target is ignored. `_check_manual_change` judges the event's `new_state`, not the current state: reading the current state lost real remote changes when an update ran between the event and the listener. Policy `block` restores immediately. A wrong mode (e.g. A/C switched to cool in heating season) is always reverted. A/C setpoint changes during an A/C heat boost are reverted, never adopted. Manual setpoints only count in `heat` and `cool`; in frost and pre-heat they are restored. Floor thermostats have no manual handling; they are re-driven. The *Resume automatic* button calls `resume_automatic()`: `settings_changed()` plus ending an override through `end_override`, which the Override switch registers (it owns the expiry timer).

## Commands

Home Assistant does not run on native Windows, so the owner works in **WSL 2 (Ubuntu)** with the repo at `~/src/room_climate`. Full setup is in `TESTING.md`.

```bash
source .venv/bin/activate                     # once per terminal
uv pip install -r requirements_test.txt       # first time, or after changing pins (venv made by uv)
pytest -q
ruff format --line-length 100 custom_components tests
ruff check --line-length 100 --select E,F,W,B,I custom_components tests
```

The same venv can test the Windows checkout: `wsl -e bash -lc "cd /mnt/c/Users/GoranMihelcic/source/repos/room_climate && ~/src/room_climate/.venv/bin/pytest -q -p no:cacheprovider"`.

Pinned versions live in `requirements_test.txt` (last verified: Home Assistant 2026.2.3, Python 3.13).

## Rules for changes

- **Run the tests and ruff before saying a change is done.** Add a test for every behaviour change; tests drive real HA services and assert on the fake devices' resulting state.
- **Never change entity keys** (`HOUSE_NUMBERS`, `ROOM_NUMBERS`, `BATH_NUMBERS`, switch/select/sensor keys). `unique_id` is `f"{entry_id}_{key}"`, so renaming a key orphans the user's entity and breaks dashboards and automations.
- **Options are stored per entry.** If you add or rename an option, read it with a default (`opts.get(KEY, DEFAULT)`) so existing entries keep working, or add a config entry migration (`async_migrate_entry`, bump `VERSION`). Version 1 entries (0.2.x) are deliberately not migrated.
- **Runtime-tunable values belong on the device** as number/select/switch entities (restored on restart), not in the options flow. Values shared by all rooms go on the House device. Device selection, schedules and limits belong in the options flow. The owner expects to see and change behaviour from the device page and dashboard.
- **UI text:** edit `strings.json`, then copy it to `translations/en.json`. Entity names are set in code (`_attr_name`); enum and select states use `translation_key`.
- **No blocking I/O in the event loop**, no polling (`_attr_should_poll = False`), no new `requirements` in the manifest without asking.
- Don't hard-code the owner's entity IDs in integration code; they appear only in tests and docs.
- Ask before changing default values or the meaning of existing settings: they alter heating in a lived-in house.

## Testing notes

- The `house` fixture registers fake `climate.set_hvac_mode` / `set_temperature` / `set_fan_mode` services that update the entity state **with `context=call.context`**. Keep that, or the own-command guard can't be tested. It also registers `notify.mobile_app_phone`, and returns `calls`, `notes` (notifications), `stuck` (entities whose hvac mode won't change, for the A/C failure path), `broken` (entities whose `set_temperature` raises `TypeError`) and `linked` (`{a: b}`: a setpoint sent to `a` is copied to `b` with `a`'s mode and no context, like heads of one Tado room).
- Tests that write files set `hass.config.config_dir = str(tmp_path)` first; read files with `hass.async_add_executor_job`.
- A second venv with the newest HA (needs Python 3.14) is at `~/rc_latest/.venv` in WSL; run the suite with both before releasing.
- `ctl_mod.COMMAND_GRACE_SECONDS = 0` in the fixture so manual changes are seen immediately; time is controlled with `freezer` and `async_fire_time_changed`. Use `_settle()` after changing state so the debouncers run; `_set()` and `_service()` include it. `_settle(seconds)` ticks *twice*; use `_later(seconds)` to move time exactly once (A/C response deadlines). Fake A/Cs report a mode at once unless in `stuck`.
- Fake devices read 15 °C by default, below the room sensor, so radiators get no correction unless a test sets `current_temperature`.
- Entries are `MockConfigEntry(version=2, data={"type": "house" | "room"})`. `_setup()` adds the House and the "Living Room" room. Most tests use `HOUSE_OPTIONS` (pre-0.4 season entity, set with `SEASON`) because they set the season directly; computed-season tests use `_setup_computed()` with `HOUSE_FORM` (outdoor `sensor.outdoor_temp` 10 °C, reference `sensor.reference_temp` 22 °C → heating at home). The heat pump is faked as `input_boolean.heat_pump`: Room Climate's switch platform loads HA's real `switch` integration, which would replace a fake `switch.turn_on`. Season delays: `_later(hass, freezer, 31 * 60)`.
- Time zone is `Europe/Zagreb`; the default test time is 10:00 local on a weekday. Schedules are plain states: `schedule.living_room` on, `schedule.living_room_bathroom` off.
- Entity IDs derive from the entry titles, e.g. `sensor.living_room_status`, `select.living_room_boost_method`, `sensor.living_room_bathroom_status`, `sensor.house_season`, `number.house_pre_heat_temperature`.

## Release

1. Bump `version` in `custom_components/room_climate/manifest.json` (semver).
2. Update `README.md` if behaviour or entities changed.
3. Commit, then tag and release so HACS offers the update:
   ```cmd
   git tag vX.Y.Z
   git push origin main --tags
   gh release create vX.Y.Z --generate-notes
   ```
4. On the HA box: HACS update (or redownload), then a full restart. Code changes need a restart, not a reload.

## Known to-dos

- `hacs.json` says minimum HA `2025.1.0`, but the platforms use `AddConfigEntryEntitiesCallback`, which newer HA versions provide. Check the version it was introduced in and update `hacs.json`.
- Window/door sensors are planned: an open window should pause heating, cooling and drying in that room.
- No CI yet. A GitHub Actions workflow running pytest, ruff and `hacs/action` validation would be the natural next step.
