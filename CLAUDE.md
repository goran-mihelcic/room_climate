# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**Room Climate** is a Home Assistant custom integration (domain `room_climate`), distributed through HACS from `goran-mihelcic/room_climate`. Each config entry is one room. It creates a room device (and an optional linked bathroom device) and drives existing `climate.*` entities: radiator TRVs and thermostats for heating, an AC for cooling, and bathroom radiators plus floor heating.

It runs in a real house (Krk, Croatia) with Tado X devices over Matter, Zigbee devices and an LG AC. Treat behaviour changes as changes to someone's heating: be conservative and keep the tests green.

## Layout

```
custom_components/room_climate/
  __init__.py      setup/unload, PLATFORMS, reload on options change
  config_flow.py   UI setup: room step + optional bathroom step; options flow (Configure)
  const.py         option keys, house-status mapping, entity specs and defaults
  controller.py    RoomController: ALL decision logic and device driving
  entity.py        RoomClimateMixin: device_info, unique_id, has_entity_name
  number.py        setpoints and tuning values (RestoreNumber)
  select.py        Boost method (only for rooms with a cooling device)
  time.py          comfort window and bathroom periods (TimeEntity + RestoreEntity)
  switch.py        Enabled, Override (with expiry timer)
  sensor.py        Target temperature (all state as attributes) and Status (enum)
  strings.json     UI text; keep translations/en.json an identical copy
tests/
  conftest.py      enables custom integrations
  test_room.py     end-to-end tests against a real HA core
```

## Architecture

- **One brain.** `RoomController` owns all state. Entities are thin: they restore their value into `ctl.values[key]` in `async_added_to_hass`, write into it on change, and call `ctl.settings_changed()`. Sensors read computed fields (`room_target`, `room_status`, `bath_target`, ...) and subscribe via `ctl.async_add_listener`.
- **Update cycle.** `request_update()` → `Debouncer` (2 s cooldown) → `_async_apply()` = `_compute(now)` → `_plan()` → `_async_drive()` per entity. Triggers: state changes of the watched entities, a 60 s interval, and setting changes. Nothing runs until HA has fully started (`async_at_started`).
- **Regime** (`_compute`), in priority order: house status unavailable → `waiting` (devices untouched) · room disabled → frost (heating/frost seasons) or off · house "Frost protection" → frost · nobody home → frost in heating, off otherwise · "Heating" → heat · "Cooling" → cool · "Comfort" → everything off.
- **Room target:** frost temp, or heat/cool setpoint inside the comfort window, setpoint ∓ setback outside it. Override beats a manual target, which beats the schedule.
- **Boost** (heating only, hysteresis): starts when `target − room > boost_delta`, ends when within `BOOST_EXIT_MARGIN` (0.3 °C). The boost method decides whether heaters get `target + boost_offset`, the AC runs in `heat` at the target, or both.
- **Bathroom:** inside the morning/evening periods the radiators use the bathroom comfort temperature and floor heating is on; otherwise radiators follow the room target and the floor is off. Heating season only.
- **Device groups** (`DeviceGroup`): heaters, coolers, bath_heaters, floor. Each tracks `desired` (mode, temp), `sent_at`, `manual_target`, `manual_off`, and a `period_key`. When the period key changes (regime, window, override), manual state is reset.
- **Driving devices:** only send `set_hvac_mode` / `set_temperature` when the state differs (battery TRVs). Clamp to the device's `min_temp`/`max_temp` and round to `target_temp_step`. Devices without an `off` mode are set to `min_temp` instead.

## Manual-change detection (the subtle part)

Someone changing a setpoint on a remote or thermostat is detected in `_check_manual_change`. Three guards stop the integration's own commands being mistaken for manual changes; keep all of them:

1. Every service call uses a fresh `Context` whose id is stored in `_own_contexts`; events with that context (or parent) are ignored.
2. `_driving` holds entities currently being commanded.
3. `sent_at` is stamped **before** the call; reports within `COMMAND_GRACE_SECONDS` (45 s) are ignored. Slow Zigbee/Matter devices echo late.

Policy `follow` adopts the new setpoint for the whole room, clamped to the configured manual limits, until the next period change. Policy `block` restores immediately. A wrong mode (e.g. AC switched to cool in heating season) is always reverted. AC setpoint changes during an AC heat boost are reverted, never adopted.

## Commands

Home Assistant does not run on native Windows, so the owner works in **WSL 2 (Ubuntu)** with the repo at `~/src/room_climate`. Full setup is in `TESTING.md`.

```bash
source .venv/bin/activate                     # once per terminal
uv pip install -r requirements_test.txt       # first time, or after changing pins (venv made by uv)
pytest -q
ruff format --line-length 100 custom_components tests
ruff check --line-length 100 --select E,F,W,B,I custom_components tests
```

Pinned versions live in `requirements_test.txt` (last verified: Home Assistant 2026.2.3, Python 3.13).

## Rules for changes

- **Run the tests and ruff before saying a change is done.** Add a test for every behaviour change; tests drive real HA services and assert on the fake devices' resulting state.
- **Never change entity keys** (`ROOM_NUMBERS`, `BATH_NUMBERS`, `ROOM_TIMES`, `BATH_TIMES`, switch/select/sensor keys). `unique_id` is `f"{entry_id}_{key}"`, so renaming a key orphans the user's entity and breaks dashboards and automations.
- **Options are stored per entry.** If you add or rename an option, read it with a default (`opts.get(KEY, DEFAULT)`) so existing rooms keep working, or add a config entry migration (`async_migrate_entry`, bump `VERSION`).
- **Runtime-tunable values belong on the device** as number/select/time/switch entities (restored on restart), not in the options flow. Device selection and limits belong in the options flow. The owner expects to see and change room behaviour from the device page and dashboard.
- **UI text:** edit `strings.json`, then copy it to `translations/en.json`. Entity names are set in code (`_attr_name`); enum and select states use `translation_key`.
- **No blocking I/O in the event loop**, no polling (`_attr_should_poll = False`), no new `requirements` in the manifest without asking.
- Don't hard-code the owner's entity IDs in integration code; they appear only in tests and docs.
- Ask before changing default values or the meaning of existing settings: they alter heating in a lived-in house.

## Testing notes

- The `house` fixture registers fake `climate.set_hvac_mode` / `climate.set_temperature` services that update the entity state **with `context=call.context`**. Keep that, or the own-command guard can't be tested.
- `ctl_mod.COMMAND_GRACE_SECONDS = 0` in the fixture so manual changes are seen immediately; time is controlled with `freezer` and `async_fire_time_changed`. Use `_settle()` after changing state so the debouncer runs.
- Time zone is `Europe/Zagreb`; the default test time is 10:00 local on a weekday (inside the comfort window, outside bathroom periods).
- Entity IDs in tests derive from the entry title "Living Room", e.g. `sensor.living_room_status`, `select.living_room_boost_method`, `sensor.living_room_bathroom_status`.

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
- `CONF_BOOST_METHOD` in options is read only as the initial value for the Boost method select (migration from 0.2.0). It can be removed once no room has it.
- No CI yet. A GitHub Actions workflow running pytest, ruff and `hacs/action` validation would be the natural next step.
