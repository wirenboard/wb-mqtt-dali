# CLAUDE.md

Guidance for Claude Code in this repo.

## Project Overview

`wb-mqtt-dali` is a Python asyncio MQTT-DALI bridge for Wiren Board hardware. Connects DALI devices to an MQTT broker for control via MQTT topics and JSON-RPC.

## Environment Setup

Use Python 3.9 — same as CI (Debian bullseye). Newer interpreters mask bugs that only hit on 3.9.

```bash
./scripts/bootstrap-venv.sh
```

The script builds a self-contained `.venv` with Python 3.9 bundled inside
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

## Agent Workflow Rules

- Never create a git commit without explicit user approval in the current conversation.
- Never modify existing tests without explicit user approval.
- Do not rename existing identifiers (locals, params, functions, methods, classes, module-level constants) unless functionally required (old name became misleading after a behavior change, or a real name clash). Subjective "consistency"/"better naming" doesn't count. Expanding a signature does not justify renaming.
- Do not introduce temporary local variables for 1–2 uses; only if used 3+ times or they materially improve readability.
- Do not disable/skip tests; do not add `# pylint: disable` / `# noqa` / `# type: ignore` without a concrete reason. Fix the underlying issue.
- Never force-push (`--force` / `--force-with-lease`) to update a PR. Add new commits — reviewers need incremental changes.
- Tests must not **add new** access to private attributes (`_underscore`) of production classes. If a test can't be written against the public API, **stop and ask the user** — the fix usually requires widening the API or rethinking the test. Pre-existing private access in untouched test code is tolerated debt.
- `# pylint: disable=protected-access` must scope to a single function or line, never a whole module.

## Task Workflow

Non-trivial changes follow plan → implement → review, with artifacts in `doc/`:

- **Plan** — `doc/<topic>_plan.md`. Written before implementation; intended approach and scope.
- **Review** — `doc/<topic>_review.md`. Written after implementation by the code-reviewer agent; findings against plan and diff.

`<topic>` is a short snake_case slug. Agents that take a plan/review as input read the matching file from `doc/` first.

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

- Style/lint config: `pyproject.toml` (baseline: `https://github.com/wirenboard/codestyle/blob/master/python/config/pyproject.toml`).
- All I/O is `asyncio`; tests use `unittest.IsolatedAsyncioTestCase`.
- Non-trivial tests start with a short docstring describing the scenario being tested (what's set up, what's exercised, what's expected). Trivial one-liners (single assertion against a pure function) don't need it; anything with multi-step setup, async interactions, or non-obvious expectations does.
- **Enums over string/int constants**: when a value has a small, fixed set of options (status, kind, mode, action), model it with `enum.Enum` rather than string or integer literals. Plain `Enum` with descriptive values is the default; reach for `IntEnum`/`StrEnum`/`Flag` only when there's a concrete reason (interop, bitwise ops). Anti-pattern: a dataclass field typed `status: str` with conventional literals `"ok"`/`"error"` — make it a typed enum.
- **Structures over `dict`/`tuple` soup when the shape is known**: avoid `Optional[dict]`, `dict[str, list[str]]`, `tuple[tuple[str, tuple[str, ...]], ...]`, or several parallel dicts keyed by the same value — they hide what each string means and force readers to reverse-engineer the shape from assignment sites. If you know the keys and types, declare a `@dataclass` (frozen for immutable records, mutable for in-place state) or `NamedTuple` and use it as the field/parameter type. When several dicts share the same key set (`a[k]`, `b[k]`, `c[k]` always read together), that's a missing dataclass — collapse them into one `dict[Key, RecordType]`. Type aliases (`ControlId = str`) are cheap and worth using to document intent in signatures. Do not type a field `Optional[T]` defensively if every code path that constructs the parent already supplies a non-`None` value — narrow it to `T` so callers and the type-checker see the real contract.

### Class method ordering

Within every class body, methods are grouped in this order, with `# --- ... ---` dividers between groups (groups that are empty are omitted, dividers too):

1. `__init__` and other dunder methods.
2. Public methods and `@property`s — the class's external API.
3. `# --- Hooks for subclasses ---` — methods intended to be overridden (typically named with a leading underscore, e.g. `_initialize_impl`, `_build_mqtt_controls`).
4. `# --- Private ---` — internal helpers not intended for subclasses to override.

Within each group, order is by relevance/call sequence, not alphabetical.
