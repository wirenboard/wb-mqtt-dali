"""Turns bus frames (sniffed or our own) into ``LevelChanged``/``ColourChanged`` events and
dispatches them -- with those from the poll path and the DALI-2 input frames -- via ``notify``
to every control of the affected device and to the groups it belongs to. Which event a command
produces and who gets it is decided here; the reacting is the controls' own.

Per-bus state lives here (DTR snapshot, colour-sequence capture); per-device state lives on
the devices -- their controls and their type handlers.
"""

import asyncio
import logging
from timeit import default_timer
from typing import Callable, Optional, Union

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
from .common_dali_device import DaliDeviceBase, MqttControlBase, Pollable
from .control_ids import ACTUAL_LEVEL, LAST_ACTED
from .dali_controls import ActualLevelControl
from .dali_device import DaliDevice
from .device_publisher import DevicePublisher
from .device_registry import DeviceRegistry
from .dtr_snapshot import DtrSnapshot
from .events import BusEvent, Dali2InputEvent, EventSource, LevelChanged
from .settle_clock import SettleBasis, SettleClock
from .virtual_devices import GroupVirtualDevice

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

# The level controls that read themselves, so they are the ones owed a confirming poll when
# a command produced no event to react to.
_LEVEL_EVENT_CONTROLS = (ACTUAL_LEVEL, LAST_ACTED)


def _level_settle_basis(command: Command) -> SettleBasis:
    if isinstance(command, _LEVEL_STEP_WINDOW):
        return SettleBasis.STEP_WINDOW
    if isinstance(command, _LEVEL_FADE):
        return SettleBasis.FADE
    return SettleBasis.IMMEDIATE


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
        """Apply a burst of bus commands (sniffed or our own) as events + confirm polls."""
        for command in commands:
            try:
                await self._apply_one(command)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self._logger.warning("Event sync failed for %s: %s", type(command).__name__, exc)

    async def notify_poll_event(self, device: DaliDevice, event: BusEvent) -> None:
        """Dispatch a poll outcome -- a confirmed read (``source=READ``) or a read failure."""
        await self._dispatch(device, event)

    async def notify_dali2_event(self, device: DaliDeviceBase, event: Dali2InputEvent) -> None:
        """Dispatch a DALI-2 input frame; an event whose instance has no control changes nothing."""
        await self._dispatch(device, event)

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
                    device.dt8_handler.schedule_poll_at(self._settle_at(SettleBasis.FADE, device))
            return
        if isinstance(command, _LEVEL_COMMANDS):
            await self._apply_level(command)

    async def _apply_level(self, command: Command) -> None:
        basis = _level_settle_basis(command)
        for device in self._resolve(command):
            settle_at = self._settle_at(basis, device)
            actual = device.get_mqtt_control(ACTUAL_LEVEL)
            if isinstance(actual, ActualLevelControl):
                predicted = actual.predict_level(command)
                if predicted is None:
                    # Not predictable (Up/Down/GoToLastActiveLevel/unbounded Recall): with no
                    # event to react to, the confirming poll the controls would have
                    # scheduled themselves is asked for directly.
                    self._schedule_level_confirmations(device, settle_at)
                else:
                    await self._dispatch(device, LevelChanged(predicted, EventSource.OBSERVED, settle_at))
            # A scene sets colour as well as level, whatever the level side made of it.
            if isinstance(command, GoToScene) and device.dt8_handler is not None:
                await self._apply_scene_colour(device, command.param, settle_at)

    async def _apply_scene_colour(self, device: DaliDevice, scene_index: int, settle_at: float) -> None:
        handler = device.dt8_handler
        handler.schedule_poll_at(settle_at)
        event = handler.apply_observed_colour(handler.scene_colour_components(scene_index), settle_at)
        if event is not None:
            await self._dispatch(device, event)

    async def _apply_activate(self, command: Command) -> None:
        try:
            for device in self._resolve(command):
                handler = device.dt8_handler
                capture = self._colour.take(device.uid)
                if handler is None:
                    continue
                settle_at = self._settle_at(SettleBasis.FADE, device)
                if capture is not None and capture.predictable and capture.components:
                    event = handler.apply_observed_colour(capture.components, settle_at)
                    if event is not None:
                        await self._dispatch(device, event)
                handler.schedule_poll_at(settle_at)
        finally:
            # Must run even if a device's dispatch blew up: a kept _fresh_dtr would let the
            # aborted transaction's registers feed the next colour sequence.
            self._colour.end_activate()

    def _schedule_level_confirmations(self, device: DaliDevice, settle_at: float) -> None:
        for control_id in _LEVEL_EVENT_CONTROLS:
            control = device.get_mqtt_control(control_id)
            if control is not None and isinstance(control, Pollable):
                control.schedule_poll_at(settle_at)

    def _settle_at(self, basis: SettleBasis, device: DaliDevice) -> float:
        """When the bus should have reached the state the observed command commands."""
        return self._now() + self._settle.settle_for(basis, device.fade_param.fade_time)

    def _resolve(self, command: Command) -> list[DaliDevice]:
        return self._registry.resolve(command.destination)

    async def _dispatch(self, device: DaliDeviceBase, event: BusEvent) -> None:
        changed = device.notify_all(event)
        if not changed:
            return
        tasks = self._publish_tasks(device, changed)
        if isinstance(device, DaliDevice):
            for group_device in self._groups_of(device):
                tasks.extend(self._publish_tasks(group_device, group_device.notify_all(event, device)))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                self._logger.error("Event dispatch publish failed for %s: %s", device.name, result)

    def _publish_tasks(
        self, device: Union[DaliDeviceBase, GroupVirtualDevice], controls: list[MqttControlBase]
    ) -> list:
        tasks = []
        for control in controls:
            # Read the state out now, not when the publish coroutine finally runs: a burst of
            # frames can rewrite the live state across the await below.
            state = control.control_info.state
            tasks.append(
                self._publisher.publish_control_state(
                    device.mqtt_id, control.control_info.id, state.value, state.error, state.meta.title
                )
            )
        return tasks

    def _groups_of(self, device: DaliDevice) -> list[GroupVirtualDevice]:
        groups = [self._group_devices.get(number) for number in device.groups]
        return [group for group in groups if group is not None]
