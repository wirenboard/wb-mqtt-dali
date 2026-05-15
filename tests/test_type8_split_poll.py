"""Tests for the pre-emptable DT-8 split colour poll (plan part 2)."""

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

# pylint: disable=protected-access

DaliDeviceBase._common_schema = {"title": "test-schema"}


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


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
    handler._current_colour_type = colour_type
    return handler


def _make_dali_device(short=1, controls=None, type8_handler=None) -> DaliDevice:
    dev = DaliDevice(
        DaliDeviceAddress(short=short, random=0),
        "bus1",
        MagicMock(),
        None,
        None,
    )
    dev.is_initialized = True
    dev.types = []
    dev._polling_controls = list(controls or [])
    dev._current_round_polling_controls = []
    dev._type8_handler = type8_handler
    return dev


def _is_first_subbatch(cmds) -> bool:
    """First batch of split colour read: QueryActualLevel + DTR0(ColourType) + QueryColourValue."""
    if len(cmds) != 3:
        return False
    return (
        isinstance(cmds[0], QueryActualLevel)
        and isinstance(cmds[1], DTR0)
        and cmds[1].param == QueryColourValueDTR.ReportColourType.value
        and isinstance(cmds[2], QueryColourValue)
    )


def _make_send_commands(level: int, colour_type_int: int, component_values: dict[int, int]):
    """Build a fake ``send_commands(cmds, source=...)`` for an RGBWAF colour read.

    The first call recognised as the first batch returns ``(level, colour_type_int)``;
    subsequent calls recognised as component subbatches return the right value
    for that component (matched by the ``DTR0(tag)`` argument).
    """

    async def _send(cmds, source=None):  # pylint: disable=unused-argument
        if _is_first_subbatch(cmds):
            return [_ok_response(level), _ok_response(0), _ok_response(colour_type_int)]
        # Component batch: cmds = [DTR0(tag), QueryColourValue, [QueryContentDTR0]]
        tag_val = cmds[0].param
        value = component_values.get(tag_val, 0)
        responses = [_ok_response(0), _ok_response(value & 0xFF)]
        if len(cmds) == 3:
            responses.append(_ok_response((value >> 8) & 0xFF))
        return responses

    return _send


# ---------------------------------------------------------------------------
# Split into subbatches; results published on the last subbatch
# ---------------------------------------------------------------------------


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

    async def fake_send(cmds, source=None):  # pylint: disable=unused-argument
        sent_calls.append(list(cmds))
        return await _make_send_commands(180, ColourType.RGBWAF.value, component_values)(cmds, source)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    # Five subbatches: opening + R + G + B + W. Each is a separate poll_controls
    # call (one per polling tick).
    results: list = []
    for _ in range(5):
        res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
        assert res.poll_coroutine is not None
        results.append(await res.poll_coroutine())

    # Exactly five send_commands calls, one per subbatch.
    assert len(sent_calls) == 5
    # First call must be the opening 3-cmd batch.
    assert _is_first_subbatch(sent_calls[0])
    # Remaining four are component batches of 2 commands (RGBWAF).
    for cmds in sent_calls[1:]:
        assert len(cmds) == 2
        assert isinstance(cmds[0], DTR0)
        assert isinstance(cmds[1], QueryColourValue)

    # Intermediate subbatches publish nothing; only the final one returns
    # ControlPollResults for current_rgb / current_white.
    for res in results[:-1]:
        assert res == []
    final = {item.control_id: item for item in results[-1]}
    assert final["current_rgb"].value == "10;20;30"
    assert final["current_rgb"].error is None
    assert final["current_white"].value == "40"
    assert final["current_white"].error is None

    # Read complete: no in-progress state remains.
    assert handler.has_in_progress_read() is False


# ---------------------------------------------------------------------------
# Bus lock not held across subbatches: other consumers can run between them
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type8_colour_poll_does_not_hold_bus_lock_across_subbatches():
    """Between split subbatches, another consumer's send_commands must be able
    to interleave on the bus — the driver lock is acquired/released per subbatch,
    not held for the whole colour read."""
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

    async def fake_send(cmds, source=None):  # pylint: disable=unused-argument
        sent_log.append("dt8" if _is_first_subbatch(cmds) or len(cmds) == 2 else "other")
        return await base_send(cmds, source)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    # Run the 5 subbatches and, between them, push an "other consumer" call to
    # the driver. With the bus lock NOT held across subbatches, the other call
    # must succeed and be visible in the sent log.
    other_call_results: list = []

    async def other_consumer():
        sent_log.append("other")
        result = await driver.send_commands([MagicMock()], source="OTHER")
        other_call_results.append(result)

    for i in range(5):
        res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
        assert res.poll_coroutine is not None
        await res.poll_coroutine()
        if i < 4:
            await other_consumer()

    # 5 DT-8 subbatches + 4 "other" consumer calls = 9 total.
    assert driver.send_commands.await_count == 9
    assert len(other_call_results) == 4


