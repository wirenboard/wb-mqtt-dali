"""Tests for the per-control polling scheduler."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from wb.mqtt_dali.application_controller import PollScheduler
from wb.mqtt_dali.common_dali_device import (
    DaliDeviceAddress,
    DaliDeviceBase,
    MqttControl,
)
from wb.mqtt_dali.dali_controls import ErrorStatusControl
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_type8_parameters import ColourType, Type8Parameters
from wb.mqtt_dali.dali_type16_parameters import Type16Parameters
from wb.mqtt_dali.dali_type21_parameters import Type21Parameters
from wb.mqtt_dali.device_publisher import ControlInfo
from wb.mqtt_dali.wbmqtt import ControlMeta

# Avoid filesystem reads in DaliDeviceBase.__init__.
# pylint: disable-next=protected-access
DaliDeviceBase._common_schema = {"title": "test-schema"}


def _readable_control(control_id: str, poll_interval=None) -> MqttControl:
    return MqttControl(
        control_info=ControlInfo(control_id, ControlMeta(read_only=True), "0"),
        query_builder=lambda addr, _id=control_id: f"Q_{_id}",
        value_formatter=lambda resp: "v",
        poll_interval=poll_interval,
    )


def _make_dali_device(short=1, mqtt_id=None, controls=None, type8_handler=None) -> DaliDevice:
    # pylint: disable=protected-access
    dev = DaliDevice(
        DaliDeviceAddress(short=short, random=0),
        "bus1",
        MagicMock(),
        mqtt_id,
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


def _ok_response():
    resp = MagicMock()
    resp.raw_value = MagicMock()
    resp.raw_value.error = False
    return resp


def test_control_with_explicit_interval_polled_at_its_rate():
    fast = _readable_control("fast")
    slow = _readable_control("slow", poll_interval=10.0)

    bus_default = 5.0

    assert fast.is_poll_due(0.0, bus_default)
    assert slow.is_poll_due(0.0, bus_default)

    fast.last_poll_time = 0.0
    slow.last_poll_time = 0.0

    assert fast.is_poll_due(5.5, bus_default)
    assert not slow.is_poll_due(5.5, bus_default)

    assert slow.is_poll_due(10.0, bus_default)


def test_control_without_interval_falls_back_to_bus_default():
    inheriting = _readable_control("inh")
    overriding = _readable_control("ovr", poll_interval=100.0)

    inheriting.last_poll_time = 0.0
    overriding.last_poll_time = 0.0

    assert inheriting.is_poll_due(5.0, 5.0)
    assert not overriding.is_poll_due(5.0, 5.0)

    assert not inheriting.is_poll_due(5.0, 30.0)
    assert not overriding.is_poll_due(5.0, 30.0)
    assert overriding.is_poll_due(100.0, 30.0)


def test_alarm_controls_use_120s_interval():
    err = ErrorStatusControl()
    type21 = Type21Parameters().get_mqtt_controls()[0]
    type16 = Type16Parameters().get_mqtt_controls()[0]

    assert err.poll_interval == 120.0
    assert type21.poll_interval == 120.0
    assert type16.poll_interval == 120.0

    for ctrl in (err, type21, type16):
        ctrl.last_poll_time = 0.0
        assert not ctrl.is_poll_due(5.0, 5.0)
        assert ctrl.is_poll_due(120.0, 5.0)


@pytest.mark.asyncio
async def test_first_poll_after_init_happens_immediately():
    controls = [_readable_control(f"c{i}") for i in range(5)]
    dev = _make_dali_device(controls=controls)

    for c in controls:
        assert c.is_poll_due(0.0, 5.0)

    res1 = dev.poll_controls(
        MagicMock(), now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    assert res1.commands_count == 3
    assert res1.has_more is True

    res2 = dev.poll_controls(
        MagicMock(), now=0.001, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    assert res2.commands_count == 2
    assert res2.has_more is False


@pytest.mark.asyncio
async def test_polling_tick_budget_capped_at_three_commands():
    controls_a = [_readable_control(f"a{i}") for i in range(3)]
    controls_b = [_readable_control(f"b{i}") for i in range(3)]
    dev_a = _make_dali_device(short=1, controls=controls_a)
    dev_b = _make_dali_device(short=2, controls=controls_b)

    scheduler = PollScheduler()
    scheduler.set_devices([dev_a, dev_b])

    sent_per_call = []

    async def fake_send(cmds, _source=None, priority=None):
        del priority
        sent_per_call.append(list(cmds))
        return [_ok_response() for _ in cmds]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    await scheduler.poll(driver, 0.0, 5.0)

    total = sum(len(call) for call in sent_per_call)
    assert total <= 3


@pytest.mark.asyncio
async def test_round_snapshot_excludes_controls_matured_mid_round():
    fast = _readable_control("fast", poll_interval=1.0)
    slow = _readable_control("slow", poll_interval=100.0)
    fillers = [_readable_control(f"f{i}", poll_interval=100.0) for i in range(3)]
    dev = _make_dali_device(controls=[fast, slow, *fillers])

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, _src: [_ok_response() for _ in cmds])

    res1 = dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    await res1.poll_coroutine()
    # pylint: disable-next=protected-access
    assert dev._current_round == [fillers[1], fillers[2]]
    # `fast` matured mid-round (t=2 ≥ 0+1) but snapshot must not pick it up.
    # pylint: disable-next=protected-access
    assert fast not in dev._current_round

    res2 = dev.poll_controls(
        driver, now=2.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    await res2.poll_coroutine()
    # pylint: disable-next=protected-access
    assert not dev._current_round

    res3 = dev.poll_controls(
        driver, now=2.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    assert res3.commands_count == 1


@pytest.mark.asyncio
async def test_tick_can_mix_commands_from_several_devices():
    dev_a = _make_dali_device(short=1, controls=[_readable_control("a0")])
    dev_b = _make_dali_device(short=2, controls=[_readable_control(f"b{i}") for i in range(2)])

    scheduler = PollScheduler()
    scheduler.set_devices([dev_a, dev_b])

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, _src: [_ok_response() for _ in cmds])

    responses = await scheduler.poll(driver, 0.0, 5.0)
    polled_devices = [device for device, _ in responses]

    assert polled_devices == [dev_a, dev_b]


@pytest.mark.asyncio
async def test_dt8_colour_step_consumes_whole_tick():
    """DT-8 opening subbatch is exactly 3 cmds — the whole per-tick budget."""
    type8_handler = Type8Parameters()
    # pylint: disable-next=protected-access
    type8_handler._current_colour_type = ColourType.RGBWAF
    dev_a = _make_dali_device(short=1, type8_handler=type8_handler)

    controls_b = [_readable_control(f"b{i}") for i in range(3)]
    dev_b = _make_dali_device(short=2, controls=controls_b)

    scheduler = PollScheduler()
    scheduler.set_devices([dev_a, dev_b])

    async def opening_batch_send(cmds, _src=None, priority=None):
        del priority
        # Last response carries the active colour type (RGBWAF = 0x80).
        responses = [_ok_response() for _ in cmds]
        responses[-1].raw_value.as_integer = ColourType.RGBWAF.value
        return responses

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=opening_batch_send)

    responses = await scheduler.poll(driver, 0.0, 5.0)
    polled_devices = [device for device, _ in responses]

    assert polled_devices == [dev_a]
    # pylint: disable-next=protected-access
    assert not dev_b._current_round
    assert type8_handler.has_in_progress_read() is True


@pytest.mark.asyncio
async def test_poll_error_does_not_change_schedule():
    c = _readable_control("c", poll_interval=5.0)
    dev = _make_dali_device(controls=[c])

    driver = AsyncMock()
    driver.send_commands = AsyncMock(return_value=[None])  # synthetic transmission failure

    res = dev.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )

    # pylint: disable-next=protected-access
    assert c not in dev._current_round

    poll_results = await res.poll_coroutine()
    assert len(poll_results) == 1
    assert poll_results[0].error == "r"

    assert c.last_poll_time == 0.0
    assert not c.is_poll_due(4.9, 5.0)
    assert c.is_poll_due(5.0, 5.0)


@pytest.mark.asyncio
async def test_overdue_deadline_after_quiescent_does_not_burst_catch_up():
    controls = [_readable_control(f"c{i}", poll_interval=5.0) for i in range(6)]
    dev = _make_dali_device(controls=controls)
    for c in controls:
        c.last_poll_time = 0.0

    scheduler = PollScheduler()
    scheduler.set_devices([dev])

    sent_per_call = []

    async def fake_send(cmds, _source=None, priority=None):
        del priority
        sent_per_call.append(list(cmds))
        return [_ok_response() for _ in cmds]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    await scheduler.poll(driver, 1000.0, 5.0)
    assert sum(len(c) for c in sent_per_call) <= 3


@pytest.mark.asyncio
async def test_device_removed_drops_its_polling_state():
    dev_a = _make_dali_device(short=1, controls=[_readable_control("a0")])
    dev_b = _make_dali_device(short=2, controls=[_readable_control("b0")])

    scheduler = PollScheduler()
    scheduler.set_devices([dev_a, dev_b])

    scheduler.remove_device(dev_a)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, _src: [_ok_response() for _ in cmds])

    responses = await scheduler.poll(driver, 0.0, 5.0)
    polled = [device for device, _ in responses]

    assert dev_a not in polled
    assert dev_b in polled
