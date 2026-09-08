"""Tests for the pre-emptable DT-8 split colour poll"""

# pylint: disable=duplicate-code

from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.address import GearShort
from dali.gear.colour import QueryColourValue, QueryColourValueDTR
from dali.gear.general import DTR0, QueryActualLevel, QueryContentDTR0

from wb.mqtt_dali.application_controller import PollScheduler
from wb.mqtt_dali.common_dali_device import (
    EVENT_RESYNC_BASE_INTERVAL,
    EVENT_STARTUP_RECONFIRM_DELAY,
    DaliDeviceAddress,
    DaliDeviceBase,
    MqttControlBase,
    NotifyResult,
)
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_type8_common import ColourComponent
from wb.mqtt_dali.dali_type8_parameters import (
    COMPONENTS_BY_COLOUR_TYPE,
    MAX_COLOUR_SUBBATCH_RETRIES,
    ColourSettings,
    ColourType,
    Type8Parameters,
)
from wb.mqtt_dali.dali_type8_rgbwaf import CurrentWhiteControl
from wb.mqtt_dali.device_publisher import ControlInfo
from wb.mqtt_dali.events import ColourChanged, EventSource
from wb.mqtt_dali.wbdali_utils import MASK
from wb.mqtt_dali.wbmqtt import ControlError, ControlMeta, ControlState

from ._control_stubs import ReadableControl

# pylint: disable-next=protected-access
DaliDeviceBase._common_schema = {"title": "test-schema"}


