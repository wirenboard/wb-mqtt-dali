import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.device.general import Reset as DeviceReset
from dali.device.general import SetShortAddress as DeviceSetShortAddress
from dali.gear.general import Reset as GearReset
from dali.gear.general import SetShortAddress as GearSetShortAddress

from wb.mqtt_dali.application_controller import (
    ApplicationController,
    ApplicationControllerState,
)
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali2_device import Dali2Device
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.gateway import Gateway
from wb.mqtt_dali.wbdali_utils import AsyncDeviceInstanceTypeMapper

# pylint: disable=protected-access

# Prevent file system access inside DaliDeviceBase.__init__
DaliDeviceBase._common_schema = {"title": "test-schema"}


def _make_bare_controller():
    controller = ApplicationController.__new__(ApplicationController)
    controller.uid = "gw_bus_1"
    controller.logger = logging.getLogger("test")
    controller.dali_devices = []
    controller.dali2_devices = []
    controller._dali2_devices_by_addr = {}
    controller._devices_by_mqtt_id = {}
    controller._dev = AsyncMock()
    controller._dev_inst_map = MagicMock(mapping={})
    controller._gtin_db = MagicMock()
    controller._device_publisher = AsyncMock()
    controller._init_scheduler = MagicMock(remove=MagicMock(), schedule=MagicMock())
    controller._refresh_group_virtual_devices = AsyncMock()
    controller._refresh_broadcast_device = AsyncMock()
    controller._update_dali2_device_instance_map = MagicMock()
    controller._run_on_topic_handler = MagicMock()
    return controller


def _make_initialized_dali_device(short=5, random=0x123456, mqtt_id=None, name=None):
    device = DaliDevice(DaliDeviceAddress(short=short, random=random), "gw_bus_1", MagicMock(), mqtt_id, name)
    # mark as initialized so the test doesn't need to mock the DALI bus reads
    device.is_initialized = True
    device.types = []
    return device


def _make_initialized_dali2_device(short=3, random=0xABCDEF, mqtt_id=None, name=None):
    device = Dali2Device(
        DaliDeviceAddress(short=short, random=random), "gw_bus_1", MagicMock(), mqtt_id, name
    )
    device.is_initialized = True
    return device


# ---------------------------------------------------------------------------
# Scenario 1: ResetDeviceSettings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_device_settings_dali_sends_reset_and_reinitializes():
    controller = _make_bare_controller()
    device = _make_initialized_dali_device()
    controller.dali_devices = [device]
    controller._devices_by_mqtt_id[device.mqtt_id] = device

    sent_commands = []

    async def fake_send(_driver, cmd, *_args, **_kwargs):
        sent_commands.append(cmd)
        return MagicMock(raw_value=MagicMock(error=False))

    initialize_mock = AsyncMock()
    with patch("wb.mqtt_dali.application_controller.send_with_retry", side_effect=fake_send), patch.object(
        DaliDevice, "initialize", initialize_mock
    ), patch("wb.mqtt_dali.application_controller.publish_device", new=AsyncMock()) as publish_mock:
        await controller._reset_device_settings_task(device)

    # Reset is sent on the bus
    assert any(isinstance(c, GearReset) for c in sent_commands)
    # initialize() was called on the new (recreated) device
    initialize_mock.assert_awaited_once()
    # MQTT controls were republished
    controller._device_publisher.remove_device.assert_awaited_once_with(device.mqtt_id)
    publish_mock.assert_awaited_once()
    # The new device replaced the old one in dali_devices (same short address)
    assert len(controller.dali_devices) == 1
    new_device = controller.dali_devices[0]
    assert new_device is not device
    assert new_device.address.short == device.address.short
    assert new_device.address.random == device.address.random
    assert new_device.mqtt_id == device.mqtt_id
    assert new_device.name == device.name
    # Aggregated virtual devices were refreshed (DALI 1 only)
    controller._refresh_group_virtual_devices.assert_awaited_once()
    controller._refresh_broadcast_device.assert_awaited_once()
    # Scenario 1 must not touch the init scheduler: short address and mqtt_id are unchanged
    controller._init_scheduler.remove.assert_not_called()
    controller._init_scheduler.schedule.assert_not_called()


