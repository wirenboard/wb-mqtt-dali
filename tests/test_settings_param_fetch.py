"""Tests for background incremental fetch of settings params.

The fetch mechanics (cursor, round-robin, reconciliation, one-shot, exception handling) are tested
directly on `SettingsFetchScheduler` and the param classes with a fake driver. Only the wiring and
priority are tested through `ApplicationController`.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.address import GearShort
from dali.gear.general import (
    QueryFadeTimeFadeRate,
    QueryGroupsEightToFifteen,
    QueryGroupsZeroToSeven,
)

from wb.mqtt_dali.application_controller import (
    ApplicationControllerTask,
    ApplicationControllerTaskType,
)
from wb.mqtt_dali.commissioning import CommissioningResult
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali_common_parameters import (
    SCENES_FETCH_CHUNK,
    SCENES_TOTAL,
    GroupsParam,
    MaxLevelParam,
    MinLevelParam,
    ScenesParam,
)
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_type8_parameters import (
    ColourSettings,
    ColourType,
    ScenesSettings,
)
from wb.mqtt_dali.dali_type8_tc import Type8TcLimits
from wb.mqtt_dali.fetch_scheduler import SettingsFetchScheduler
from wb.mqtt_dali.settings import SettingsParamBase, SettingsParamName

from ._app_controller_helpers import make_loop_controller, stop_loop

# Prevent file system access inside DaliDeviceBase.__init__
DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access

_LOGGER = logging.getLogger("test.fetch")


# --- Fakes ---------------------------------------------------------------


def _query_response(value: int) -> MagicMock:
    resp = MagicMock()
    resp.raw_value = MagicMock()
    resp.raw_value.as_integer = value
    resp.raw_value.error = False
    return resp


class _SceneLevelDriver:  # pylint: disable=too-few-public-methods
    """Fake driver answering QuerySceneLevel batches by scene number -> level."""

    def __init__(self, levels: dict) -> None:
        self._levels = levels
        self.batches: list[list[int]] = []

    async def send_commands(self, commands, source=None, priority=None):
        del source, priority
        scenes = [cmd.param for cmd in commands]
        self.batches.append(scenes)
        return [_query_response(self._levels[s]) for s in scenes]


class _FakeFetchDevice:  # pylint: disable=too-few-public-methods
    """Minimal device exposing only what SettingsFetchScheduler reads."""

    def __init__(self, params, short: int = 3) -> None:
        self.is_initialized = False
        self.address = DaliDeviceAddress(short=short, random=0)
        self.dali_commands = DaliCommandsCompatibilityLayer()
        self._params = params

    def get_settings_parameter_handlers(self):
        return self._params


def _stub_param(cls, fetch_return):
    """Real settings param with its fetch replaced by a controllable AsyncMock.

    Returns the param and the mock; assert on the returned mock so the static checker sees a Mock.
    """
    param = cls()
    fetch = AsyncMock(return_value=fetch_return)
    param.fetch = fetch
    return param, fetch


# --- Base fetch ----------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_base_calls_read_and_returns_true():
    """The default fetch reads the whole param in one call and reports completion."""
    param = SettingsParamBase(SettingsParamName("x"))
    param.read = AsyncMock(return_value={"k": 1})
    driver = AsyncMock()
    addr = GearShort(3)

    result = await param.fetch(driver, addr, _LOGGER)

    param.read.assert_awaited_once_with(driver, addr, _LOGGER)
    assert result is True


@pytest.mark.asyncio
async def test_fetch_param_completes_over_multiple_calls():
    """An overriding fetch returns False until the last chunk, then True; the accumulated state
    matches a full read() and fetch itself returns only a bool (no values leak out)."""
    levels = {i: i * 10 for i in range(SCENES_TOTAL)}
    driver = _SceneLevelDriver(levels)
    addr = GearShort(3)
    param = ScenesParam()

    results = []
    for _ in range(SCENES_TOTAL // SCENES_FETCH_CHUNK):
        results.append(await param.fetch(driver, addr, _LOGGER))

    assert all(isinstance(r, bool) for r in results)
    assert results[:-1] == [False] * (len(results) - 1)
    assert results[-1] is True

    # The accumulated state after fetch equals a full read() of the same device.
    fetched = await param.read(driver, addr, _LOGGER)
    fresh = ScenesParam()
    full = await fresh.read(_SceneLevelDriver(levels), addr, _LOGGER)
    assert fetched == full


@pytest.mark.asyncio
async def test_scenesparam_fetch_two_chunks():
    """ScenesParam reads scenes 0-7 (False) then 8-15 (True); the result equals a full read."""
    levels = {i: i for i in range(SCENES_TOTAL)}
    driver = _SceneLevelDriver(levels)
    addr = GearShort(7)
    param = ScenesParam()

    first = await param.fetch(driver, addr, _LOGGER)
    second = await param.fetch(driver, addr, _LOGGER)

    assert first is False
    assert second is True
    assert driver.batches[0] == list(range(0, SCENES_FETCH_CHUNK))
    assert driver.batches[1] == list(range(SCENES_FETCH_CHUNK, SCENES_TOTAL))

    fresh = ScenesParam()
    assert await param.read(driver, addr, _LOGGER) == await fresh.read(_SceneLevelDriver(levels), addr)


@pytest.mark.asyncio
async def test_scenessettings_fetch_one_scene_per_call():
    """ScenesSettings reads exactly one DT8 scene per call (16 calls, last True); the accumulated
    state equals a full read(); a follow-up read() re-reads every scene from scratch."""
    addr = GearShort(3)
    param = ScenesSettings(ColourType.RGBWAF, Type8TcLimits())
    driver = AsyncMock()
    driver.run_sequence = AsyncMock(side_effect=lambda *a, **k: ColourSettings(ColourType.RGBWAF, 5))

    for i in range(SCENES_TOTAL - 1):
        done = await param.fetch(driver, addr, _LOGGER)
        assert done is False
        assert driver.run_sequence.await_count == i + 1
    done = await param.fetch(driver, addr, _LOGGER)
    assert done is True
    assert driver.run_sequence.await_count == SCENES_TOTAL

    fetched = await param.read(driver, addr, _LOGGER)
    assert driver.run_sequence.await_count == 2 * SCENES_TOTAL  # read() re-reads every scene
    assert len(fetched[param.property_name]) == SCENES_TOTAL

    fresh = ScenesSettings(ColourType.RGBWAF, Type8TcLimits())
    fresh_driver = AsyncMock()
    fresh_driver.run_sequence = AsyncMock(side_effect=lambda *a, **k: ColourSettings(ColourType.RGBWAF, 5))
    assert fetched == await fresh.read(fresh_driver, addr)


# --- read() vs background fetch -----------------------------------------


@pytest.mark.asyncio
async def test_read_refetches_from_scratch():
    """After a partial background fetch, a full read() re-queries all 16 scenes from scratch and
    marks fetch complete, so a subsequent fetch returns True immediately."""
    levels = {i: i for i in range(SCENES_TOTAL)}
    driver = _SceneLevelDriver(levels)
    addr = GearShort(3)
    param = ScenesParam()

    await param.fetch(driver, addr, _LOGGER)  # scenes 0-7, partial
    result = await param.read(driver, addr, _LOGGER)

    assert driver.batches[-1] == list(range(SCENES_TOTAL))  # full re-read from scratch
    assert len(result["scenes"]) == SCENES_TOTAL
    assert await param.fetch(driver, addr, _LOGGER) is True  # nothing left


# --- Scheduler mechanics -------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_exception_drops_param():
    """A fetch that raises is logged and dropped (never retried), while the other params keep
    being fetched."""
    bad = MaxLevelParam()
    bad_fetch = AsyncMock(side_effect=RuntimeError("boom"))
    bad.fetch = bad_fetch
    good, good_fetch = _stub_param(MinLevelParam, fetch_return=False)
    device = _FakeFetchDevice([bad, good])
    scheduler = SettingsFetchScheduler()
    scheduler.add_device(device)
    driver = AsyncMock()

    for _ in range(3):
        await scheduler.fetch_step(driver, _LOGGER)

    bad_fetch.assert_awaited_once()  # dropped after the first failure, never retried
    assert good_fetch.await_count == 2


@pytest.mark.asyncio
async def test_fetch_added_when_device_registered_is_round_robined():
    """add_device makes the device's fetch params eligible; the scheduler then round-robins one
    fetch per step across them."""
    p1, p1_fetch = _stub_param(MaxLevelParam, fetch_return=False)
    p2, p2_fetch = _stub_param(MinLevelParam, fetch_return=False)
    device = _FakeFetchDevice([p1, p2])
    scheduler = SettingsFetchScheduler()
    assert scheduler.is_empty()

    scheduler.add_device(device)
    driver = AsyncMock()

    await scheduler.fetch_step(driver, _LOGGER)
    await scheduler.fetch_step(driver, _LOGGER)

    assert p1_fetch.await_count == 1
    assert p2_fetch.await_count == 1


@pytest.mark.asyncio
async def test_fetch_dropped_when_device_removed():
    """remove_device drops the device's params; it is called from every removal path (single
    _remove_device and the batched commissioning sweep)."""
    param, param_fetch = _stub_param(MaxLevelParam, fetch_return=False)
    device = _FakeFetchDevice([param])
    scheduler = SettingsFetchScheduler()
    scheduler.add_device(device)
    assert not scheduler.is_empty()
    driver = AsyncMock()

    scheduler.remove_device(device)

    assert scheduler.is_empty()
    assert await scheduler.fetch_step(driver, _LOGGER) is False
    param_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_refreshed_after_commissioning():
    """After commissioning, vanished devices are dropped via remove_device while surviving devices'
    params keep being fetched."""
    old_param, old_fetch = _stub_param(MaxLevelParam, fetch_return=False)
    new_param, new_fetch = _stub_param(MaxLevelParam, fetch_return=False)
    old_device = _FakeFetchDevice([old_param], short=1)
    new_device = _FakeFetchDevice([new_param], short=2)
    scheduler = SettingsFetchScheduler()
    scheduler.add_device(old_device)
    scheduler.add_device(new_device)
    driver = AsyncMock()

    scheduler.remove_device(old_device)  # vanished during commissioning

    await scheduler.fetch_step(driver, _LOGGER)
    await scheduler.fetch_step(driver, _LOGGER)

    old_fetch.assert_not_awaited()
    assert new_fetch.await_count == 2


# --- Fade read at init ---------------------------------------------------


@pytest.mark.asyncio
async def test_fade_read_during_initialization():
    """initialize() reads fade time/rate on the bus alongside groups, before any load_info, so the
    value is available right after the device comes up."""
    device = DaliDevice(DaliDeviceAddress(short=5, random=0x10), "gw_bus_1", MagicMock())
    driver = AsyncMock()
    driver.run_sequence = AsyncMock(return_value=[])  # no part-2xx device types
    driver.send_commands = AsyncMock(return_value=[_query_response(0), _query_response(0)])

    fade_resp = MagicMock()
    fade_resp.raw_value = MagicMock(error=False)
    fade_resp.fade_time = 3
    fade_resp.fade_rate = 7
    driver.send = AsyncMock(return_value=fade_resp)

    await device.initialize(driver)

    assert device.is_initialized
    sent = [call.args[0] for call in driver.send.await_args_list]
    assert any(isinstance(cmd, QueryFadeTimeFadeRate) for cmd in sent)
    group_cmds = [cmd for batch in driver.send_commands.await_args_list for cmd in batch.args[0]]
    assert any(isinstance(cmd, (QueryGroupsZeroToSeven, QueryGroupsEightToFifteen)) for cmd in group_cmds)


# --- Controller wiring & priority ---------------------------------------


def _execute_control_task(device) -> ApplicationControllerTask:
    control = MagicMock()
    control.control_info.id = "ctrl"
    control.value_to_set = "v"
    return ApplicationControllerTask(ApplicationControllerTaskType.EXECUTE_CONTROL, (device, control))


@pytest.mark.asyncio
async def test_fetch_added_when_device_initializes():
    """On a successful (re)initialization the controller registers the device with the fetch
    scheduler - the same path serves first attempts and retries."""
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller._fetch_scheduler = MagicMock()
    controller._device_publisher = AsyncMock()
    controller._run_on_topic_handler = MagicMock()
    controller._refresh_group_virtual_devices = AsyncMock()
    controller._refresh_broadcast_device = AsyncMock()
    device = DaliDevice(DaliDeviceAddress(short=5, random=0x10), "gw_bus_1", MagicMock())
    controller._devices_by_mqtt_id = {device.mqtt_id: device}

    with patch("wb.mqtt_dali.application_controller.try_initialize_device", AsyncMock(return_value=True)):
        await controller._do_init_device(device.mqtt_id, 0.0)

    controller._fetch_scheduler.add_device.assert_called_once_with(device)


@pytest.mark.asyncio
async def test_polling_loop_calls_fetch_once_per_idle_iteration():
    """Each idle _poll_step performs exactly one fetch and advances the round-robin cursor."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=1.0)
    p1, p1_fetch = _stub_param(MaxLevelParam, fetch_return=False)
    p2, p2_fetch = _stub_param(MinLevelParam, fetch_return=False)
    device = _FakeFetchDevice([p1, p2])
    controller.dali_devices = [device]
    controller._fetch_scheduler.add_device(device)

    await controller._poll_step(0.0)
    assert p1_fetch.await_count + p2_fetch.await_count == 1

    await controller._poll_step(0.0)
    assert p1_fetch.await_count == 1
    assert p2_fetch.await_count == 1


