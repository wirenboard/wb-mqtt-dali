import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wb.mqtt_dali.application_controller import ApplicationController
from wb.mqtt_dali.commissioning import ChangedDevice, CommissioningResult
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer

# pylint: disable=protected-access

# Prevent file system access inside DaliDeviceBase.__init__
DaliDeviceBase._common_schema = {"title": "test-schema"}


class TestApplicationControllerVirtualGroups:  # pylint: disable=too-few-public-methods
    def test_get_active_group_numbers(self):
        controller = ApplicationController.__new__(ApplicationController)
        controller.dali_devices = [
            SimpleNamespace(groups=set([0, 2])),
            SimpleNamespace(groups=set([1])),
            SimpleNamespace(groups=set([2])),
        ]

        assert getattr(controller, "_get_active_group_numbers")() == [0, 1, 2]


def _make_bare_controller():
    controller = ApplicationController.__new__(ApplicationController)
    controller.uid = "gw_bus_1"
    controller.logger = logging.getLogger("test")
    controller._dev = AsyncMock()
    controller._gtin_db = MagicMock()
    controller._devices_by_mqtt_id = {}
    controller._init_scheduler = MagicMock()
    controller._init_scheduler.remove = MagicMock()
    controller._init_scheduler.schedule = MagicMock()
    controller._device_publisher = AsyncMock()
    controller._try_init_new_device = AsyncMock()
    controller._refresh_group_virtual_devices = AsyncMock()
    controller._refresh_broadcast_device = AsyncMock()
    controller.dali_devices = []
    controller.dali2_devices = []
    controller._dali2_devices_by_addr = {}
    return controller


@pytest.mark.asyncio
async def test_resolve_initial_names_formats_known_product():
    controller = _make_bare_controller()
    compat = DaliCommandsCompatibilityLayer()
    addresses = [DaliDeviceAddress(short=3, random=0x1234)]

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="LED Driver"),
    ):
        names = await controller._resolve_initial_names(addresses, compat)

    assert names == ["LED Driver 3"]


@pytest.mark.asyncio
async def test_resolve_initial_names_returns_none_for_unknown_product():
    controller = _make_bare_controller()
    compat = DaliCommandsCompatibilityLayer()
    addresses = [DaliDeviceAddress(short=7, random=0x22)]

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value=None),
    ):
        names = await controller._resolve_initial_names(addresses, compat)

    assert names == [None]


@pytest.mark.asyncio
async def test_resolve_initial_names_returns_none_on_exception():
    controller = _make_bare_controller()
    compat = DaliCommandsCompatibilityLayer()
    addresses = [DaliDeviceAddress(short=2, random=0x00)]

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(side_effect=RuntimeError("bus down")),
    ):
        names = await controller._resolve_initial_names(addresses, compat)

    assert names == [None]


@pytest.mark.asyncio
async def test_resolve_initial_names_handles_empty_input():
    controller = _make_bare_controller()
    compat = DaliCommandsCompatibilityLayer()

    with patch("wb.mqtt_dali.application_controller.read_product_name") as mock_rpn:
        names = await controller._resolve_initial_names([], compat)
        mock_rpn.assert_not_called()

    assert names == []


@pytest.mark.asyncio
async def test_update_dali_devices_sets_custom_name_for_new_with_known_gtin():
    controller = _make_bare_controller()

    new_addr = DaliDeviceAddress(short=5, random=0xABCD)
    commissioning_result = CommissioningResult(new=[new_addr])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="Acme Lamp"),
    ):
        await controller._update_dali_devices(commissioning_result)

    assert len(controller.dali_devices) == 1
    device = controller.dali_devices[0]
    assert device.address.short == 5
    assert device.name == "Acme Lamp 5"
    assert device.has_custom_name is True


@pytest.mark.asyncio
async def test_update_dali_devices_uses_default_name_for_unknown_gtin():
    controller = _make_bare_controller()

    new_addr = DaliDeviceAddress(short=8, random=0x01)
    commissioning_result = CommissioningResult(new=[new_addr])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value=None),
    ):
        await controller._update_dali_devices(commissioning_result)

    assert len(controller.dali_devices) == 1
    device = controller.dali_devices[0]
    assert device.name == "DALI 8"
    assert device.has_custom_name is False


@pytest.mark.asyncio
async def test_update_dali_devices_sets_custom_name_for_changed_device():
    controller = _make_bare_controller()

    # Simulate a previously-known device at short 2 that is now a replacement
    old_device = SimpleNamespace(
        address=DaliDeviceAddress(short=2, random=0xAA),
        mqtt_id="gw_bus_1_2",
    )
    controller.dali_devices = [old_device]

    changed = ChangedDevice(
        new=DaliDeviceAddress(short=2, random=0xBB),
        old_short=2,
    )
    commissioning_result = CommissioningResult(changed=[changed])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="Replacement Driver"),
    ):
        await controller._update_dali_devices(commissioning_result)

    # Old device is replaced by a new one carrying the resolved name
    assert len(controller.dali_devices) == 1
    device = controller.dali_devices[0]
    assert device.address.short == 2
    assert device.address.random == 0xBB
    assert device.name == "Replacement Driver 2"
    assert device.has_custom_name is True


@pytest.mark.asyncio
async def test_update_dali2_devices_sets_custom_name_for_new_with_known_gtin():
    controller = _make_bare_controller()

    new_addr = DaliDeviceAddress(short=4, random=0x55)
    commissioning_result = CommissioningResult(new=[new_addr])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="Sensor Pro"),
    ):
        await controller._update_dali2_devices(commissioning_result)

    assert len(controller.dali2_devices) == 1
    device = controller.dali2_devices[0]
    assert device.address.short == 4
    assert device.name == "Sensor Pro 4"
    assert device.has_custom_name is True


@pytest.mark.asyncio
async def test_update_dali2_devices_sets_custom_name_for_changed_device():
    controller = _make_bare_controller()

    # Simulate a previously-known DALI2 device at short 6 that is now a replacement
    old_device = SimpleNamespace(
        address=DaliDeviceAddress(short=6, random=0x11),
        mqtt_id="gw_bus_1_d2_6",
    )
    controller.dali2_devices = [old_device]

    changed = ChangedDevice(
        new=DaliDeviceAddress(short=6, random=0x22),
        old_short=6,
    )
    commissioning_result = CommissioningResult(changed=[changed])

    with patch(
        "wb.mqtt_dali.application_controller.read_product_name",
        new=AsyncMock(return_value="Replacement Sensor"),
    ):
        await controller._update_dali2_devices(commissioning_result)

    # Old device is replaced by a new one carrying the resolved name
    assert len(controller.dali2_devices) == 1
    device = controller.dali2_devices[0]
    assert device.address.short == 6
    assert device.address.random == 0x22
    assert device.name == "Replacement Sensor 6"
    assert device.has_custom_name is True