def _readable_control(control_id: str, poll_interval=None) -> MqttControlBase:
    return ReadableControl(
        ControlInfo(control_id, ControlState(ControlMeta(read_only=True), "0")),
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


async def _make_type8_handler_with_scene(colour_type: ColourType, scene: ColourSettings) -> Type8Parameters:
    """Handler whose scene table holds ``scene``, filled through the settings read (its only
    public way in). The colour type is already known, so nothing is read for it."""
    handler = _make_type8_handler(colour_type)
    driver = AsyncMock()
    await handler.read_mandatory_info(driver, GearShort(1))
    driver.run_sequence = AsyncMock(return_value=scene)
    await handler.scenes_settings.read(driver, GearShort(1))
    return handler


def _whole_cycle_failure(colour_type: ColourType) -> ColourChanged:
    """What a failed read cycle reports: one failure flag over every component of the active
    colour type, whichever subbatch it was that ran out of retries."""
    return ColourChanged(
        {component: None for component in COMPONENTS_BY_COLOUR_TYPE[colour_type]},
        EventSource.READ,
        failed=True,
    )


def _read_components(results: list[list]) -> dict:
    """Every component the split read reported, flattened across the cycle's ticks."""
    return {
        component: raw
        for events in results
        for event in events
        for component, raw in event.components.items()
    }


@pytest.mark.asyncio
async def test_scene_colour_keeps_masked_component():
    """A scene whose stored colour leaves a component at MASK (here white) reports only the
    components it does set, so a GoToScene leaves the device's current white alone."""
    scene = ColourSettings(ColourType.RGBWAF, level=100)
    scene.colour.red, scene.colour.green, scene.colour.blue = 1, 2, 3  # white left at MASK
    handler = await _make_type8_handler_with_scene(ColourType.RGBWAF, scene)

    components = handler.scene_colour_components(3)

    assert components == {ColourComponent.RED: 1, ColourComponent.GREEN: 2, ColourComponent.BLUE: 3}
    white = CurrentWhiteControl()
    white.notify(ColourChanged({ColourComponent.WHITE: 200}, EventSource.READ))  # known current white
    scene_event = ColourChanged(components, EventSource.OBSERVED)
    assert white.notify(scene_event) is NotifyResult.NOTHING_TO_PUBLISH
    assert white.control_info.state.value == "200"


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
        res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
        assert res.poll_coroutine is not None
        results.append(await res.poll_coroutine())

    assert len(sent_calls) == 5
    assert _is_first_subbatch(sent_calls[0])
    for cmds in sent_calls[1:]:
        assert len(cmds) == 2
        assert isinstance(cmds[0], DTR0)
        assert isinstance(cmds[1], QueryColourValue)

    # The cycle reports its picture once, whole, on the last component.
    assert results[:4] == [[], [], [], []]
    assert results[4] == [
        ColourChanged(
            {
                ColourComponent.RED: 10,
                ColourComponent.GREEN: 20,
                ColourComponent.BLUE: 30,
                ColourComponent.WHITE: 40,
            },
            EventSource.READ,
        )
    ]

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
        res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
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

    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
    await res.poll_coroutine()
    assert handler.has_in_progress_read()

    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
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
        res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
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
        await scheduler.poll(driver, 0.0)

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

    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
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

    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
    poll_results = await res.poll_coroutine()

    assert poll_results == [_whole_cycle_failure(ColourType.RGBWAF)]

    assert driver.send_commands.await_count == MAX_COLOUR_SUBBATCH_RETRIES
    assert not handler.has_in_progress_read()

    assert handler.next_due_at == EVENT_STARTUP_RECONFIRM_DELAY
    # Colour is an event control: it re-syncs on the long jittered base interval, not
    # the 5s bus default. So it is not due at 5s but is due past the jitter ceiling.
    assert handler.is_poll_due(5.0) is False
    assert handler.is_poll_due(EVENT_RESYNC_BASE_INTERVAL * 1.31) is True


@pytest.mark.asyncio
async def test_type8_failed_first_subbatch_leaves_round_and_backs_off():
    """A failing DT8 first subbatch publishes the read error, then leaves the poll round:
    a second poll_controls on the same round must not re-issue the read (the handler is
    popped, no coroutine, no new bus traffic), and the handler is not due again until its
    poll interval elapses. Guards against the restart-in-place regression where a stuck
    failing colour read sticks at the head of the round."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, source=None: [_bad_response() for _ in cmds])

    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
    poll_results = await res.poll_coroutine()

    assert poll_results == [_whole_cycle_failure(ColourType.RGBWAF)]
    assert not handler.has_in_progress_read()

    sends_after_failure = driver.send_commands.await_count
    assert sends_after_failure == MAX_COLOUR_SUBBATCH_RETRIES

    # Second poll on the same round: the handler must be dropped, not restarted in place.
    res2 = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
    assert res2.poll_coroutine is None
    assert res2.has_more is False
    # pylint: disable-next=protected-access
    assert not dev._current_round
    assert driver.send_commands.await_count == sends_after_failure

    # Not due again until the interval elapses (first poll pulls the reconfirm in).
    assert handler.is_poll_due(5.0) is False
    assert handler.is_poll_due(EVENT_RESYNC_BASE_INTERVAL * 1.31) is True


@pytest.mark.asyncio
async def test_type8_failed_read_does_not_block_sibling_pollable():
    """With a persistently failing DT8 at the head of the round and a healthy sibling
    control behind it, the failing colour read must not starve the sibling: the sibling
    is skipped on the tick the DT8 subbatch runs, but once the DT8 backs off it is polled
    on the very next tick instead of the DT8 restarting ahead of it forever."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    sibling = _readable_control("sibling", poll_interval=5.0)
    dev = _make_dali_device(controls=[sibling], type8_handler=handler)
    # Put the failing DT8 at the head so it would block the sibling if it restarted in place.
    # pylint: disable-next=protected-access
    dev._pollables = [handler, sibling]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, source=None: [_bad_response() for _ in cmds])

    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
    await res.poll_coroutine()

    # First tick ran the DT8 first subbatch (and it failed); the sibling was held back.
    assert not handler.has_in_progress_read()
    assert sibling.next_due_at is None  # held back, so still unpolled

    # Next tick: DT8 backs off and is dropped, so the sibling finally gets its turn.
    dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
    assert sibling.next_due_at == 5.0
    # pylint: disable-next=protected-access
    assert handler not in dev._current_round