@pytest.mark.asyncio
async def test_fetch_yields_to_due_control_poll():
    """A due control poll runs and fetch is skipped in that iteration."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=1.0)
    controller._poll_devices = AsyncMock()
    controller._fetch_scheduler = AsyncMock()
    device = MagicMock()
    device.is_initialized = True
    device.time_until_next_poll = MagicMock(return_value=0.0)
    controller.dali_devices = [device]
    controller._poll_scheduler.poll_turn = True

    await controller._poll_step(0.0)

    controller._poll_devices.assert_awaited()
    controller._fetch_scheduler.fetch_step.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_yields_to_queued_tasks():
    """While the task queue stays non-empty, the loop never reaches the idle fetch step."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=0.01)
    controller._fetch_scheduler = AsyncMock()
    device = MagicMock()

    async def _execute_and_refill(*_args, **_kwargs):
        if controller._tasks_queue.qsize() < 3:
            for _ in range(2):
                controller._tasks_queue.put_nowait(_execute_control_task(device))

    device.execute_control = AsyncMock(side_effect=_execute_and_refill)
    controller._tasks_queue.put_nowait(_execute_control_task(device))
    controller._tasks_queue.put_nowait(_execute_control_task(device))

    loop_task = asyncio.create_task(controller._polling_loop())
    try:
        await asyncio.sleep(0.2)
    finally:
        await stop_loop(controller, loop_task)

    assert device.execute_control.await_count >= 5
    controller._fetch_scheduler.fetch_step.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_suppressed_in_quiescent_mode():
    """No background fetch while the bus is in quiescent mode."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=0.01)
    controller._fetch_scheduler = AsyncMock()
    controller._in_quiescent_mode = True

    loop_task = asyncio.create_task(controller._polling_loop())
    try:
        await asyncio.sleep(0.05)
    finally:
        await stop_loop(controller, loop_task)

    controller._fetch_scheduler.fetch_step.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_suppressed_during_commissioning():
    """No background fetch while a commissioning task occupies the loop."""
    # pylint: disable=protected-access
    controller = make_loop_controller(polling_interval=0.01)
    controller._fetch_scheduler = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_commissioning():
        started.set()
        await release.wait()

    controller._run_commissioning_in_child_task = AsyncMock(side_effect=_blocking_commissioning)
    controller._tasks_queue.put_nowait(ApplicationControllerTask(ApplicationControllerTaskType.COMMISSIONING))

    loop_task = asyncio.create_task(controller._polling_loop())
    try:
        await asyncio.wait_for(started.wait(), 1.0)
        await asyncio.sleep(0.05)
        controller._fetch_scheduler.fetch_step.assert_not_awaited()
    finally:
        release.set()
        await stop_loop(controller, loop_task)


# --- add_device selection -----------------------------------------------


@pytest.mark.asyncio
async def test_add_device_filters_out_of_set_and_dedups():
    """add_device registers only FETCH_PARAM_CLASSES params and is idempotent on re-add (init retry)."""
    in_set, in_fetch = _stub_param(MaxLevelParam, fetch_return=True)
    out_param, out_fetch = _stub_param(GroupsParam, fetch_return=True)
    device = _FakeFetchDevice([in_set, out_param])
    scheduler = SettingsFetchScheduler()
    driver = AsyncMock()

    scheduler.add_device(device)
    scheduler.add_device(device)  # init retry must not duplicate entries

    await scheduler.fetch_step(driver, _LOGGER)

    assert scheduler.is_empty()  # exactly one entry (the in-set param), drained in one step
    in_fetch.assert_awaited_once()
    out_fetch.assert_not_awaited()  # out-of-set param was never registered


# --- controller remove wiring --------------------------------------------


def _removal_controller():
    """Bare controller with collaborators mocked, holding one registered DaliDevice."""
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller._fetch_scheduler = MagicMock()
    controller._device_registry = MagicMock()
    controller._device_publisher = AsyncMock()
    controller._refresh_group_virtual_devices = AsyncMock()
    controller._refresh_broadcast_device = AsyncMock()
    device = DaliDevice(DaliDeviceAddress(short=5, random=0x10), "gw_bus_1", MagicMock())
    controller.dali_devices = [device]
    controller._devices_by_mqtt_id = {device.mqtt_id: device}
    return controller, device


@pytest.mark.asyncio
async def test_controller_remove_device_drops_from_fetch():
    """_remove_device unregisters the device from the fetch scheduler."""
    # pylint: disable=protected-access
    controller, device = _removal_controller()

    await controller._remove_device(device)

    controller._fetch_scheduler.remove_device.assert_called_once_with(device)


@pytest.mark.asyncio
async def test_commissioning_sweep_removes_missing_from_fetch():
    """The commissioning reconciliation drops a now-missing device from the fetch scheduler."""
    # pylint: disable=protected-access
    controller, device = _removal_controller()

    await controller._update_dali_devices(CommissioningResult(missing=[device.address]))

    controller._fetch_scheduler.remove_device.assert_called_once_with(device)


@pytest.mark.asyncio
async def test_fetch_uses_live_address_after_short_change():
    """fetch_step derives the address live, so a short-address change after add_device is picked up."""
    param, fetch = _stub_param(MaxLevelParam, fetch_return=False)
    device = _FakeFetchDevice([param], short=3)
    scheduler = SettingsFetchScheduler()
    scheduler.add_device(device)
    driver = AsyncMock()

    device.address.short = 7  # e.g. via apply_parameters; the same device object stays registered
    await scheduler.fetch_step(driver, _LOGGER)

    fetch.assert_awaited_once_with(driver, GearShort(7), _LOGGER)