@pytest.mark.asyncio
async def test_reset_device_settings_dali2_sends_reset_and_reinitializes():
    controller = _make_bare_controller()
    device = _make_initialized_dali2_device()
    controller.dali2_devices = [device]
    controller._dali2_devices_by_addr = {device.address.short: device}
    controller._devices_by_mqtt_id[device.mqtt_id] = device

    sent_commands = []

    async def fake_send(_driver, cmd, *_args, **_kwargs):
        sent_commands.append(cmd)
        return MagicMock(raw_value=MagicMock(error=False))

    initialize_mock = AsyncMock()
    with patch("wb.mqtt_dali.application_controller.send_with_retry", side_effect=fake_send), patch.object(
        Dali2Device, "initialize", initialize_mock
    ), patch("wb.mqtt_dali.application_controller.publish_device", new=AsyncMock()):
        await controller._reset_device_settings_task(device)

    assert any(isinstance(c, DeviceReset) for c in sent_commands)
    initialize_mock.assert_awaited_once()
    # DALI 2 update path uses dali2_devices_by_addr and instance map
    assert len(controller.dali2_devices) == 1
    new_device = controller.dali2_devices[0]
    assert new_device is not device
    assert controller._dali2_devices_by_addr[device.address.short] is new_device
    controller._update_dali2_device_instance_map.assert_called_once_with(new_device)
    # No DALI 1 aggregate refresh for DALI 2 devices
    controller._refresh_group_virtual_devices.assert_not_called()
    controller._refresh_broadcast_device.assert_not_called()


@pytest.mark.asyncio
async def test_reset_device_settings_does_not_force_reload_params():
    controller = _make_bare_controller()
    device = _make_initialized_dali_device()
    # Stub out previous params/schema as if loaded once before
    device.params = {"name": "old"}
    device.schema = {"title": "old"}
    controller.dali_devices = [device]
    controller._devices_by_mqtt_id[device.mqtt_id] = device

    load_info_mock = AsyncMock()
    initialize_mock = AsyncMock()
    with patch(
        "wb.mqtt_dali.application_controller.send_with_retry",
        new=AsyncMock(return_value=MagicMock(raw_value=MagicMock(error=False))),
    ), patch.object(DaliDevice, "initialize", initialize_mock), patch.object(
        DaliDevice, "load_info", load_info_mock
    ), patch(
        "wb.mqtt_dali.application_controller.publish_device", new=AsyncMock()
    ):
        await controller._reset_device_settings_task(device)

    # ResetDeviceSettings runs initialize() but never load_info()
    initialize_mock.assert_awaited_once()
    load_info_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_device_settings_propagates_reset_failure_without_reinit():
    controller = _make_bare_controller()
    device = _make_initialized_dali_device()
    controller.dali_devices = [device]
    controller._devices_by_mqtt_id[device.mqtt_id] = device

    initialize_mock = AsyncMock()
    with patch(
        "wb.mqtt_dali.application_controller.send_with_retry",
        new=AsyncMock(side_effect=RuntimeError("bus down")),
    ), patch.object(DaliDevice, "initialize", initialize_mock), patch(
        "wb.mqtt_dali.application_controller.publish_device", new=AsyncMock()
    ) as publish_mock:
        with pytest.raises(RuntimeError, match="bus down"):
            await controller._reset_device_settings_task(device)

    initialize_mock.assert_not_awaited()
    publish_mock.assert_not_awaited()
    controller._device_publisher.remove_device.assert_not_awaited()
    # Original device still in place
    assert controller.dali_devices == [device]


@pytest.mark.asyncio
async def test_reset_device_settings_propagates_init_failure_without_republish():
    controller = _make_bare_controller()
    device = _make_initialized_dali_device()
    controller.dali_devices = [device]
    controller._devices_by_mqtt_id[device.mqtt_id] = device

    with patch(
        "wb.mqtt_dali.application_controller.send_with_retry",
        new=AsyncMock(return_value=MagicMock(raw_value=MagicMock(error=False))),
    ), patch.object(DaliDevice, "initialize", AsyncMock(side_effect=RuntimeError("init failed"))), patch(
        "wb.mqtt_dali.application_controller.publish_device", new=AsyncMock()
    ) as publish_mock:
        with pytest.raises(RuntimeError, match="init failed"):
            await controller._reset_device_settings_task(device)

    publish_mock.assert_not_awaited()
    controller._device_publisher.remove_device.assert_not_awaited()
    assert controller.dali_devices == [device]


