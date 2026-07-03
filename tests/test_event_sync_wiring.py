"""ApplicationController wiring of the event-sync layer.

Own and foreign commands are both applied from bus observation (the monitor path): a
WB-sourced frame that reached the bus is applied there, once — the send-time replay (the
raw-command batch and ``/on`` control execution) was removed. On-topic confirm no longer
publishes the *value* of the setpoints event sync owns, only their write error. The
coordinator itself is replaced with a mock here — its behaviour is covered in
test_event_sync_coordinator.py.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.command import Response
from dali.frame import BackwardFrame, ForwardFrame
from dali.gear.general import DAPC, Off, QueryActualLevel

from wb.mqtt_dali.application_controller import (
    ApplicationControllerTask,
    ApplicationControllerTaskType,
)
from wb.mqtt_dali.bus_traffic import BusTrafficItem, BusTrafficSource
from wb.mqtt_dali.common_dali_device import ControlPollResult
from wb.mqtt_dali.control_ids import SET_RGB, WANTED_LEVEL
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.virtual_devices import GroupVirtualDevice
from wb.mqtt_dali.wbdali_error_response import WbGatewayTransmissionError

from ._app_controller_helpers import make_loop_controller, stop_loop


def _ff(command) -> ForwardFrame:
    return command.frame


def _monitor_controller():
    """Loop controller wired for ``_handle_bus_traffic_frame`` with a mocked coordinator."""
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller.logger = logging.getLogger("test.wiring")
    controller._dev_inst_map = None
    controller._last_bus_traffic_device_type = 0
    controller._bus_monitor_enabled = False
    controller._bus_monitor_syslog_enabled = False
    controller._mqtt_dispatcher = MagicMock()
    controller._one_shot_tasks = MagicMock()
    controller._event_sync = MagicMock()
    controller._event_sync.apply_commands = MagicMock(return_value=None)
    return controller


async def _run_confirm(controller, device_id: str, control_id: str, payload: str) -> None:
    """Drive one on-topic control write through the running polling loop (the confirm path)."""
    # pylint: disable=protected-access
    message = MagicMock()
    message.topic.value = f"/devices/{device_id}/controls/{control_id}/on"
    message.payload = payload.encode()
    loop_task = asyncio.create_task(controller._polling_loop())
    try:
        await asyncio.wait_for(controller._handle_on_topic(message), 1.0)
    finally:
        await stop_loop(controller, loop_task)


@pytest.mark.asyncio
async def test_send_command_batch_does_not_apply_at_send_time():
    """The raw-command batch no longer drives event sync: each frame that reaches the bus
    is applied from the monitor path instead (the send-time apply was removed)."""
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller._event_sync = MagicMock()
    controller._event_sync.apply_commands = AsyncMock()
    commands = [Off(5), DAPC(5, 100)]

    async def fake_send(_drv, command, *_a, **_kw):
        return command.response(None) if command.response is not None else Response(BackwardFrame(0))

    with patch("wb.mqtt_dali.application_controller.send_with_retry", side_effect=fake_send):
        await controller._send_command_batch_task(commands)

    controller._event_sync.apply_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_command_batch_stops_at_transport_error():
    """A transport error truncates the returned results at the failing command and, as
    before, drives no event sync from the send path."""
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller._event_sync = MagicMock()
    controller._event_sync.apply_commands = AsyncMock()
    commands = [Off(5), DAPC(5, 100)]

    async def fake_send(_drv, command, *_a, **_kw):
        if command is commands[1]:
            return WbGatewayTransmissionError()
        return Response(BackwardFrame(0))

    with patch("wb.mqtt_dali.application_controller.send_with_retry", side_effect=fake_send):
        results = await controller._send_command_batch_task(commands)

    assert len(results) == 2
    assert results[1].status.value == "error"
    controller._event_sync.apply_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_control_does_not_apply_at_send_time():
    """A successful /on control execution no longer replays its DALI commands into event
    sync at send time — the emitted frames are applied from the monitor path."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=0.01)
    controller._event_sync = MagicMock()
    controller._event_sync.apply_commands = AsyncMock()
    device = MagicMock()
    device.execute_control = AsyncMock(return_value=None)
    control = MagicMock()
    control.control_info.id = "wanted_level"
    control.value_to_set = "50"
    task = ApplicationControllerTask(ApplicationControllerTaskType.EXECUTE_CONTROL, (device, control))
    controller._tasks_queue.put_nowait(task)

    loop_task = asyncio.create_task(controller._polling_loop())
    try:
        await asyncio.wait_for(task.future, 1.0)
        await asyncio.sleep(0.01)
    finally:
        await stop_loop(controller, loop_task)

    device.execute_control.assert_awaited_once()
    controller._event_sync.apply_commands.assert_not_awaited()