@pytest.mark.asyncio
async def test_type8_successful_cycle_reschedules_by_interval():
    """Regression for the happy path: a full multi-subbatch RGBWAF read completes, drops
    out of the round, and reschedules by its poll interval — a follow-up poll before the
    interval elapses must not restart the read (round stays empty, no extra bus traffic)."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    component_values = {
        QueryColourValueDTR.RedDimLevel.value: 10,
        QueryColourValueDTR.GreenDimLevel.value: 20,
        QueryColourValueDTR.BlueDimLevel.value: 30,
        QueryColourValueDTR.WhiteDimLevel.value: 40,
    }
    driver = AsyncMock()
    driver.send_commands = AsyncMock(
        side_effect=_make_send_commands(180, ColourType.RGBWAF.value, component_values)
    )

    results: list = []
    for _ in range(5):
        res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
        assert res.poll_coroutine is not None
        results.append(await res.poll_coroutine())

    assert _read_components(results) == {
        ColourComponent.RED: 10,
        ColourComponent.GREEN: 20,
        ColourComponent.BLUE: 30,
        ColourComponent.WHITE: 40,
    }
    assert not handler.has_in_progress_read()
    # pylint: disable-next=protected-access
    assert not dev._current_round

    # A poll before the interval elapses must not restart the read.
    sends_after_success = driver.send_commands.await_count
    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
    assert res.poll_coroutine is None
    assert driver.send_commands.await_count == sends_after_success

    assert handler.is_poll_due(5.0) is False
    assert handler.is_poll_due(EVENT_RESYNC_BASE_INTERVAL * 1.31) is True


@pytest.mark.asyncio
async def test_type8_unfinished_cycle_stays_in_round_regardless_of_interval():
    """Regression for the in-progress invariant: once the opening subbatch has run, a
    partially-read cycle keeps the handler in the round and continues with its component
    subbatches on the next tick even though the poll interval has not elapsed — the
    back-off guard must only apply to finished cycles, never to an in-flight one."""
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
        side_effect=_make_send_commands(100, ColourType.RGBWAF.value, component_values)
    )

    # Opening subbatch: identifies the colour type, leaves the read in progress.
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
    assert handler.has_in_progress_read()
    # The schedule has already moved on, so a *finished* cycle would back off here — but
    # this cycle is unfinished, so the handler must stay eligible at the same instant.
    assert handler.next_due_at == EVENT_STARTUP_RECONFIRM_DELAY
    assert handler.is_poll_due(0.0) is True
    # The bypass lives in `is_poll_due` only; the wait still reports the real schedule.
    assert handler.time_until_next_poll(0.0) == pytest.approx(EVENT_STARTUP_RECONFIRM_DELAY)

    # pylint: disable-next=protected-access
    assert handler in dev._current_round
    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
    assert res.poll_coroutine is not None
    await res.poll_coroutine()
    assert handler.has_in_progress_read()
    # pylint: disable-next=protected-access
    assert handler in dev._current_round


@pytest.mark.asyncio
async def test_type8_failed_component_backs_off_at_nonzero_time_then_resumes():
    """Component-subbatch failure plus the back-off guard at nonzero elapsed times: the cycle
    ends at t=2.0, the ticks that follow put no colour subbatch on the bus, and only once now
    is past the poll interval does the handler open a fresh read."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    # Large interval keeps the sibling out of the round once polled, so the resume tick
    # isolates the handler and its fresh opening is unambiguous.
    sibling = _readable_control("sibling", poll_interval=100.0)
    dev = _make_dali_device(controls=[sibling], type8_handler=handler)
    # Failing DT8 at the head: a restart-in-place would starve the sibling queued behind it.
    # pylint: disable-next=protected-access
    dev._pollables = [handler, sibling]

    sent_calls: list[list] = []

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        sent_calls.append(list(cmds))
        if _is_first_subbatch(cmds):
            return [_ok_response(60), _ok_response(0), _ok_response(ColourType.RGBWAF.value)]
        if len(cmds) == 1:  # sibling single-command query
            return [_ok_response(0)]
        return [_bad_response() for _ in cmds]  # DT8 component subbatch: fail

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    # t0: opening subbatch succeeds; a multi-component read is now in progress.
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
    assert handler.has_in_progress_read()
    # First-poll reconfirm pulls the due moment in to the startup delay.
    assert handler.next_due_at == EVENT_STARTUP_RECONFIRM_DELAY

    # t=2.0: the first component subbatch fails and ends the cycle.
    await dev.poll_controls(driver, now=2.0, max_commands=3, default_max_commands=3).poll_coroutine()
    assert not handler.has_in_progress_read()

    # The handler is still at the head of the round; the next tick pops it and gets to the
    # sibling behind it.
    await dev.poll_controls(driver, now=2.0, max_commands=3, default_max_commands=3).poll_coroutine()
    assert sibling.next_due_at == 102.0  # sibling reached in the same round, not starved

    # Next tick, still t=2.0 and before the due moment: the handler backs off.
    assert 2.0 < EVENT_STARTUP_RECONFIRM_DELAY
    sends_before_backoff = len(sent_calls)
    res_backoff = dev.poll_controls(driver, now=2.0, max_commands=3, default_max_commands=3)
    # pylint: disable-next=protected-access
    assert handler not in dev._current_round  # popped, not restarted in place
    assert res_backoff.poll_coroutine is None
    assert len(sent_calls) == sends_before_backoff  # no colour subbatch on the bus this tick

    # now past the due moment: guard falls through, handler opens a fresh read (first subbatch).
    resume_now = EVENT_STARTUP_RECONFIRM_DELAY + 1.0
    sends_before_resume = len(sent_calls)
    res_resume = dev.poll_controls(driver, now=resume_now, max_commands=3, default_max_commands=3)
    assert res_resume.poll_coroutine is not None
    await res_resume.poll_coroutine()
    resume_sends = sent_calls[sends_before_resume:]
    assert any(_is_first_subbatch(c) for c in resume_sends)
    assert handler.has_in_progress_read()