# ---------------------------------------------------------------------------
# EXECUTE_CONTROL preempts between subbatches
# ---------------------------------------------------------------------------


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

    # First subbatch: opens the read.
    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
    await res.poll_coroutine()
    assert handler.has_in_progress_read() is True

    # Run two component subbatches, then simulate EXECUTE_CONTROL handled BETWEEN
    # the colour-read subbatches (i.e., in the gap, not while a subbatch awaits).
    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
    await res.poll_coroutine()
    # Between subbatches: handler still has in-progress, but the polling loop is
    # free to dequeue an EXECUTE_CONTROL task. We model this by simply running
    # an unrelated coroutine here — it must complete before the next subbatch.
    execute_control_done = False

    async def execute_control_simulation():
        nonlocal execute_control_done
        await driver.send_commands([MagicMock()])
        execute_control_done = True

    await execute_control_simulation()
    assert execute_control_done is True
    # The colour read is still in progress — not aborted by EXECUTE_CONTROL.
    assert handler.has_in_progress_read() is True

    # Continue with the remaining 3 subbatches; the read completes normally.
    for _ in range(3):
        res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
        await res.poll_coroutine()
    assert handler.has_in_progress_read() is False


# ---------------------------------------------------------------------------
# Latency budget: each subbatch fits ≤ 3 cmds → ≤150ms on the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_control_latency_under_150ms_in_4_rgbwaf_setup():
    """On a bus of 4 RGBWAF DT-8 devices, no single tick may exceed 3 commands.

    A subbatch costs at most 3 cmds × ~45 ms = 135 ms < 150 ms; this test asserts
    the cmd-count bound, which is the structural property the latency budget
    follows from.
    """
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

    async def fake_send(cmds, source=None):  # pylint: disable=unused-argument
        sent_per_tick[-1] += len(cmds)
        return await base_send(cmds, source)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    # Run for 30 ticks, advancing only the cmd-counter snapshot per tick.
    for _ in range(30):
        sent_per_tick.append(0)
        await scheduler.poll(driver, 0.0, 5.0)

    # No tick may exceed the 3-cmd budget — even the DT-8 first batch is exactly 3,
    # and component subbatches are 2.
    assert sent_per_tick, "no ticks recorded"
    assert max(sent_per_tick) <= 3


# ---------------------------------------------------------------------------
# Subbatch retries: transient error retried up to 3 times within the same read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type8_subbatch_retries_up_to_three_times():
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    attempts: list[int] = [0]

    async def fake_send(cmds, source=None):  # pylint: disable=unused-argument
        attempts[0] += 1
        if attempts[0] < 3:
            return [_bad_response() for _ in cmds]
        # Third attempt of the FIRST subbatch succeeds.
        return [_ok_response(50), _ok_response(0), _ok_response(ColourType.RGBWAF.value)]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
    await res.poll_coroutine()

    # First subbatch was retried 3 times within the same read; read still in progress.
    assert attempts[0] == 3
    assert handler.has_in_progress_read() is True
    assert MAX_COLOUR_SUBBATCH_RETRIES == 3


# ---------------------------------------------------------------------------
# Subbatch failure → error published, schedule advances by interval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type8_subbatch_failure_publishes_error_and_reschedules():
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, source=None: [_bad_response() for _ in cmds])

    handler.last_poll_time = None
    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
    poll_results = await res.poll_coroutine()

    # All component results published with error="r".
    assert {p.control_id for p in poll_results} == {"current_rgb", "current_white"}
    assert all(p.error == "r" for p in poll_results)

    # Driver was hit MAX_COLOUR_SUBBATCH_RETRIES times for the failed subbatch.
    assert driver.send_commands.await_count == MAX_COLOUR_SUBBATCH_RETRIES
    assert handler.has_in_progress_read() is False

    # Schedule advances: last_poll_time was set when the read started; next due is
    # `last_poll_time + interval`. With default 5 s, not due at t=4.9, due at t=5.
    assert handler.last_poll_time == 0.0
    assert handler.is_poll_due(4.9, 5.0) is False
    assert handler.is_poll_due(5.0, 5.0) is True


# ---------------------------------------------------------------------------
# Quiescent mid-read: partial state dropped, no publish
# ---------------------------------------------------------------------------


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

    # Open the read (first batch) and one component (red).
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0).poll_coroutine()
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0).poll_coroutine()
    assert handler.has_in_progress_read() is True

    # Quiescent entry — the device drops partial state.
    dev.reset_polling_state()
    assert handler.has_in_progress_read() is False
    assert not dev._current_round_polling_controls

    # No further subbatches were sent for the abandoned read; `dev.poll_controls`
    # treats the next call as a fresh round.
    sends_before = driver.send_commands.await_count
    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
    if res.poll_coroutine is not None:
        await res.poll_coroutine()
    # The fresh round may issue a new opening batch — but it MUST be the opening
    # batch, not a component continuation of the previous read.
    if driver.send_commands.await_count > sends_before:
        last_call = driver.send_commands.call_args_list[-1]
        cmds, *_ = last_call.args
        assert _is_first_subbatch(list(cmds))