def test_own_command_applied_via_monitor():
    """A WB-sourced frame that reached the bus (no transport error) is applied from the
    monitor path — own commands are applied there now, not at send time."""
    # pylint: disable=protected-access
    controller = _monitor_controller()

    item = BusTrafficItem(
        request=_ff(DAPC(5, 200)),
        response=Response(BackwardFrame(0)),
        request_source=BusTrafficSource.WB,
        frame_counter=1,
    )
    controller._handle_bus_traffic_frame(item)

    controller._event_sync.apply_commands.assert_called_once()
    applied = controller._event_sync.apply_commands.call_args.args[0]
    assert len(applied) == 1 and isinstance(applied[0], DAPC)


def test_own_command_transport_error_not_applied():
    """A WB-sourced frame that hit a transport error never reached the bus, so event sync
    does not apply it."""
    # pylint: disable=protected-access
    controller = _monitor_controller()

    item = BusTrafficItem(
        request=_ff(DAPC(5, 200)),
        response=WbGatewayTransmissionError(),
        request_source=BusTrafficSource.WB,
        frame_counter=1,
    )
    controller._handle_bus_traffic_frame(item)

    controller._event_sync.apply_commands.assert_not_called()


@pytest.mark.asyncio
async def test_own_command_applied_once():
    """An own command reaches event sync exactly once — from the monitor path. With the
    send-time replay removed, sending then observing the frame is a single apply."""
    # pylint: disable=protected-access
    controller = _monitor_controller()
    cmd = DAPC(5, 100)

    async def fake_send(_drv, _command, *_a, **_kw):
        return Response(BackwardFrame(0))

    with patch("wb.mqtt_dali.application_controller.send_with_retry", side_effect=fake_send):
        await controller._send_command_batch_task([cmd])
    controller._event_sync.apply_commands.assert_not_called()  # not at send time

    item = BusTrafficItem(
        request=_ff(cmd),
        response=Response(BackwardFrame(0)),
        request_source=BusTrafficSource.WB,
        frame_counter=1,
    )
    controller._handle_bus_traffic_frame(item)

    controller._event_sync.apply_commands.assert_called_once()  # applied once, from observation


def test_own_query_frame_not_applied():
    """Our own polling QUERY reaches the monitor as a WB frame too, but it carries no
    state effect (it declares a response class), so it must not schedule an event-sync
    apply — only send-only effect/DTR commands do."""
    # pylint: disable=protected-access
    controller = _monitor_controller()

    item = BusTrafficItem(
        request=_ff(QueryActualLevel(5)),
        response=Response(BackwardFrame(120)),
        request_source=BusTrafficSource.WB,
        frame_counter=1,
    )
    controller._handle_bus_traffic_frame(item)

    controller._event_sync.apply_commands.assert_not_called()


@pytest.mark.asyncio
async def test_setpoint_value_not_published_by_confirm():
    """After a successful setpoint write, confirm clears the write error but does not
    republish the value — event sync owns it (published from the observed truth)."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=0.01)
    device = MagicMock(spec=DaliDevice)
    device.mqtt_id = "dev-5"
    device.execute_control = AsyncMock(return_value=None)
    control = MagicMock()
    control.control_info.id = WANTED_LEVEL
    control.is_dirty.return_value = False
    device.get_mqtt_control.return_value = control
    controller._devices_by_mqtt_id = {"dev-5": device}

    await _run_confirm(controller, "dev-5", WANTED_LEVEL, "50")

    controller._device_publisher.set_control_error.assert_any_await("dev-5", "wanted_level", "")
    value_calls = [
        c
        for c in controller._device_publisher.set_control_value.await_args_list
        if c.args[:2] == ("dev-5", "wanted_level")
    ]
    assert value_calls == []


@pytest.mark.asyncio
async def test_setpoint_write_error_held_by_confirm():
    """A failed setpoint write still makes confirm publish the "w" write error."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=0.01)
    device = MagicMock(spec=DaliDevice)
    device.mqtt_id = "dev-5"
    device.execute_control = AsyncMock(side_effect=RuntimeError("bus down"))
    control = MagicMock()
    control.control_info.id = SET_RGB
    control.is_dirty.return_value = False
    device.get_mqtt_control.return_value = control
    controller._devices_by_mqtt_id = {"dev-5": device}

    await _run_confirm(controller, "dev-5", SET_RGB, "1;2;3")

    controller._device_publisher.set_control_error.assert_any_await("dev-5", "set_rgb", "w")


