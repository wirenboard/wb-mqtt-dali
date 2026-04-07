# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Branches

- **Main working branch:** `feature/PRJ-592`
- When squashing commits, use `git reset --soft feature/PRJ-592` as the base

## Project Overview

`wb-mqtt-dali` is a Python asyncio MQTT-DALI bridge for Wiren Board hardware. It connects DALI (Digital Addressable Lighting Interface) devices to an MQTT broker, enabling smart lighting control via MQTT topics and RPC commands.

## Commands

## Environment Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

- Always use tools from `.venv/bin/...`.

### Linting
```bash
.venv/bin/black --check wb/ tests/ e2e/ bin/wb-mqtt-dali
.venv/bin/isort --check-only --profile black wb/ tests/ e2e/ bin/wb-mqtt-dali
.venv/bin/pylint --rcfile=pyproject.toml wb/ tests/ e2e/ bin/wb-mqtt-dali

# Auto-fix formatting
.venv/bin/isort --profile black wb/ tests/ e2e/ bin/wb-mqtt-dali
.venv/bin/black wb/ tests/ e2e/ bin/wb-mqtt-dali
```

### Mandatory Verification Pipeline (after any code change)
```bash
.venv/bin/isort --profile black wb/ tests/ e2e/ bin/wb-mqtt-dali
.venv/bin/black wb/ tests/ e2e/ bin/wb-mqtt-dali
.venv/bin/pylint --rcfile=pyproject.toml wb/ tests/ e2e/ bin/wb-mqtt-dali
.venv/bin/pytest
```

- Do not commit if any command above fails.
- Never create a git commit without explicit user approval in the current conversation.
- Never modify existing tests without explicit user approval in the current conversation.

### Testing
```bash
.venv/bin/pytest
.venv/bin/pytest tests/test_foo.py
.venv/bin/pytest -k test_function_name
```

### Building
```bash
python setup.py build   # Build package
```

## Architecture

The system bridges DALI buses to MQTT. Each DALI bus runs as an independent `ApplicationController` instance.

### Data Flow
```
MQTT Broker ←→ MQTTDispatcher
                    ↓
            ApplicationController  (per bus, state machine)
              ↙            ↘
    Commissioning      DevicePublisher
    (discovery)        (MQTT publishing)
          ↓
    DaliDevice / Dali2Device
          ↓
    WBDALIDriver ←→ Physical DALI Bus (Modbus or WebSocket)
```

### Key Modules

- **`gateway.py`** – `WbDaliGateway` manages multiple DALI buses, creates one `ApplicationController` per bus via `bus_from_json()`.

- **`application_controller.py`** – Core per-bus state machine. States: `UNINITIALIZED → INITIALIZING → READY ↔ COMMISSIONING / IN_QUIESCENT_MODE`. Handles polling loop, state sync, and RPC delegation.

- **`commissioning.py`** – DALI device discovery using binary search (`BinarySearchAddressFinder`). Produces `CommissioningResult` tracking new/missing/changed devices.

- **`wbdali.py`** / **`wbmdali.py`** – Driver abstractions for new WB-DALI and legacy WB-MDALI hardware respectively. Provide command queuing and bus I/O.

- **`device_publisher.py`** – Publishes device state to MQTT topics. Runs the polling loop.

- **`mqtt_dispatcher.py`** – Routes incoming MQTT topic messages to registered handlers.

- **`mqtt_rpc_server.py`** – Handles JSON-RPC commands over MQTT (e.g., commissioning, device control).

- **`common_dali_device.py`** / **`dali_device.py`** / **`dali2_device.py`** – Device models. `CommonDaliDevice` is the base; `DaliDevice` handles DALI 1.0; `Dali2Device` adds DALI 2.0 extended features.

- **`settings.py`** – Configuration parameter management (parsed from `/etc/wb-mqtt-dali.conf`, validated against `wb-mqtt-dali.schema.json`).

### Device Type System

Parameter modules follow the naming pattern `dali_type{N}_parameters.py` (50+ types). Color control (Type 8) has sub-modules for xy, TC, RGBWAF, and primary-N color modes. `gear/` subpackage contains feature modules (switching, dimming curve, thermal protection, demand response).

### DALI Compat Layers

`dali_compat.py` and `dali2_compat.py` wrap the upstream `python-dali` library (Wiren Board fork) to normalize differences between DALI 1.0 and 2.0 command APIs.

## Code Style

- Code style and lint rules are defined in `pyproject.toml`.
- The canonical baseline for Python style config is:
  `https://github.com/wirenboard/codestyle/blob/master/python/config/pyproject.toml`

### Key Design Notes

- All I/O is `asyncio`-based. Tests use `unittest.IsolatedAsyncioTestCase`.
- Black/Pylint configured in `pyproject.toml`.
- Python 3.9+ required (`.python-version` pins 3.9.2).
- `doc/Internals.md` contains sequence diagrams for initialization, RPC rescanning, quiescent mode, and settings flow — read it before modifying `application_controller.py` or `commissioning.py`.
