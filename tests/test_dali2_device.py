from unittest.mock import AsyncMock

import pytest
from dali.address import DeviceShort

from wb.mqtt_dali.dali2_device import DeviceGroupsParam

# pylint: disable=protected-access


@pytest.mark.asyncio
async def test_device_groups_write_updates_expected_masks(monkeypatch):
    param = DeviceGroupsParam()

    initial_state = [False] * 32
    initial_state[0] = True  # will be removed
    initial_state[20] = True  # will be removed
    param._groups = initial_state.copy()

    desired_state = initial_state.copy()
    desired_state[0] = False  # remove lower-half bit
    desired_state[1] = True  # add lower-half bit
    desired_state[20] = False  # remove upper-half bit
    desired_state[25] = True  # add upper-half bit

    short_address = 7
    driver = AsyncMock()
    driver.send_commands.return_value = [None] * 4

    query_commands = ["query0", "query1", "query2", "query3"]
    monkeypatch.setattr(
        "wb.mqtt_dali.dali2_device.DeviceGroupsParam._build_query_commands",
        lambda self, address: query_commands,
    )
    monkeypatch.setattr(
        "wb.mqtt_dali.dali2_device.DeviceGroupsParam._parse_group_responses",
        lambda self, responses: desired_state,
    )

    monkeypatch.setattr("wb.mqtt_dali.dali2_device.DTR1", lambda value: f"DTR1:{value}")
    monkeypatch.setattr("wb.mqtt_dali.dali2_device.DTR2", lambda value: f"DTR2:{value}")
    monkeypatch.setattr(
        "wb.mqtt_dali.dali2_device.RemoveFromDeviceGroupsZeroToFifteen",
        lambda address: "REMOVE_LOWER",
    )
    monkeypatch.setattr(
        "wb.mqtt_dali.dali2_device.RemoveFromDeviceGroupsSixteenToThirtyOne",
        lambda address: "REMOVE_UPPER",
    )
    monkeypatch.setattr(
        "wb.mqtt_dali.dali2_device.AddToDeviceGroupsZeroToFifteen",
        lambda address: "ADD_LOWER",
    )
    monkeypatch.setattr(
        "wb.mqtt_dali.dali2_device.AddToDeviceGroupsSixteenToThirtyOne",
        lambda address: "ADD_UPPER",
    )

    result = await param.write(driver, short_address, {"device_groups": desired_state.copy()})

    driver.send_commands.assert_awaited_once()
    sent_commands = driver.send_commands.await_args.args[0]
    assert sent_commands == [
        "DTR1:1",
        "DTR2:0",
        "REMOVE_LOWER",
        "DTR1:16",
        "DTR2:0",
        "REMOVE_UPPER",
        "DTR1:2",
        "DTR2:0",
        "ADD_LOWER",
        "DTR1:0",
        "DTR2:2",
        "ADD_UPPER",
        *query_commands,
    ]
    assert result == {"device_groups": desired_state}
    assert param._groups == desired_state


def test_device_groups_builds_command_sequence(monkeypatch):
    param = DeviceGroupsParam()
    monkeypatch.setattr("wb.mqtt_dali.dali2_device.DTR1", lambda value: ("DTR1", value))
    monkeypatch.setattr("wb.mqtt_dali.dali2_device.DTR2", lambda value: ("DTR2", value))

    def fake_command(address):
        return ("CMD", address)

    address = DeviceShort(5)
    sequence = param._build_group_command_sequence(address, 0x1234, fake_command)

    assert sequence == [("DTR1", 0x34), ("DTR2", 0x12), ("CMD", address)]


@pytest.mark.asyncio
async def test_device_groups_write_no_changes_skips_driver_call():
    param = DeviceGroupsParam()
    current_state = [False] * 32
    current_state[5] = True
    param._groups = current_state.copy()

    driver = AsyncMock()
    result = await param.write(driver, 3, {"device_groups": current_state.copy()})

    assert result == {}
    driver.send_commands.assert_not_called()