@pytest.mark.asyncio
async def test_owned_setpoint_echoed_by_confirm_for_virtual_device():
    """A virtual (group) device is not a DaliDevice, so the owned-setpoint suppression the
    confirm path applies to real gear does not fire: an owned setpoint write is still echoed
    to MQTT after a successful write (the isinstance guard's False branch)."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=0.01)
    device = MagicMock(spec=GroupVirtualDevice)
    device.mqtt_id = "grp-1"
    device.execute_control = AsyncMock(return_value=None)
    control = MagicMock()
    control.control_info.id = WANTED_LEVEL
    control.is_dirty.return_value = False
    device.get_mqtt_control.return_value = control
    controller._devices_by_mqtt_id = {"grp-1": device}

    await _run_confirm(controller, "grp-1", WANTED_LEVEL, "50")

    controller._device_publisher.set_control_value.assert_any_await("grp-1", "wanted_level", "50")


@pytest.mark.asyncio
async def test_publish_poll_results_flags_read_error_and_mirrors_setpoints():
    """_publish_poll_results driven directly with a failed + a successful readback: the
    failing control is flagged read_error and gets /meta/error=r, the successful one is
    cleared, and the setpoint mirror runs once against the *materialized* responses —
    passing a generator pins the ``responses = list(responses)`` fix (a spent iterator
    would silently drop the mirror)."""
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller._event_sync = MagicMock()
    controller._event_sync.publish_poll_setpoint_mirror = AsyncMock()

    fail_control = MagicMock()
    fail_control.read_error = False
    ok_control = MagicMock()
    ok_control.read_error = True
    controls = {"current_rgb": fail_control, "actual_level": ok_control}

    device = MagicMock(spec=DaliDevice)
    device.mqtt_id = "dev-5"
    device.name = "dev-5"
    device.groups = []
    device.get_mqtt_control.side_effect = controls.get

    responses = (
        result
        for result in [
            ControlPollResult(control_id="current_rgb", value=None, error="r"),
            ControlPollResult(control_id="actual_level", value="50.000"),
        ]
    )
    await controller._publish_poll_results(device, responses)

    assert fail_control.read_error is True
    assert ok_control.read_error is False
    controller._device_publisher.set_control_error.assert_any_await("dev-5", "current_rgb", "r")

    controller._event_sync.publish_poll_setpoint_mirror.assert_awaited_once()
    mirrored = controller._event_sync.publish_poll_setpoint_mirror.await_args.args[1]
    assert isinstance(mirrored, list) and len(mirrored) == 2


@pytest.mark.asyncio
async def test_nonowned_control_published_by_confirm():
    """A non-owned writable (a colour step pushbutton) still has its value published by
    confirm after a successful write."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=0.01)
    device = MagicMock(spec=DaliDevice)
    device.mqtt_id = "dev-5"
    device.execute_control = AsyncMock(return_value=None)
    control = MagicMock()
    control.control_info.id = "colour_temperature_step_warmer"
    control.is_dirty.return_value = False
    device.get_mqtt_control.return_value = control
    controller._devices_by_mqtt_id = {"dev-5": device}

    await _run_confirm(controller, "dev-5", "colour_temperature_step_warmer", "1")

    controller._device_publisher.set_control_value.assert_any_await(
        "dev-5", "colour_temperature_step_warmer", "1"
    )


def test_sniffed_foreign_frame_is_applied():
    """A BUS-sourced gear frame is forwarded to event sync; the decoded command is passed."""
    # pylint: disable=protected-access
    controller = _monitor_controller()

    item = BusTrafficItem(
        request=_ff(DAPC(5, 200)),
        response=None,
        request_source=BusTrafficSource.BUS,
        frame_counter=1,
    )
    controller._handle_bus_traffic_frame(item)

    # The sniffed apply is scheduled on the one-shot task set.
    assert controller._one_shot_tasks.add.called
    controller._event_sync.apply_commands.assert_called_once()
    applied = controller._event_sync.apply_commands.call_args.args[0]
    assert len(applied) == 1 and isinstance(applied[0], DAPC)
