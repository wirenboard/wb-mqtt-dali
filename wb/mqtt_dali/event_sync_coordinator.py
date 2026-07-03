"""Event-driven state sync orchestrator.

Thin observer layer: turns commands seen on the bus (sniffed + our own) into
optimistic MQTT updates and confirmation polls. It decides *which* control a
command affects and asks that control to apply it; the control owns its typed
state and produces the published string. Per-device state lives on the devices;
the coordinator holds only the per-bus DTR snapshot and colour-sequence capture.
"""

import asyncio
import logging
from timeit import default_timer
from typing import Callable, NamedTuple, Optional

from dali.command import Command
from dali.gear.colour import (
    Activate,
    ColourTemperatureTcStepCooler,
    ColourTemperatureTcStepWarmer,
    SetTemporaryColourTemperature,
    SetTemporaryPrimaryNDimLevel,
    SetTemporaryRGBDimLevel,
    SetTemporaryRGBWAFControl,
    SetTemporaryWAFDimLevel,
    SetTemporaryXCoordinate,
    SetTemporaryYCoordinate,
    XCoordinateStepDown,
    XCoordinateStepUp,
    YCoordinateStepDown,
    YCoordinateStepUp,
)
from dali.gear.general import (
    DAPC,
    Down,
    GoToLastActiveLevel,
    GoToScene,
    Off,
    OnAndStepUp,
    RecallMaxLevel,
    RecallMinLevel,
    SetFadeRate,
    SetFadeTime,
    StepDown,
    StepDownAndOff,
    StepUp,
    Up,
)

from .colour_sequence_tracker import ColourSequenceTracker
from .common_dali_device import ControlPollResult, EventPollSchedule
from .control_ids import (
    ACTUAL_LEVEL,
    CURRENT_COLOUR_TEMPERATURE,
    CURRENT_PRIMARY_N,
    CURRENT_RGB,
    CURRENT_WHITE,
    CURRENT_X_COORDINATE,
    CURRENT_Y_COORDINATE,
)
from .control_ids import DAPC as DAPC_ID
from .control_ids import (
    PRIMARY_N_MAX,
    SET_COLOUR_TEMPERATURE,
    SET_PRIMARY_N,
    SET_RGB,
    SET_WHITE,
    SET_X_COORDINATE,
    SET_Y_COORDINATE,
    WANTED_LEVEL,
)
from .dali_controls import ActualLevelControl
from .dali_device import DaliDevice
from .dali_type7_parameters import LastActedControl
from .device_publisher import DevicePublisher
from .device_registry import DeviceRegistry
from .dtr_snapshot import DtrSnapshot
from .settle_clock import SettleBasis, SettleClock
from .virtual_devices import GroupStateUpdateKind, GroupVirtualDevice

_LEVEL_FADE = (DAPC, GoToScene, GoToLastActiveLevel)
_LEVEL_STEP_WINDOW = (Up, Down)
_LEVEL_IMMEDIATE = (Off, RecallMaxLevel, RecallMinLevel, StepUp, StepDown, StepDownAndOff, OnAndStepUp)
_LEVEL_COMMANDS = _LEVEL_FADE + _LEVEL_STEP_WINDOW + _LEVEL_IMMEDIATE

_COLOUR_SET_TEMPORARY = (
    SetTemporaryColourTemperature,
    SetTemporaryXCoordinate,
    SetTemporaryYCoordinate,
    SetTemporaryRGBDimLevel,
    SetTemporaryWAFDimLevel,
    SetTemporaryPrimaryNDimLevel,
    SetTemporaryRGBWAFControl,
)
_COLOUR_STEP = (
    XCoordinateStepUp,
    XCoordinateStepDown,
    YCoordinateStepUp,
    YCoordinateStepDown,
    ColourTemperatureTcStepCooler,
    ColourTemperatureTcStepWarmer,
)


