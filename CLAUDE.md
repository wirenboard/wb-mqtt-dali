# CLAUDE.md

Guidance for Claude Code in this repo.

## Project Overview

`wb-mqtt-dali` is a Python asyncio MQTT-DALI bridge for Wiren Board hardware. Connects DALI devices to an MQTT broker for control via MQTT topics and JSON-RPC.

## Environment Setup

Use Python 3.13.5 — matches the target platform.

```bash
./scripts/bootstrap-venv.sh
```

The script builds a self-contained `.venv` with Python 3.13.5 bundled inside
(`.venv/python/`), so the same `.venv` works both on the host and inside the
agent-vm (which bind-mount the project at the same absolute path but have
different `$HOME`). It is idempotent — safe to rerun to sync dependencies.

Always use tools from `.venv/bin/...`.

## Mandatory Verification Pipeline (after any code change)

```bash
.venv/bin/isort --profile black wb/ tests/ e2e/ bin/wb-mqtt-dali
.venv/bin/black wb/ tests/ e2e/ bin/wb-mqtt-dali
.venv/bin/pylint --rcfile=pyproject.toml wb/ tests/ e2e/ bin/wb-mqtt-dali
.venv/bin/pytest
```

## Project Rules & Code Style

The agent workflow rules (commits, tests, renames, temp vars, private-attribute access,
pylint scoping, …) and the code style (enums over constants, structures over dict soup,
class method ordering) live in @project-rules.md — the single source of truth, imported
here so it loads in every session. The `code-review-orchestrator` skill reviews against the same
file. Edit those rules there, not in this file.

## Task Workflow

Non-trivial changes follow plan → implement → review:

- **Plan** — `docs/<topic>_plan.md`. Written before implementation; intended approach and scope.
- **Review** — the `code-review-orchestrator` skill, run after implementation. Produces one in-chat report with a merge verdict; writes no file.

`<topic>` is a short snake_case slug. Agents that take a plan as input read the matching `docs/<topic>_plan.md` first.

## Architecture

Bridges DALI buses to MQTT. Each bus runs as an independent `ApplicationController`.

```
MQTT Broker  (user topics + RPC)
      ↕
MQTTDispatcher
      ↓
ApplicationController  (per bus, state machine)
  ↙            ↘
Commissioning   DevicePublisher
(discovery)     (MQTT publishing)
      ↓
DaliDevice / Dali2Device
      ↓
WBDALIDriver
      ↕   (MQTT RPC + topics on the same broker)
wb-mqtt-serial
      ↕
Physical DALI Bus (Modbus)
```

### Key Modules

- **`gateway.py`** — `WbDaliGateway`: manages buses, creates one `ApplicationController` per bus via `bus_from_json()`.
- **`application_controller.py`** — Per-bus state machine: `UNINITIALIZED → INITIALIZING → READY ↔ COMMISSIONING / IN_QUIESCENT_MODE`. Polling loop, state sync, RPC delegation.
- **`commissioning.py`** — Device discovery via binary search (`BinarySearchAddressFinder`). Produces `CommissioningResult` (new/missing/changed).
- **`wbdali.py`** — WB-DALI driver: command queuing and transport to `wb-mqtt-serial` via MQTT RPC + reply topics (no direct bus I/O).
- **`device_publisher.py`** — Publishes state to MQTT; runs the polling loop.
- **`mqtt_dispatcher.py`** — Routes incoming MQTT messages to handlers.
- **`mqtt_rpc_server.py`** — JSON-RPC over MQTT (commissioning, device control).
- **`common_dali_device.py` / `dali_device.py` / `dali2_device.py`** — Device models. `CommonDaliDevice` is the base; `Dali2Device` adds DALI 2 extended features.
- **`settings.py`** — Config parsed from `/etc/wb-mqtt-dali.conf`, validated against `wb-mqtt-dali.schema.json`.

### Device Types & Compat

- Parameter modules named `dali_type{N}_parameters.py` (50+ types). Type 8 (color control) has sub-modules: xy, TC, RGBWAF, primary-N. `gear/` holds feature modules (switching, dimming curve, thermal protection, demand response).
- `dali_compat.py` / `dali2_compat.py` wrap upstream `python-dali` (Wiren Board fork) to normalize DALI vs DALI 2 command APIs.

## Code Style & Notes

Code style and class method ordering are part of the project rules — see
@project-rules.md.