@pytest.mark.asyncio
async def test_type8_component_failure_ends_the_cycle_without_asking_the_rest():
    """Gear that answered the opening subbatch and then went silent: the first component
    subbatch exhausts its retries and that ends the cycle, so the failure costs one retry set
    instead of one per component and is reported over the whole colour type."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    sent_calls: list[list] = []

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        sent_calls.append(list(cmds))
        if _is_first_subbatch(cmds):
            return [_ok_response(60), _ok_response(0), _ok_response(ColourType.RGBWAF.value)]
        return [_bad_response() for _ in cmds]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
    events = await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()

    assert events == [_whole_cycle_failure(ColourType.RGBWAF)]
    assert not handler.has_in_progress_read()
    component_subbatches = [cmds for cmds in sent_calls if not _is_first_subbatch(cmds)]
    assert len(component_subbatches) == MAX_COLOUR_SUBBATCH_RETRIES
    assert {cmds[0].param for cmds in component_subbatches} == {QueryColourValueDTR.RedDimLevel.value}


@pytest.mark.asyncio
async def test_type8_first_poll_schedules_startup_reconfirm():
    """A DT8 colour control's first ever poll pulls one reconfirm in at the startup delay,
    mirroring the gear-control first-poll behaviour."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, source=None: [_bad_response() for _ in cmds])

    dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)

    assert handler.next_due_at == EVENT_STARTUP_RECONFIRM_DELAY


@pytest.mark.asyncio
async def test_type8_confirmation_survives_the_end_of_the_cycle_it_interrupted():
    """A confirmation landing mid-cycle must outlive that cycle: the read in flight started
    before the command and reports the pre-command colour, so ending the cycle must not drop
    the confirmation deadline back to the periodic schedule."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    dev = _make_dali_device(type8_handler=handler)

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        if _is_first_subbatch(cmds):
            return [_ok_response(60), _ok_response(0), _ok_response(ColourType.RGBWAF.value)]
        return [_bad_response() for _ in cmds]  # component subbatch fails -> cycle ends

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
    assert handler.has_in_progress_read()

    handler.schedule_poll_at(50.0)  # a colour command observed on the bus mid-cycle

    # The first component subbatch fails, and its exhausted retries end the cycle.
    await dev.poll_controls(driver, now=2.0, max_commands=3, default_max_commands=3).poll_coroutine()
    assert not handler.has_in_progress_read()
    assert handler.next_due_at == 50.0


@pytest.mark.asyncio
async def test_type8_confirmation_survives_a_cycle_that_completes_successfully():
    """The sibling of the failure case above: a finished cycle ends at a different code site,
    and it must not stamp the schedule either — the #209 back-off comes from the open stamp."""
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
        side_effect=_make_send_commands(180, ColourType.RGBWAF.value, component_values)
    )

    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
    assert handler.has_in_progress_read()

    handler.schedule_poll_at(50.0)  # a colour command observed on the bus mid-cycle

    # Drain the remaining component subbatches; the cycle reaches its success path.
    for _ in range(8):
        if not handler.has_in_progress_read():
            break
        res = dev.poll_controls(driver, now=2.0, max_commands=3, default_max_commands=3)
        assert res.poll_coroutine is not None
        await res.poll_coroutine()

    assert not handler.has_in_progress_read()
    assert handler.next_due_at == 50.0


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

    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
    assert handler.has_in_progress_read()

    dev.reset_polling_state()
    assert not handler.has_in_progress_read()
    # pylint: disable-next=protected-access
    assert not dev._current_round

    sends_before = driver.send_commands.await_count
    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
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

    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
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

    assert handler.is_poll_due(0.0) is True
    assert a.is_poll_due(0.0) is True

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=lambda cmds, source=None: [_ok_response() for _ in cmds])

    res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
    assert res.commands_count == 1
    # pylint: disable-next=protected-access
    assert handler in dev._current_round
    # Handler deferred (3-cmd subbatch doesn't fit in remaining 2-cmd budget) so its schedule
    # has not moved yet. Asserted on the moment: the bypass makes `is_poll_due` True either way.
    assert handler.next_due_at is None
    assert not handler.has_in_progress_read()
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

    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()
    await dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3).poll_coroutine()

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
        res = dev.poll_controls(driver, now=0.0, max_commands=3, default_max_commands=3)
        results.append(await res.poll_coroutine())

    assert _read_components(results) == {
        ColourComponent.X_COORDINATE: 0x1234,
        ColourComponent.Y_COORDINATE: 0x5678,
    }