class Publish(NamedTuple):
    """A predicted control value to publish to MQTT."""

    control_id: str
    value: str


# State<->setpoint pairing lives here (not on the controls): each colour ``current_*`` state
# is mirrored to its ``set_*`` setpoint; the level triplet is handled separately below.
_COLOUR_MIRROR = {
    CURRENT_RGB: SET_RGB,
    CURRENT_WHITE: SET_WHITE,
    CURRENT_COLOUR_TEMPERATURE: SET_COLOUR_TEMPERATURE,
    CURRENT_X_COORDINATE: SET_X_COORDINATE,
    CURRENT_Y_COORDINATE: SET_Y_COORDINATE,
    **{CURRENT_PRIMARY_N.format(i): SET_PRIMARY_N.format(i) for i in range(PRIMARY_N_MAX)},
}
_OWNED_SETPOINTS = frozenset({WANTED_LEVEL, DAPC_ID, *_COLOUR_MIRROR.values()})

# Reverse of the pairing above: each owned setpoint -> the state control it mirrors. Used
# to mirror a member's setpoint onto the group only when its paired state control is
# currently pinned to that same member (so the group's set_*/wanted_level/dapc track the
# member whose current_*/actual_level the group is showing).
_SETPOINT_STATE = {
    WANTED_LEVEL: ACTUAL_LEVEL,
    DAPC_ID: ACTUAL_LEVEL,
    **{set_id: current_id for current_id, set_id in _COLOUR_MIRROR.items()},
}


def is_event_sync_owned_setpoint(control_id: str) -> bool:
    """A writable setpoint event sync is the sole publisher of; on-topic confirm holds only
    its write error, never republishing the value (published from the observed truth)."""
    return control_id in _OWNED_SETPOINTS


def _setpoint_mirror_publishes(device: DaliDevice, control_id: str, value: str) -> list[Publish]:
    """Setpoint representations paired with a state control's observed (id, value).

    Only representations the device actually exposes are emitted; the clamp/round a real
    setpoint write would apply is a no-op for an observed value (or an accepted jump), so the
    string is mirrored verbatim -- except ``wanted_level``, an integer-% control (see below).
    """
    if control_id == ACTUAL_LEVEL:
        return _level_setpoint_publishes(device, value)
    set_id = _COLOUR_MIRROR.get(control_id)
    if set_id is not None and device.get_mqtt_control(set_id) is not None:
        return [Publish(set_id, value)]
    return []


def _level_setpoint_publishes(device: DaliDevice, value: str) -> list[Publish]:
    """The level triplet from actual_level's observed %: wanted_level, actual_level and dapc
    are three representations of one truth (the observed raw).

    ``actual_level`` (the passed ``value``) keeps its fractional ``:.3f`` %. ``wanted_level`` is
    an integer-only percent control, so it's published rounded. ``dapc`` carries the raw level.
    """
    publishes: list[Publish] = []
    if device.get_mqtt_control(WANTED_LEVEL) is not None:
        publishes.append(Publish(WANTED_LEVEL, str(round(float(value)))))
    actual = device.get_mqtt_control(ACTUAL_LEVEL)
    if (
        isinstance(actual, ActualLevelControl)
        and actual.current_level is not None
        and device.get_mqtt_control(DAPC_ID) is not None
    ):
        publishes.append(Publish(DAPC_ID, str(actual.current_level)))
    return publishes


def _level_settle_basis(command: Command) -> SettleBasis:
    if isinstance(command, _LEVEL_STEP_WINDOW):
        return SettleBasis.STEP_WINDOW
    if isinstance(command, _LEVEL_FADE):
        return SettleBasis.FADE
    return SettleBasis.IMMEDIATE


