"""The seams a host other than the controller needs — no monkeypatching required.

The WASM device editor used to reach these by substituting module attributes
and poking private state; each has a public counterpart now.
"""

import asyncio
import json
import logging
from timeit import default_timer
from unittest.mock import AsyncMock, MagicMock

import pytest

from wb.mqtt_dali.application_controller import (
    ApplicationController,
    ApplicationControllerConfig,
    ApplicationControllerState,
    CommissioningStatus,
)
from wb.mqtt_dali.common_dali_device import (
    DATA_DIR_ENV,
    DaliDeviceAddress,
    DaliDeviceBase,
    data_dir,
)
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.device_init_scheduler import DeviceInitScheduler
from wb.mqtt_dali.gateway import Gateway

from ._app_controller_helpers import make_loop_controller


def _controller(driver_factory):
    return ApplicationController(
        ApplicationControllerConfig("gw", 1, [], []), MagicMock(), MagicMock(), driver_factory=driver_factory
    )


def test_a_host_hands_in_its_own_driver_through_the_factory():
    """The factory receives the same arguments the default construction uses."""
    driver = MagicMock()
    factory = MagicMock(return_value=driver)
    controller = _controller(factory)
    assert controller.driver is driver
    config, dispatcher, logger, dev_inst_map = factory.call_args.args
    assert (config.device_name, config.bus) == ("gw", 1)
    assert dispatcher is not None and logger is controller.logger and dev_inst_map is not None


def test_the_monitor_flag_and_the_bus_population_reach_the_driver():
    driver = MagicMock()
    controller = _controller(lambda *_: driver)
    controller.set_bus_monitor_enabled(True)
    driver.set_bus_monitor_enabled.assert_called_with(True)
    controller._notify_bus_population()  # pylint: disable=protected-access
    driver.set_has_control_devices.assert_called_with(False)


@pytest.mark.asyncio
async def test_first_init_attempts_are_awaitable():
    """With every configured device tried once — here: none configured — the wait ends."""
    # pylint: disable=protected-access
    controller = make_loop_controller()
    controller._init_scheduler = DeviceInitScheduler()
    controller._first_attempts_done = asyncio.Event()
    controller._init_scheduler.schedule("dev", default_timer())
    controller._devices_by_mqtt_id = {}  # the entry is dropped as unknown on its first attempt
    await controller._poll_step(default_timer())
    await controller._poll_step(default_timer())
    await asyncio.wait_for(controller.wait_first_init_attempts(), 1.0)


def test_config_listeners_hear_every_write(tmp_path):
    # pylint: disable=protected-access
    gateway = Gateway.__new__(Gateway)
    gateway._config_listeners = []
    gateway._config_path = str(tmp_path / "wb-mqtt-dali.conf")
    gateway._debug = False
    gateway.wb_dali_gateways = []
    heard = []
    unregister = gateway.on_config_saved(lambda: heard.append(1))
    gateway._write_configuration()
    assert heard == [1]
    assert json.loads((tmp_path / "wb-mqtt-dali.conf").read_text())["gateways"] == []
    unregister()
    gateway._write_configuration()
    assert heard == [1]


@pytest.mark.asyncio
async def test_commissioning_is_awaitable_per_bus():
    # pylint: disable=protected-access
    gateway = Gateway.__new__(Gateway)
    gateway._commissioning_finished = {}
    await asyncio.wait_for(gateway.wait_commissioning("bus"), 0.1)  # nothing running: returns at once
    gateway._note_commissioning_state("bus", CommissioningStatus.QUEUED)
    waiter = asyncio.ensure_future(gateway.wait_commissioning("bus"))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    gateway._note_commissioning_state("bus", CommissioningStatus.COMPLETED)
    await asyncio.wait_for(waiter, 0.1)


def test_group_membership_can_be_seeded_before_the_first_read():
    DaliDeviceBase.set_common_schema({"title": "test-schema"})
    device = DaliDevice(DaliDeviceAddress(short=1, random=0), "bus", MagicMock())
    assert device.groups == set()
    device.seed_groups([1, 5, 40])  # 40 is not a DALI group
    assert device.groups == {1, 5}


def test_the_data_dir_can_be_relocated(monkeypatch, tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "common_device.schema.json").write_text(json.dumps({"title": "relocated"}))
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    assert data_dir() == str(tmp_path)
    DaliDeviceBase.set_common_schema({})
    DaliDevice(DaliDeviceAddress(short=1, random=0), "bus", MagicMock())
    assert DaliDeviceBase._common_schema == {"title": "relocated"}  # pylint: disable=protected-access
    DaliDeviceBase.set_common_schema({"title": "test-schema"})


@pytest.mark.asyncio
async def test_start_hands_the_driver_the_configured_monitor_state():
    """A restored config with the monitor on (and no DALI-2 devices) must reach
    the driver at startup, not only when someone later flips the toggle."""
    driver = MagicMock()
    driver.initialize = AsyncMock()
    controller = ApplicationController.__new__(ApplicationController)
    controller.logger = logging.getLogger("test.host_apis.start")
    controller._dev = driver  # pylint: disable=protected-access
    controller._bus_monitor_enabled = True  # pylint: disable=protected-access
    controller._state = ApplicationControllerState.UNINITIALIZED  # pylint: disable=protected-access
    controller._state_lock = asyncio.Lock()  # pylint: disable=protected-access
    controller._device_publisher = MagicMock()  # pylint: disable=protected-access
    controller._device_publisher.initialize = AsyncMock()  # pylint: disable=protected-access
    controller._publish_virtual_device = AsyncMock()  # pylint: disable=protected-access
    controller._broadcast_device = MagicMock(mqtt_id="bcast")  # pylint: disable=protected-access
    controller.dali_devices = []
    controller.dali2_devices = []
    controller._devices_by_mqtt_id = {}  # pylint: disable=protected-access
    controller._init_scheduler = MagicMock()  # pylint: disable=protected-access
    controller._polling_loop = MagicMock(return_value=_never())  # pylint: disable=protected-access
    await controller.start()
    try:
        driver.set_bus_monitor_enabled.assert_called_once_with(True)
        driver.set_has_control_devices.assert_called_once_with(False)
    finally:
        controller._polling_task.cancel()  # pylint: disable=protected-access


async def _never():
    await asyncio.Event().wait()
