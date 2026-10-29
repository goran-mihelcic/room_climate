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
  entity.py        RoomClimateMixin: device_info, unique_id, has_entity_name
  number.py        setpoints and tuning values (RestoreNumber), house and room
  select.py        Boost method (only for rooms with an A/C)
  switch.py        In use, Override (with expiry timer)
  sensor.py        House Season; room/bathroom Target temperature (all state as attributes) and Status
  strings.json     UI text; keep translations/en.json an identical copy
tests/
  conftest.py      enables custom integrations
  test_room.py     end-to-end tests against a real HA core
```

## Architecture

- **House and rooms.** `hass.data[DOMAIN]` is a `DomainData` with `house` (the `HouseController` or `None`) and `rooms` (entry_id → `RoomController`). Rooms read the house live (`read_season()`, `read_at_home()`, `read_prepare()`, `values`) on every update. The house watches its input entities and calls `request_update()` on every room when they or its numbers change. Without a house, or while the season is unavailable, rooms are `waiting` and leave devices alone.
- **One brain per room.** `RoomController` owns all room state. Entities are thin: they restore their value into `ctl.values[key]` in `async_added_to_hass`, write into it on change, and call `ctl.settings_changed()`. Sensors read computed fields (`room_target`, `room_status`, `bath_target`, ...) and subscribe via `ctl.async_add_listener`. The house works the same way for its numbers and Season sensor.
- **Update cycle.** `request_update()` → `Debouncer` (2 s cooldown) → `_async_apply()` = `_compute()` → `_plan()` → `driver.async_drive()` per entity → report the floor thermostat demand to the house. Triggers: state changes of the room's devices, sensors and schedules, a 60 s interval, house changes and setting changes. Nothing runs until HA has fully started (`async_at_started`).
- **Regime** (`_compute`): season unavailable or no house → `waiting` · Heating while nobody is home is treated as Frost protection · Frost protection → `preheat` if *prepare house* is on and the room is in use, else `frost` · Heating → `heat` if in use, else `frost` · Cooling → `cool` if in use and (at home or prepare house), else `off` · Comfort → `off`.
- **Room target:** frost and pre-heat temperatures come from the house. Heating: comfort temperature while the room's schedule entity is on, eco temperature while it is off. Cooling: cooling temperature on schedule, no target (A/C off) off schedule. No schedule entity = always on schedule. Override beats a manual target, which beats the schedule.
- **Radiator correction** (`_update_corrections`): only with a room temperature sensor. Per radiator (and A/C), `clamp(device reading − room, 0, correction_max)`, moved only when it changes by `CORRECTION_HYSTERESIS` (0.5 °C). Added to radiator setpoints in heat, frost and pre-heat, and to the A/C during a heat boost. Never in cooling, never on bathroom radiators.
- **Boost** (heating only, hysteresis): starts when `target − room > boost_delta`, ends when `target − room ≤ boost_margin`. The boost method decides whether radiators get `target + boost_offset`, the A/C runs in `heat` at the target, or both.
- **Drying:** allowed when the room has no target (off, or cooling off schedule) or is in frost protection. Starts above the house `dry_humidity`, stops at `dry_humidity − dry_hysteresis`. Only A/C units that list `dry` in `hvac_modes`.
- **Floor thermostats** (`HouseController.set_pump_demand`): each room reports `{thermostat: highest heating setpoint of its radiators and bathroom radiators, or None}`, and withdraws its demand while waiting or on unload. The house drives each thermostat to the max over all rooms, or off when none is heating, through its own debouncer and every 60 s.
- **Bathroom:** while the room is heating and the bathroom schedule is on, the radiators use the bathroom comfort temperature and floor heating is on; otherwise radiators follow the room target and the floor is off.
- **Device groups** (`DeviceGroup`): heaters, coolers (`ac=True`), bath_heaters, floor; the house has a `pumps` group. Each tracks `desired` (mode, temp), `sent_at`, `manual_target`, `manual_off`, and a `period_key`. When the period key changes (regime, schedule, override, and drying for coolers), manual state is reset.
- **Driving devices** (`ClimateDriver`): only send `set_hvac_mode` / `set_temperature` when the state differs (battery TRVs). Clamp to the device's `min_temp`/`max_temp` and round to `target_temp_step`. Devices without an `off` mode are set to `min_temp` instead. A/C units: set mode, wait `AC_MODE_CHECK_SECONDS` (3 s), check, retry once; on failure notify the house's notify services once per failure episode and send nothing else; then set temperature, then fan mode `auto`.

## Manual-change detection (the subtle part)

Someone changing a setpoint on a remote or thermostat is detected in `_check_manual_change`. Three guards stop the integration's own commands being mistaken for manual changes; keep all of them:

1. Every service call uses a fresh `Context` whose id is stored in the driver's `_own_contexts`; events with that context (or parent) are ignored.
2. `driver.driving` holds entities currently being commanded (including the 3 s A/C check).
3. `sent_at` is stamped **before** the call; reports within `COMMAND_GRACE_SECONDS` (45 s) are ignored. Slow Zigbee/Matter devices echo late.

Policy `follow` adopts the new setpoint for the whole room (minus that radiator's correction), clamped to the configured manual limits, until the next period change. Policy `block` restores immediately. A wrong mode (e.g. A/C switched to cool in heating season) is always reverted. A/C setpoint changes during an A/C heat boost are reverted, never adopted. Manual setpoints only count in `heat` and `cool`; in frost and pre-heat they are restored. Floor thermostats have no manual handling; they are re-driven.

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

- The `house` fixture registers fake `climate.set_hvac_mode` / `set_temperature` / `set_fan_mode` services that update the entity state **with `context=call.context`**. Keep that, or the own-command guard can't be tested. It also registers `notify.mobile_app_phone`, and returns `calls`, `notes` (notifications) and `stuck` (entities whose hvac mode won't change, for the A/C failure path).
- `ctl_mod.COMMAND_GRACE_SECONDS = 0` and `driver_mod.AC_MODE_CHECK_SECONDS = 0` in the fixture so manual changes are seen immediately and the A/C check doesn't sleep; time is controlled with `freezer` and `async_fire_time_changed`. Use `_settle()` after changing state so the debouncers run; `_set()` and `_service()` include it.
- Fake devices read 15 °C by default, below the room sensor, so radiators get no correction unless a test sets `current_temperature`.
- Entries are `MockConfigEntry(version=2, data={"type": "house" | "room"})`. `_setup()` adds the House and the "Living Room" room.
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
