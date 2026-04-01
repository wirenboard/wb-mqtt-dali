import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.address import DeviceShort, GearShort
from dali.device.general import IdentifyDevice as DeviceIdentifyDevice
from dali.gear.general import IdentifyDevice as GearIdentifyDevice
from dali.gear.general import RecallMaxLevel, RecallMinLevel

from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali2_device import Dali2Device
from wb.mqtt_dali.dali_device import DaliDevice

DaliDeviceBase._common_schema = {"title": "test-schema"}


def make_version_response(version_byte):
    resp = MagicMock()
    resp.value = version_byte
    resp.raw_value = MagicMock()
    resp.raw_value.error = False
    resp.raw_value.as_integer = version_byte
    return resp


def make_dali_device():
    return DaliDevice(DaliDeviceAddress(short=5, random=0x123456), "bus_1", MagicMock())


def make_dali2_device():
    return Dali2Device(DaliDeviceAddress(short=3, random=0xABCDEF), "bus_1", MagicMock())


@pytest.mark.asyncio
async def test_dali_device_identify_sends_command():
    device = make_dali_device()
    driver = AsyncMock()
    driver.send = AsyncMock(side_effect=[make_version_response(0x0C), None])

    await device.identify(driver)

    assert driver.send.call_count == 2
    _, second_cmd = [call.args[0] for call in driver.send.call_args_list]
    assert isinstance(second_cmd, GearIdentifyDevice)
    assert second_cmd.destination == GearShort(5)


@pytest.mark.asyncio
async def test_dali_device_identify_any_version_sends_command():
    device = make_dali_device()
    driver = AsyncMock()
    driver.send = AsyncMock(side_effect=[make_version_response(0x08), None])  # version 2.0

    await device.identify(driver)

    assert driver.send.call_count == 2
    _, second_cmd = [call.args[0] for call in driver.send.call_args_list]
    assert isinstance(second_cmd, GearIdentifyDevice)
    assert second_cmd.destination == GearShort(5)


@pytest.mark.asyncio
async def test_dali_device_identify_no_version_response_blinks():
    device = make_dali_device()
    driver = AsyncMock()
    driver.send = AsyncMock(return_value=None)

    orig_sleep = asyncio.sleep
    asyncio.sleep = AsyncMock(return_value=None)
    try:
        await device.identify(driver)
    finally:
        asyncio.sleep = orig_sleep

    # restore later if needed: asyncio.sleep = orig_sleep
    # 2 retries of QueryVersionNumber, then blink sequence
    sent_types = [type(c.args[0]) for c in driver.send.call_args_list[2:]]
    assert sent_types.count(RecallMaxLevel) == 5
    assert sent_types.count(RecallMinLevel) == 5
    assert sent_types[-1].__name__ == "Terminate"


@pytest.mark.asyncio
async def test_dali_device_identify_yes_version_response_blinks():
    # Device returns YES (value=1) to QueryVersionNumber — treat as old gear
    device = make_dali_device()
    driver = AsyncMock()
    driver.send = AsyncMock(return_value=make_version_response(1))

    await device.identify(driver)

    # 2 retries of QueryVersionNumber, then blink sequence
    sent_types = [type(c.args[0]) for c in driver.send.call_args_list[2:]]
    assert sent_types.count(RecallMaxLevel) == 5
    assert sent_types.count(RecallMinLevel) == 5
    assert sent_types[-1].__name__ == "Terminate"


@pytest.mark.asyncio
async def test_dali2_device_identify_sends_command():
    device = make_dali2_device()
    driver = AsyncMock()

    await device.identify(driver)

    driver.send.assert_awaited_once()
    cmd = driver.send.call_args.args[0]
    assert isinstance(cmd, DeviceIdentifyDevice)
    assert cmd.destination == DeviceShort(3)
