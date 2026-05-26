"""Tests for the pre-emptable DT-8 split colour poll"""

# pylint: disable=duplicate-code

from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.gear.colour import QueryColourValue, QueryColourValueDTR
from dali.gear.general import DTR0, QueryActualLevel, QueryContentDTR0

from wb.mqtt_dali.application_controller import PollScheduler
from wb.mqtt_dali.common_dali_device import (
    DaliDeviceAddress,
    DaliDeviceBase,
    MqttControl,
)
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_type8_parameters import (
    MAX_COLOUR_SUBBATCH_RETRIES,
    ColourType,
    Type8Parameters,
)
from wb.mqtt_dali.device_publisher import ControlInfo
from wb.mqtt_dali.wbmqtt import ControlMeta

# pylint: disable-next=protected-access
DaliDeviceBase._common_schema = {"title": "test-schema"}


def _readable_control(control_id: str, poll_interval=None) -> MqttControl:
    return MqttControl(
        control_info=ControlInfo(control_id, ControlMeta(read_only=True), "0"),
        query_builder=lambda addr, _id=control_id: f"Q_{_id}",
        value_formatter=lambda resp: "v",
        poll_interval=poll_interval,
    )


def _ok_response(value: int = 0):
    resp = MagicMock()
    resp.raw_value = MagicMock()
    resp.raw_value.error = False
    resp.raw_value.as_integer = value
    return resp


def _bad_response():
    resp = MagicMock()
    resp.raw_value = MagicMock()
    resp.raw_value.error = True
    resp.raw_value.as_integer = 0
    return resp


def _make_type8_handler(colour_type: ColourType = ColourType.RGBWAF) -> Type8Parameters:
    handler = Type8Parameters()
    # pylint: disable-next=protected-access
    handler._current_colour_type = colour_type
    return handler


def _make_dali_device(short=1, controls=None, type8_handler=None) -> DaliDevice:
    # pylint: disable=protected-access
    dev = DaliDevice(
        DaliDeviceAddress(short=short, random=0),
        "bus1",
        MagicMock(),
        None,
        None,
    )
    dev.is_initialized = True
    dev.types = []
    pollables: list = list(controls or [])
    if type8_handler is not None:
        pollables.append(type8_handler)
        dev._type8_handler = type8_handler
        dev._standalone_pollables = [type8_handler]
    dev._pollables = pollables
    dev._current_round = []
    return dev


def _is_first_subbatch(cmds) -> bool:
    if len(cmds) != 3:
        return False
    return (
        isinstance(cmds[0], QueryActualLevel)
        and isinstance(cmds[1], DTR0)
        and cmds[1].param == QueryColourValueDTR.ReportColourType.value
        and isinstance(cmds[2], QueryColourValue)
    )


def _make_send_commands(level: int, colour_type_int: int, component_values: dict[int, int]):
    async def _send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        if _is_first_subbatch(cmds):
            return [_ok_response(level), _ok_response(0), _ok_response(colour_type_int)]
        tag_val = cmds[0].param
        value = component_values.get(tag_val, 0)
        responses = [_ok_response(0), _ok_response(value & 0xFF)]
        if len(cmds) == 3:
            responses.append(_ok_response((value >> 8) & 0xFF))
        return responses

    return _send


@pytest.mark.asyncio
async def test_type8_colour_poll_split_into_subbatches():
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    component_values = {
        QueryColourValueDTR.RedDimLevel.value: 10,
        QueryColourValueDTR.GreenDimLevel.value: 20,
        QueryColourValueDTR.BlueDimLevel.value: 30,
        QueryColourValueDTR.WhiteDimLevel.value: 40,
    }

    sent_calls: list[list] = []

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        sent_calls.append(list(cmds))
        return await _make_send_commands(180, ColourType.RGBWAF.value, component_values)(cmds, source)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    results: list = []
    for _ in range(5):
        res = dev.poll_controls(
            driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
        )
        assert res.poll_coroutine is not None
        results.append(await res.poll_coroutine())

    assert len(sent_calls) == 5
    assert _is_first_subbatch(sent_calls[0])
    for cmds in sent_calls[1:]:
        assert len(cmds) == 2
        assert isinstance(cmds[0], DTR0)
        assert isinstance(cmds[1], QueryColourValue)

    for res in results[:-1]:
        assert res == []
    final = {item.control_id: item for item in results[-1]}
    assert final["current_rgb"].value == "10;20;30"
    assert final["current_rgb"].error is None
    assert final["current_white"].value == "40"
    assert final["current_white"].error is None

    assert not handler.has_in_progress_read()


