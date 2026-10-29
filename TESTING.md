# Running the tests

The tests start a real Home Assistant core in memory, load Room Climate, and drive simulated climate devices (TRVs, thermostat, AC, bathroom radiator and floor heating). No Home Assistant installation, broker or real devices are needed, and nothing touches your house.

A full run takes a few seconds.

## Prerequisites

| What | Why |
|---|---|
| **Windows 10/11 with WSL 2** (Ubuntu) | Home Assistant does not run on native Windows: it uses Linux-only modules. On macOS or Linux, skip WSL. |
| **uv** inside WSL | Installs Python 3.13 (3.13.2 or later, required by Home Assistant 2026.x) and the dependencies |
| **git** inside WSL | To clone the repo |
| **About 1.5 GB free disk** | Home Assistant core plus its dependencies in a virtual environment |
| **VS Code with the WSL extension** (optional) | Edit and run from Windows while the code lives in Linux |

## One-time setup

### 1. Install WSL with Ubuntu

In **PowerShell as Administrator**:

```powershell
wsl --install -d Ubuntu-24.04
```

Restart when asked, then open **Ubuntu** from the Start menu and create a Linux user name and password.

If WSL is already installed, check that it is version 2: `wsl -l -v` should show `VERSION 2`.

### 2. Install git and uv in Ubuntu

**uv** is a small tool that downloads and manages Python itself, so it works on every Ubuntu version (no PPA needed). `build-essential` provides the C compiler that a few Home Assistant dependencies need when they install. In the **Ubuntu** terminal:

```bash
sudo apt update
sudo apt install -y git curl build-essential
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv --version
```

The last line should print a version, for example `uv 0.9.x`. If it says `command not found`, close and reopen the Ubuntu terminal.

### 3. Clone the repository into the Linux file system

Keep the code under your Linux home (`~`), not under `/mnt/c/...`. Tests on the Windows drive work, but installs and runs are many times slower.

```bash
mkdir -p ~/src && cd ~/src
git clone https://github.com/goran-mihelcic/room_climate.git
cd room_climate
```

If git asks who you are on the first commit:

```bash
git config --global user.name "Goran Mihelcic"
git config --global user.email "goran@mihelcic.net"
```

### 4. Create the virtual environment and install dependencies

```bash
uv python install 3.13
uv venv --python 3.13 .venv
source .venv/bin/activate
python --version
uv pip install -r requirements_test.txt
```

`python --version` should print `Python 3.13.x`. The install takes a minute or two the first time. `.venv` is already in `.gitignore`.

Use `uv pip install ...` rather than `pip install ...`: a uv-created venv has no `pip` of its own.

## Running the tests

Every time you open a new terminal:

```bash
cd ~/src/room_climate
source .venv/bin/activate
```

Your prompt now starts with `(.venv)`.

| Task | Command |
|---|---|
| Run all tests | `pytest` |
| Short output | `pytest -q` |
| One test | `pytest -k test_boost_with_ac_keeps_radiators_at_target` |
| Tests whose name matches a word | `pytest -k bath` |
| Stop at the first failure, show prints/logs | `pytest -x -s` |
| Show the integration's debug log on failure | `pytest -x --log-level=DEBUG` |
| Format the code | `ruff format --line-length 100 custom_components tests` |
| Lint the code | `ruff check --line-length 100 --select E,F,W,B,I custom_components tests` |

A good run ends with a line like:

```
25 passed in 2.4s
```

Run **format, lint and tests** before every commit.

## Working from VS Code on Windows

1. Install the **WSL** extension in VS Code.
2. In the Ubuntu terminal, inside the repo, run `code .`. VS Code opens connected to WSL (bottom-left shows *WSL: Ubuntu*).
3. Install the **Python** extension in the WSL window, then **Python: Select Interpreter** → `./.venv/bin/python`.
4. The **Testing** panel (flask icon) discovers the tests from `pytest.ini`; run or debug single tests from there.

Claude Code works the same way: start it from the Ubuntu terminal inside `~/src/room_climate`.

## What the tests cover

| Area | Examples |
|---|---|
| Setup dialogs | room + bathroom steps, overlapping devices rejected, house entities pre-filled, Configure dialog |
| House status | Heating, Cooling, Frost protection, Comfort, unavailable status leaves devices alone |
| Presence and Enabled | away → frost protection, disabled room |
| Schedules | comfort window and setback, bathroom morning/evening periods, floor heating on/off |
| Boost | raise heaters, AC in heat mode, both, hysteresis, select on the device |
| Manual changes | AC remote set to 18 °C clamped to 22 °C, block policy, radiator turned off by hand, reset at next period, own commands not mistaken for manual |
| Override and restart | override expiry, values and override restored after a restart |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ModuleNotFoundError: No module named 'fcntl'` | You are running on native Windows. Use the Ubuntu (WSL) terminal. |
| `requires a different Python: 3.12...` | The venv was made with the wrong Python. `rm -rf .venv` and repeat step 4. |
| `Unable to locate package python3.13` | You followed an older version of this guide (apt + PPA). Use uv as in step 2 instead. |
| `Failed to build lru-dict` … `No such file or directory: 'cc'` | No C compiler. Run `sudo apt install -y build-essential`, then repeat `uv pip install -r requirements_test.txt`. |
| `pip: command not found` inside the venv | Expected with uv. Use `uv pip install ...`. |
| `pytest: command not found` | The venv isn't active. Run `source .venv/bin/activate`. |
| Install is very slow, or tests take minutes | The repo is under `/mnt/c/...`. Clone it under `~` instead. |
| `Detected that custom integration ... does blocking call` or other new warnings after upgrading | Home Assistant changed an API. Read the warning; it names the file and line to fix. |
| Tests fail right after `pip install --upgrade` | A newer Home Assistant version changed behaviour. Go back to the pinned versions with `uv pip install -r requirements_test.txt`, then upgrade deliberately (below). |

## Upgrading to a newer Home Assistant

`pytest-homeassistant-custom-component` versions are tied to Home Assistant releases.

```bash
uv pip install --upgrade pytest-homeassistant-custom-component ruff
uv pip show pytest-homeassistant-custom-component homeassistant | grep -E "Name|Version"
pytest -q
```

If everything passes, update the two pinned versions in `requirements_test.txt` and the "Last verified" line, and commit. Do this when your Home Assistant at home is upgraded, so the tests run against the same version as your house.