# ---------------------------------------------------------------------------
# Scenario 2: ResetDevice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_device_dali_sends_reset_and_set_short_mask_and_removes():
    controller = _make_bare_controller()
    device = _make_initialized_dali_device()
    controller.dali_devices = [device]
    controller._devices_by_mqtt_id[device.mqtt_id] = device

    captured: list = []

    async def fake_send_commands(_driver, commands, *_args, **_kwargs):
        captured.append(list(commands))
        return [MagicMock(raw_value=MagicMock(error=False)) for _ in commands]

    with patch(
        "wb.mqtt_dali.application_controller.send_commands_with_retry", side_effect=fake_send_commands
    ):
        await controller._reset_device_task(device)

    assert len(captured) == 1
    sent = captured[0]
    assert any(isinstance(c, GearReset) for c in sent)
    assert any(isinstance(c, GearSetShortAddress) for c in sent)
    # device removed from active configuration
    assert controller.dali_devices == []
    assert device.mqtt_id not in controller._devices_by_mqtt_id
    controller._device_publisher.remove_device.assert_awaited_once_with(device.mqtt_id)
    controller._init_scheduler.remove.assert_called_once_with(device.mqtt_id)
    # virtual aggregated devices refreshed for DALI 1
    controller._refresh_group_virtual_devices.assert_awaited_once()
    controller._refresh_broadcast_device.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_device_dali2_sends_reset_and_clears_addr_maps():
    controller = _make_bare_controller()
    device = _make_initialized_dali2_device()
    controller.dali2_devices = [device]
    controller._dali2_devices_by_addr = {device.address.short: device}
    controller._devices_by_mqtt_id[device.mqtt_id] = device
    # populate _dev_inst_map for this short address using a real mapper so that
    # remove_short_address() actually mutates the underlying dict
    other_short = (device.address.short + 1) % 64
    controller._dev_inst_map = AsyncDeviceInstanceTypeMapper(
        initial={
            (device.address.short, 0): 1,
            (device.address.short, 1): 2,
            (other_short, 0): 3,
        }
    )

    captured: list = []

    async def fake_send_commands(_driver, commands, *_args, **_kwargs):
        captured.append(list(commands))
        return [MagicMock(raw_value=MagicMock(error=False)) for _ in commands]

    with patch(
        "wb.mqtt_dali.application_controller.send_commands_with_retry", side_effect=fake_send_commands
    ):
        await controller._reset_device_task(device)

    sent = captured[0]
    assert any(isinstance(c, DeviceReset) for c in sent)
    assert any(isinstance(c, DeviceSetShortAddress) for c in sent)
    # internal maps no longer reference the removed device
    assert controller.dali2_devices == []
    assert device.address.short not in controller._dali2_devices_by_addr
    assert (device.address.short, 0) not in controller._dev_inst_map.mapping
    assert (device.address.short, 1) not in controller._dev_inst_map.mapping
    # entries for other short addresses are kept untouched
    assert (other_short, 0) in controller._dev_inst_map.mapping
    assert device.mqtt_id not in controller._devices_by_mqtt_id
    controller._device_publisher.remove_device.assert_awaited_once_with(device.mqtt_id)
    # No DALI 1 aggregate refresh for DALI 2 devices
    controller._refresh_group_virtual_devices.assert_not_called()
    controller._refresh_broadcast_device.assert_not_called()


@pytest.mark.asyncio
async def test_reset_device_propagates_failure_without_removal():
    controller = _make_bare_controller()
    device = _make_initialized_dali_device()
    controller.dali_devices = [device]
    controller._devices_by_mqtt_id[device.mqtt_id] = device

    with patch(
        "wb.mqtt_dali.application_controller.send_commands_with_retry",
        new=AsyncMock(side_effect=RuntimeError("bus down")),
    ):
        with pytest.raises(RuntimeError, match="bus down"):
            await controller._reset_device_task(device)

    # Device stays in active configuration on failure
    assert controller.dali_devices == [device]
    assert controller._devices_by_mqtt_id[device.mqtt_id] is device
    controller._device_publisher.remove_device.assert_not_awaited()
    controller._init_scheduler.remove.assert_not_called()


# ---------------------------------------------------------------------------
# Public API state checks
# ---------------------------------------------------------------------------


def _make_controller_with_state(state):
    controller = _make_bare_controller()
    controller._state = state
    controller._state_lock = asyncio.Lock()
    controller._tasks_queue = asyncio.Queue()
    return controller


@pytest.mark.asyncio
async def test_reset_device_settings_raises_when_not_ready():
    controller = _make_controller_with_state(ApplicationControllerState.UNINITIALIZED)
    device = _make_initialized_dali_device()

    with pytest.raises(RuntimeError):
        await controller.reset_device_settings(device)
    # No bus traffic was attempted
    controller._dev.send.assert_not_called()