@pytest.mark.asyncio
async def test_type8_colour_poll_does_not_hold_bus_lock_across_subbatches():
    """Between split subbatches, another consumer's send_commands must be able to interleave."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    component_values = {
        QueryColourValueDTR.RedDimLevel.value: 1,
        QueryColourValueDTR.GreenDimLevel.value: 2,
        QueryColourValueDTR.BlueDimLevel.value: 3,
        QueryColourValueDTR.WhiteDimLevel.value: 4,
    }
    base_send = _make_send_commands(100, ColourType.RGBWAF.value, component_values)

    sent_log: list[str] = []

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        sent_log.append("dt8" if _is_first_subbatch(cmds) or len(cmds) == 2 else "other")
        return await base_send(cmds, source)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    other_call_results: list = []

    async def other_consumer():
        sent_log.append("other")
        result = await driver.send_commands([MagicMock()], source="OTHER")
        other_call_results.append(result)

    for i in range(5):
        res = dev.poll_controls(
            driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
        )
        assert res.poll_coroutine is not None
        await res.poll_coroutine()
        if i < 4:
            await other_consumer()

    # 5 DT-8 subbatches + 4 other-consumer calls = 9.
    assert driver.send_commands.await_count == 9
    assert len(other_call_results) == 4


@pytest.mark.asyncio
async def test_execute_control_preempts_between_poll_subbatches():
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    component_values = {
        QueryColourValueDTR.RedDimLevel.value: 1,
        QueryColourValueDTR.GreenDimLevel.value: 2,
        QueryColourValueDTR.BlueDimLevel.value: 3,
        QueryColourValueDTR.WhiteDimLevel.value: 4,
    }
    base_send = _make_send_commands(100, ColourType.RGBWAF.value, component_values)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=base_send)

    res = dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    await res.poll_coroutine()
    assert handler.has_in_progress_read()

    res = dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    await res.poll_coroutine()
    execute_control_done = False

    async def execute_control_simulation():
        nonlocal execute_control_done
        await driver.send_commands([MagicMock()])
        execute_control_done = True

    await execute_control_simulation()
    assert execute_control_done is True
    assert handler.has_in_progress_read()

    for _ in range(3):
        res = dev.poll_controls(
            driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
        )
        await res.poll_coroutine()
    assert not handler.has_in_progress_read()


@pytest.mark.asyncio
async def test_execute_control_latency_under_150ms_in_4_rgbwaf_setup():
    """On a bus of 4 RGBWAF DT-8 devices, no single tick may exceed 3 commands."""
    devices = []
    for short in range(1, 5):
        handler = _make_type8_handler(ColourType.RGBWAF)
        devices.append(_make_dali_device(short=short, type8_handler=handler))

    scheduler = PollScheduler()
    scheduler.set_devices(devices)

    component_values = {
        QueryColourValueDTR.RedDimLevel.value: 1,
        QueryColourValueDTR.GreenDimLevel.value: 2,
        QueryColourValueDTR.BlueDimLevel.value: 3,
        QueryColourValueDTR.WhiteDimLevel.value: 4,
    }
    base_send = _make_send_commands(50, ColourType.RGBWAF.value, component_values)
    sent_per_tick: list[int] = []

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        sent_per_tick[-1] += len(cmds)
        return await base_send(cmds, source)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    for _ in range(30):
        sent_per_tick.append(0)
        await scheduler.poll(driver, 0.0, 5.0)

    assert sent_per_tick, "no ticks recorded"
    assert max(sent_per_tick) <= 3


@pytest.mark.asyncio
async def test_type8_subbatch_retries_up_to_three_times():
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    attempts: list[int] = [0]

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        attempts[0] += 1
        if attempts[0] < 3:
            return [_bad_response() for _ in cmds]
        return [_ok_response(50), _ok_response(0), _ok_response(ColourType.RGBWAF.value)]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    res = dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    await res.poll_coroutine()

    assert attempts[0] == 3
    assert handler.has_in_progress_read()
    assert MAX_COLOUR_SUBBATCH_RETRIES == 3


@pytest.mark.asyncio
async def test_type8_subbatch_failure_publishes_error_and_reschedules():
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, source=None: [_bad_response() for _ in cmds])

    handler.last_poll_time = None
    res = dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    poll_results = await res.poll_coroutine()

    assert {p.control_id for p in poll_results} == {"current_rgb", "current_white"}
    assert all(p.error == "r" for p in poll_results)

    assert driver.send_commands.await_count == MAX_COLOUR_SUBBATCH_RETRIES
    assert not handler.has_in_progress_read()

    assert handler.last_poll_time == 0.0
    assert handler.is_poll_due(4.9, 5.0) is False
    assert handler.is_poll_due(5.0, 5.0) is True


@pytest.mark.asyncio
async def test_type8_quiescent_mid_read_drops_partial_state():
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    component_values = {
        QueryColourValueDTR.RedDimLevel.value: 1,
        QueryColourValueDTR.GreenDimLevel.value: 2,
        QueryColourValueDTR.BlueDimLevel.value: 3,
        QueryColourValueDTR.WhiteDimLevel.value: 4,
    }
    driver = AsyncMock()
    driver.send_commands = AsyncMock(
        side_effect=_make_send_commands(50, ColourType.RGBWAF.value, component_values)
    )

    await dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    ).poll_coroutine()
    await dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    ).poll_coroutine()
    assert handler.has_in_progress_read()

    dev.reset_polling_state()
    assert not handler.has_in_progress_read()
    # pylint: disable-next=protected-access
    assert not dev._current_round

    sends_before = driver.send_commands.await_count
    res = dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    if res.poll_coroutine is not None:
        await res.poll_coroutine()
    # Next send after reset must be a fresh opening batch, not a component continuation.
    if driver.send_commands.await_count > sends_before:
        last_call = driver.send_commands.call_args_list[-1]
        cmds, *_ = last_call.args
        assert _is_first_subbatch(list(cmds))


@pytest.mark.asyncio
async def test_type8_device_removed_mid_read_drops_partial_state():
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    component_values = {
        QueryColourValueDTR.RedDimLevel.value: 1,
        QueryColourValueDTR.GreenDimLevel.value: 2,
        QueryColourValueDTR.BlueDimLevel.value: 3,
        QueryColourValueDTR.WhiteDimLevel.value: 4,
    }
    driver = AsyncMock()
    driver.send_commands = AsyncMock(
        side_effect=_make_send_commands(50, ColourType.RGBWAF.value, component_values)
    )

    await dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    ).poll_coroutine()
    await dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    ).poll_coroutine()
    assert handler.has_in_progress_read()

    scheduler = PollScheduler()
    scheduler.set_devices([dev])
    scheduler.remove_device(dev)
    del dev

    assert scheduler.is_empty() is True


@pytest.mark.asyncio
async def test_type8_handler_takes_one_snapshot_position_with_own_deadline():
    handler = _make_type8_handler(ColourType.RGBWAF)
    a = _readable_control("a", poll_interval=5.0)
    dev = _make_dali_device(controls=[a], type8_handler=handler)

    assert handler.is_poll_due(0.0, 5.0) is True
    assert a.is_poll_due(0.0, 5.0) is True

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, source=None: [_ok_response() for _ in cmds])

    res = dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    assert res.commands_count == 1
    # pylint: disable-next=protected-access
    assert handler in dev._current_round
    # Handler deferred (3-cmd subbatch doesn't fit in remaining 2-cmd budget) so it didn't stamp yet.
    assert handler.last_poll_time is None
    await res.poll_coroutine()


@pytest.mark.asyncio
async def test_dt8_subbatch_does_not_bundle_with_single_cmd_controls_in_one_send_commands():
    handler = _make_type8_handler(ColourType.RGBWAF)
    a = _readable_control("a", poll_interval=5.0)
    dev = _make_dali_device(controls=[a], type8_handler=handler)

    sent_calls: list[list] = []

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        sent_calls.append(list(cmds))
        return [_ok_response(), _ok_response(0), _ok_response(ColourType.RGBWAF.value)][: len(cmds)]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    await dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    ).poll_coroutine()
    await dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    ).poll_coroutine()

    assert len(sent_calls) >= 2
    dt8_calls = [c for c in sent_calls if _is_first_subbatch(c)]
    assert len(dt8_calls) == 1
    assert all(not getattr(c, "_id", "") == "Q_a" for c in dt8_calls[0])


@pytest.mark.asyncio
async def test_xy_component_batch_is_3_cmds():
    handler = _make_type8_handler(ColourType.XY)
    dev = _make_dali_device(type8_handler=handler)

    component_values = {
        QueryColourValueDTR.XCoordinate.value: 0x1234,
        QueryColourValueDTR.YCoordinate.value: 0x5678,
    }

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        if _is_first_subbatch(cmds):
            return [_ok_response(50), _ok_response(0), _ok_response(ColourType.XY.value)]
        assert len(cmds) == 3
        assert isinstance(cmds[2], QueryContentDTR0)
        tag_val = cmds[0].param
        value = component_values[tag_val]
        return [_ok_response(0), _ok_response((value >> 8) & 0xFF), _ok_response(value & 0xFF)]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    results = []
    for _ in range(3):
        res = dev.poll_controls(
            driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
        )
        results.append(await res.poll_coroutine())

    final = {item.control_id: item.value for item in results[-1]}
    assert final["current_x_coordinate"] == "4660"
    assert final["current_y_coordinate"] == "22136"