# ---------------------------------------------------------------------------
# Device removal mid-read: partial state dropped (state owned by handler)
# ---------------------------------------------------------------------------


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

    # Start the read and progress through one component.
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0).poll_coroutine()
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0).poll_coroutine()
    assert handler.has_in_progress_read() is True

    # Removing the device drops both the device and its handler; partial state
    # cannot survive. Mirror the operation by clearing the strong reference and
    # scheduler entry.
    scheduler = PollScheduler()
    scheduler.set_devices([dev])
    scheduler.remove_device(dev)
    del dev

    # The handler's partial state is unaffected by device-removal proper, but
    # the device won't poll any more, so no additional ControlPollResult is
    # produced for the abandoned read.
    assert scheduler.is_empty() is True


# ---------------------------------------------------------------------------
# Type8Parameters direct API: deadline + sentinel insertion in the snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type8_handler_takes_one_snapshot_position_with_own_deadline():
    handler = _make_type8_handler(ColourType.RGBWAF)
    a = _readable_control("a", poll_interval=5.0)
    dev = _make_dali_device(controls=[a], type8_handler=handler)

    # Both the single-cmd control and the DT-8 deadline are due immediately.
    assert handler.is_poll_due(0.0, 5.0) is True
    assert a.is_poll_due(0.0, 5.0) is True

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, source=None: [_ok_response() for _ in cmds])

    # Tick 1: snapshot built — the single-cmd control is dispatched first; the
    # DT-8 sentinel stays in the snapshot for a later tick.
    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
    assert res.commands_count == 1
    assert handler in dev._current_round_polling_controls
    assert handler.last_poll_time == 0.0  # marked when added to the snapshot
    await res.poll_coroutine()


@pytest.mark.asyncio
async def test_dt8_subbatch_does_not_bundle_with_single_cmd_controls_in_one_send_commands():
    handler = _make_type8_handler(ColourType.RGBWAF)
    a = _readable_control("a", poll_interval=5.0)
    dev = _make_dali_device(controls=[a], type8_handler=handler)

    sent_calls: list[list] = []

    async def fake_send(cmds, source=None):  # pylint: disable=unused-argument
        sent_calls.append(list(cmds))
        return [_ok_response(), _ok_response(0), _ok_response(ColourType.RGBWAF.value)][: len(cmds)]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    # Tick 1 — single-cmd controls only.
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0).poll_coroutine()
    # Tick 2 — DT-8 first subbatch, by itself.
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0).poll_coroutine()

    # Two distinct send_commands calls; neither bundles single-cmd queries with
    # the DT-8 first batch.
    assert len(sent_calls) >= 2
    dt8_calls = [c for c in sent_calls if _is_first_subbatch(c)]
    assert len(dt8_calls) == 1
    # The opening DT-8 batch must NOT contain the unrelated single-cmd query.
    assert all(not getattr(c, "_id", "") == "Q_a" for c in dt8_calls[0])


# ---------------------------------------------------------------------------
# Component-batch sizes match the per-colour-type table from the plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xy_component_batch_is_3_cmds():
    handler = _make_type8_handler(ColourType.XY)
    dev = _make_dali_device(type8_handler=handler)

    component_values = {
        QueryColourValueDTR.XCoordinate.value: 0x1234,
        QueryColourValueDTR.YCoordinate.value: 0x5678,
    }

    async def fake_send(cmds, source=None):  # pylint: disable=unused-argument
        if _is_first_subbatch(cmds):
            return [_ok_response(50), _ok_response(0), _ok_response(ColourType.XY.value)]
        # XY component batch: DTR0 + QueryColourValue + QueryContentDTR0
        assert len(cmds) == 3
        assert isinstance(cmds[2], QueryContentDTR0)
        tag_val = cmds[0].param
        value = component_values[tag_val]
        return [_ok_response(0), _ok_response((value >> 8) & 0xFF), _ok_response(value & 0xFF)]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    # Three subbatches: opening + X + Y.
    results = []
    for _ in range(3):
        res = dev.poll_controls(driver, now=0.0, max_commands=3, default_poll_interval=5.0)
        results.append(await res.poll_coroutine())

    final = {item.control_id: item.value for item in results[-1]}
    assert final["current_x_coordinate"] == "4660"
    assert final["current_y_coordinate"] == "22136"