# Public entry points: apply_commands (observed commands) and publish_poll_setpoint_mirror
# (a re-sync poll's readback); everything else is internal dispatch.
class EventSyncCoordinator:  # pylint: disable=too-many-instance-attributes
    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        publisher: DevicePublisher,
        device_registry: DeviceRegistry,
        group_devices_by_number: dict[int, GroupVirtualDevice],
        logger: logging.Logger,
        settle_clock: Optional[SettleClock] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._publisher = publisher
        self._registry = device_registry
        self._group_devices = group_devices_by_number
        self._logger = logger
        self._settle = settle_clock if settle_clock is not None else SettleClock()
        self._now = now_fn if now_fn is not None else default_timer
        self._dtr = DtrSnapshot()
        self._colour = ColourSequenceTracker()

    async def apply_commands(self, commands: list[Command]) -> None:
        """Apply a burst of bus commands (sniffed or our own) to MQTT state + confirm polls."""
        for command in commands:
            try:
                await self._apply_one(command)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._logger.warning("Event sync failed for %s: %s", type(command).__name__, exc)

    async def publish_poll_setpoint_mirror(
        self, device: DaliDevice, results: list[ControlPollResult]
    ) -> None:
        """Publish the setpoints paired with a re-sync poll's state readbacks.

        The poll path already publishes the state controls (and drives /meta/error); this
        mirrors their setpoints from the same truth so wanted_level/dapc/set_* stay in sync
        even for commands prediction can't follow (Up/Down/GoToLastActiveLevel, colour steps).
        """
        publishes: list[Publish] = []
        for res in results:
            if res.error is not None or res.value is None:
                continue
            publishes.extend(_setpoint_mirror_publishes(device, res.control_id, res.value))
        await self._publish(device, publishes)

    # --- Private ---

    async def _apply_one(self, command: Command) -> None:
        register = self._dtr.record(command)
        if register is not None:
            self._colour.note_dtr(register)
            return
        if isinstance(command, SetFadeTime):
            for device in self._resolve(command):
                device.fade_param.set_fade_time(self._dtr.dtr0)
            return
        if isinstance(command, SetFadeRate):
            return  # fade_rate isn't tracked: only fade_time feeds the settle timing
        if isinstance(command, _COLOUR_SET_TEMPORARY):
            for device in self._resolve(command):
                if device.dt8_handler is not None:
                    self._colour.record(device.uid, command, self._dtr)
            return
        if isinstance(command, Activate):
            await self._apply_activate(command)
            return
        if isinstance(command, _COLOUR_STEP):
            for device in self._resolve(command):
                if device.dt8_handler is not None:
                    self._schedule_confirm(device, [device.dt8_handler], SettleBasis.FADE)
            return
        if isinstance(command, _LEVEL_COMMANDS):
            await self._apply_level(command)

    async def _apply_level(self, command: Command) -> None:
        basis = _level_settle_basis(command)
        for device in self._resolve(command):
            actual = device.get_mqtt_control(ACTUAL_LEVEL)
            if not isinstance(actual, ActualLevelControl):
                continue
            prev_level = actual.current_level
            level_value = actual.apply(command)
            new_level = actual.current_level

            publishes: list[Publish] = []
            # Suppress every level representation while the level's read poll is failing:
            # publishing would clear /meta/error=r (Device.set_control_value side effect).
            if level_value is not None and not actual.read_error:
                publishes.append(Publish(ACTUAL_LEVEL, level_value))
                publishes.extend(_setpoint_mirror_publishes(device, ACTUAL_LEVEL, level_value))

            pollables: list[EventPollSchedule] = [actual]

            last_acted = device.get_mqtt_control("last_acted")
            if isinstance(last_acted, LastActedControl):
                pollables.append(last_acted)
                acted_value = last_acted.apply(prev_level, new_level)
                if acted_value is not None:
                    publishes.append(Publish("last_acted", acted_value))

            if isinstance(command, GoToScene) and device.dt8_handler is not None:
                pollables.append(device.dt8_handler)
                publishes.extend(self._scene_colour_publishes(device, command.param))

            await self._publish(device, publishes)
            self._schedule_confirm(device, pollables, basis)

    async def _apply_activate(self, command: Command) -> None:
        for device in self._resolve(command):
            handler = device.dt8_handler
            capture = self._colour.take(device.uid)
            publishes: list[Publish] = []
            if handler is not None and capture is not None and capture.predictable and capture.components:
                publishes = self._colour_publishes(device, handler.apply_colour(capture.components))
            await self._publish(device, publishes)
            if handler is not None:
                self._schedule_confirm(device, [handler], SettleBasis.FADE)
        self._colour.end_activate()

    def _scene_colour_publishes(self, device: DaliDevice, scene_index: int) -> list[Publish]:
        handler = device.dt8_handler
        if handler is None or handler.scenes_settings is None:
            return []
        colour = handler.scenes_settings.scene_colour(scene_index)
        if colour is None:
            return []
        return self._colour_publishes(device, handler.apply_scene_colour(colour))

    def _colour_publishes(self, device: DaliDevice, results: list[ControlPollResult]) -> list[Publish]:
        """Turn a colour handler's current_* results into current_* + paired set_* publishes.

        Skips a component whose current_* state control is in read error (same suppression
        as level), so a standing /meta/error=r isn't cleared by an optimistic publish.
        """
        publishes: list[Publish] = []
        for res in results:
            if res.value is None:
                continue
            control = device.get_mqtt_control(res.control_id)
            if control is not None and control.read_error:
                continue
            publishes.append(Publish(res.control_id, res.value))
            publishes.extend(_setpoint_mirror_publishes(device, res.control_id, res.value))
        return publishes

    def _resolve(self, command: Command) -> list[DaliDevice]:
        return self._registry.resolve(command.destination)

    def _schedule_confirm(
        self, device: DaliDevice, pollables: list[EventPollSchedule], basis: SettleBasis
    ) -> None:
        settle = self._settle.settle_for(basis, device.fade_param.fade_time)
        now = self._now()
        at = now + settle
        for pollable in pollables:
            # The latest command's settle wins: an immediate-then-fade burst confirms
            # after the fade, not on the earlier command's short window.
            pollable.schedule_confirmation(now, at)

    async def _publish(self, device: DaliDevice, publishes: list[Publish]) -> None:
        if not publishes:
            return
        tasks = []
        for publish in publishes:
            tasks.append(self._publisher.set_control_value(device.mqtt_id, publish.control_id, publish.value))
            tasks.extend(self._group_mirror_tasks(device, publish.control_id, publish.value))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                self._logger.error("Event sync publish failed for %s: %s", device.name, result)

    def _group_mirror_tasks(self, device: DaliDevice, control_id: str, value: str) -> list:
        tasks: list = []
        for group_number in device.groups:
            group_device = self._group_devices.get(group_number)
            if group_device is None:
                continue
            task = self._group_mirror_task(group_device, device, control_id, value)
            if task is not None:
                tasks.append(task)
        return tasks

    def _group_mirror_task(
        self, group_device: GroupVirtualDevice, device: DaliDevice, control_id: str, value: str
    ):
        source = group_device.state_source
        if control_id in source.control_ids:
            update = source.record_poll(
                candidate_uid=device.uid, control_id=control_id, success=True, value=value
            )
            if update is not None and update.kind is GroupStateUpdateKind.VALUE:
                return self._publisher.set_control_value(
                    group_device.mqtt_id, update.control_id, update.payload
                )
            return None
        # Owned setpoint: mirror it onto the group only while its paired state control is
        # pinned to this same member, so the group's setpoint stays consistent with the
        # member whose state the group is currently showing.
        state_id = _SETPOINT_STATE.get(control_id)
        if (
            state_id is not None
            and source.pinned_source(state_id) == device.uid
            and group_device.get_mqtt_control(control_id) is not None
        ):
            return self._publisher.set_control_value(group_device.mqtt_id, control_id, value)
        return None