class _ColourControlsDevice(DaliDevice):
    """Gear whose controls are exactly the ones its DT8 handler declares, so a read cycle's
    event reaches them through the real ``notify_all`` fan-out."""

    def __init__(self, handler: Type8Parameters) -> None:
        self._colour_handler = handler
        super().__init__(DaliDeviceAddress(short=1, random=0), "bus1", MagicMock())
        self.rebuild_mqtt_controls()

    def _build_mqtt_controls(self) -> list[MqttControlBase]:
        return self._colour_handler.get_mqtt_controls()


async def _colour_chunk_events(handler: Type8Parameters, driver, now: float = 0.0) -> list:
    """One tick of the split colour read: what the chunk that just landed produced -- nothing
    until the cycle ends or fails."""
    step = handler.next_poll_step(driver, GearShort(1), max_commands=3, default_max_commands=3, now=now)
    assert step.poll_coroutine is not None
    return await step.poll_coroutine()


async def _published_per_tick(
    device: DaliDevice, handler: Type8Parameters, driver, ticks: int, now: float = 0.0
) -> list[list[str]]:
    """Drive ``ticks`` poll ticks, dispatching what each one produced to the device, and
    collect per tick the ids of the controls that asked to publish."""
    per_tick: list[list[str]] = []
    for _ in range(ticks):
        tick: list[str] = []
        for event in await _colour_chunk_events(handler, driver, now=now):
            tick.extend(c.control_info.id for c in device.notify_all(event))
        per_tick.append(tick)
    return per_tick


def _value(device: DaliDevice, control_id: str) -> str:
    return device.get_mqtt_control(control_id).control_info.state.value


_RGBWAF_VALUES = {
    QueryColourValueDTR.RedDimLevel.value: 10,
    QueryColourValueDTR.GreenDimLevel.value: 20,
    QueryColourValueDTR.BlueDimLevel.value: 30,
    QueryColourValueDTR.WhiteDimLevel.value: 40,
}


def _error(device: DaliDevice, control_id: str) -> ControlError:
    return device.get_mqtt_control(control_id).control_info.state.error