@pytest.mark.asyncio
async def test_reset_device_raises_when_not_ready():
    controller = _make_controller_with_state(ApplicationControllerState.UNINITIALIZED)
    device = _make_initialized_dali_device()

    with pytest.raises(RuntimeError):
        await controller.reset_device(device)
    controller._dev.send.assert_not_called()


# ---------------------------------------------------------------------------
# Gateway RPC handlers
# ---------------------------------------------------------------------------


def _make_bare_gateway_with_device(device, bus):
    gateway = Gateway.__new__(Gateway)
    gateway._save_configuration = AsyncMock()
    gateway._get_bus_and_device_by_id = MagicMock(return_value=(bus, device))
    return gateway


@pytest.mark.asyncio
async def test_reset_device_settings_rpc_calls_bus_and_returns_empty():
    bus = SimpleNamespace(reset_device_settings=AsyncMock())
    device = _make_initialized_dali_device()
    gateway = _make_bare_gateway_with_device(device, bus)

    result = await gateway.reset_device_settings_rpc_handler({"deviceId": device.uid})

    bus.reset_device_settings.assert_awaited_once_with(device)
    gateway._save_configuration.assert_not_called()
    assert result == {}


@pytest.mark.asyncio
async def test_reset_device_rpc_calls_bus_and_saves_configuration():
    bus = SimpleNamespace(reset_device=AsyncMock())
    device = _make_initialized_dali_device()
    gateway = _make_bare_gateway_with_device(device, bus)

    result = await gateway.reset_device_rpc_handler({"deviceId": device.uid})

    bus.reset_device.assert_awaited_once_with(device)
    gateway._save_configuration.assert_awaited_once()
    assert result == {}


@pytest.mark.asyncio
async def test_reset_device_settings_rpc_missing_device_id_raises():
    gateway = Gateway.__new__(Gateway)
    with pytest.raises(ValueError, match="deviceId"):
        await gateway.reset_device_settings_rpc_handler({})


@pytest.mark.asyncio
async def test_reset_device_rpc_missing_device_id_raises():
    gateway = Gateway.__new__(Gateway)
    with pytest.raises(ValueError, match="deviceId"):
        await gateway.reset_device_rpc_handler({})


@pytest.mark.asyncio
async def test_reset_device_settings_rpc_unknown_device_raises_not_found():
    gateway = Gateway.__new__(Gateway)
    gateway._get_bus_and_device_by_id = MagicMock(return_value=(None, None))

    with pytest.raises(ValueError, match="not found"):
        await gateway.reset_device_settings_rpc_handler({"deviceId": "missing"})


@pytest.mark.asyncio
async def test_reset_device_rpc_unknown_device_raises_not_found():
    gateway = Gateway.__new__(Gateway)
    gateway._get_bus_and_device_by_id = MagicMock(return_value=(None, None))
    gateway._save_configuration = AsyncMock()

    with pytest.raises(ValueError, match="not found"):
        await gateway.reset_device_rpc_handler({"deviceId": "missing"})
    gateway._save_configuration.assert_not_called()


@pytest.mark.asyncio
async def test_reset_device_rpc_does_not_save_when_bus_call_fails():
    bus = SimpleNamespace(reset_device=AsyncMock(side_effect=RuntimeError("bus down")))
    device = _make_initialized_dali_device()
    gateway = _make_bare_gateway_with_device(device, bus)

    with pytest.raises(RuntimeError, match="bus down"):
        await gateway.reset_device_rpc_handler({"deviceId": device.uid})

    gateway._save_configuration.assert_not_called()


# ---------------------------------------------------------------------------
# Editor endpoints registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_endpoints_registered_on_start():
    """Verify both reset endpoints are registered with the RPC server during start()."""
    gateway = Gateway.__new__(Gateway)
    gateway.rpc_server = MagicMock()
    gateway.rpc_server.start = AsyncMock()
    gateway.rpc_server.add_endpoint = AsyncMock()
    gateway.wb_dali_gateways = []
    gateway._mqtt_dispatcher = AsyncMock()
    gateway._update_gateways = AsyncMock()
    gateway._gtin_db = MagicMock()

    with patch("wb.mqtt_dali.gateway.remove_topics_by_driver", new=AsyncMock()), patch(
        "wb.mqtt_dali.gateway.wait_for_rpc_endpoint", new=AsyncMock()
    ):
        await gateway.start()

    registered = {
        (call_args.args[0], call_args.args[1])
        for call_args in gateway.rpc_server.add_endpoint.await_args_list
    }
    assert ("Editor", "ResetDeviceSettings") in registered
    assert ("Editor", "ResetDevice") in registered