@pytest.mark.asyncio
async def test_colour_read_publishes_the_whole_cycle_at_its_end():
    """A full RGBWAF read cycle through the real controls: the unit of publication is the
    cycle, not the chunk, so nothing goes out until its last component, and then every control
    of the active colour type does -- each current_* out of this one read, with its set_*."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    device = _ColourControlsDevice(handler)
    driver = AsyncMock()
    driver.send_commands = AsyncMock(
        side_effect=_make_send_commands(180, ColourType.RGBWAF.value, _RGBWAF_VALUES)
    )

    # 5 ticks: the opening subbatch plus one per component.
    published_per_tick = await _published_per_tick(device, handler, driver, ticks=5)

    assert published_per_tick == [
        [],
        [],
        [],
        [],
        ["current_rgb", "set_rgb", "current_white", "set_white"],
    ]
    assert not handler.has_in_progress_read()
    assert _value(device, "current_rgb") == "10;20;30"
    assert _value(device, "set_rgb") == "10;20;30"
    assert _value(device, "current_white") == "40"
    assert _value(device, "set_white") == "40"


@pytest.mark.asyncio
async def test_colour_read_failure_errors_every_topic_of_the_type():
    """One cycle lands, then the next one's red subbatch runs out of retries and ends it. The
    failure reaches every current_* of the active colour type, not just the topic that owns
    red, and their values stand under the error; the set_* mirrors keep theirs, error-free."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    device = _ColourControlsDevice(handler)
    driver = AsyncMock()
    driver.send_commands = AsyncMock(
        side_effect=_make_send_commands(180, ColourType.RGBWAF.value, _RGBWAF_VALUES)
    )
    await _published_per_tick(device, handler, driver, ticks=5)
    assert _value(device, "current_rgb") == "10;20;30"

    async def fake_send(cmds, source=None, priority=None):  # pylint: disable=unused-argument
        if _is_first_subbatch(cmds):
            return [_ok_response(180), _ok_response(0), _ok_response(ColourType.RGBWAF.value)]
        return [_bad_response() for _ in cmds]

    driver.send_commands = AsyncMock(side_effect=fake_send)
    # The handler is due again once the startup reconfirm delay has passed.
    published_per_tick = await _published_per_tick(
        device, handler, driver, ticks=2, now=EVENT_STARTUP_RECONFIRM_DELAY
    )

    assert published_per_tick == [[], ["current_rgb", "current_white"]]
    assert not handler.has_in_progress_read()
    assert _error(device, "current_rgb") == ControlError.READ
    assert _error(device, "current_white") == ControlError.READ
    assert _value(device, "current_rgb") == "10;20;30"
    assert _value(device, "current_white") == "40"
    assert _error(device, "set_rgb") == ControlError.NONE
    assert _error(device, "set_white") == ControlError.NONE
    assert _value(device, "set_rgb") == "10;20;30"
    assert _value(device, "set_white") == "40"


@pytest.mark.asyncio
async def test_masked_component_errors_only_its_own_topic():
    """The gear answers green with its MASK sentinel and the rest normally. That is no failed
    read, so MASK travels raw and is resolved by the control that owns the component:
    current_rgb errors itself, current_white publishes as usual, set_rgb keeps its value."""
    handler = _make_type8_handler(ColourType.RGBWAF)
    device = _ColourControlsDevice(handler)
    driver = AsyncMock()
    driver.send_commands = AsyncMock(
        side_effect=_make_send_commands(
            180,
            ColourType.RGBWAF.value,
            {**_RGBWAF_VALUES, QueryColourValueDTR.GreenDimLevel.value: MASK},
        )
    )

    published_per_tick = await _published_per_tick(device, handler, driver, ticks=5)

    assert published_per_tick == [[], [], [], [], ["current_rgb", "current_white", "set_white"]]
    assert _error(device, "current_rgb") == ControlError.READ
    assert _value(device, "current_rgb") == "0;0;0"  # the default it started at: MASK is no value
    assert _error(device, "set_rgb") == ControlError.NONE
    assert _value(device, "set_rgb") == "0;0;0"
    assert _error(device, "current_white") == ControlError.NONE
    assert _value(device, "current_white") == "40"


def test_observed_command_leaves_a_component_it_did_not_name_alone():
    """A sniffed RGB command on a device nothing has read yet: white, which no command and no
    read has ever named, is left out of the event and its control stays at its default.

    Then white becomes known and the same command arrives carrying it at MASK, the way our own
    colour writes fill in the fields they do not set -- a filler that must not erase it.
    """
    handler = _make_type8_handler(ColourType.RGBWAF)
    device = _ColourControlsDevice(handler)

    event = handler.apply_observed_colour(
        {ColourComponent.RED: 1, ColourComponent.GREEN: 2, ColourComponent.BLUE: 3}, settle_at=7.0
    )

    assert ColourComponent.WHITE not in event.components
    assert event.settle_at == 7.0
    assert [c.control_info.id for c in device.notify_all(event)] == ["current_rgb", "set_rgb"]
    assert _value(device, "current_white") == "0"
    assert _error(device, "current_white") == ControlError.NONE

    handler.apply_observed_colour({ColourComponent.WHITE: 40}, settle_at=7.0)
    with_filler = handler.apply_observed_colour(
        {
            ColourComponent.RED: 4,
            ColourComponent.GREEN: 5,
            ColourComponent.BLUE: 6,
            ColourComponent.WHITE: MASK,
        },
        settle_at=7.0,
    )

    assert with_filler.components[ColourComponent.WHITE] == 40
