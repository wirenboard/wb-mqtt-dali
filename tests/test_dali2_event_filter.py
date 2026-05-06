from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.address import (
    DeviceBroadcast,
    DeviceGroup,
    DeviceShort,
    GearBroadcast,
    GearGroup,
    InstanceNumber,
)
from dali.device import light, occupancy, pushbutton
from dali.device.general import DTR0, QueryEventFilterZeroToSeven, SetEventFilter

from wb.mqtt_dali.dali2_device import InstanceParameters
from wb.mqtt_dali.dali2_type1_parameters import EventFilterParam
from wb.mqtt_dali.device import absolute_input_device, feedback, general_purpose_sensor

# pylint: disable=protected-access


def _bits_from_mask(mask: int) -> dict:
    return {
        bit_name: bool(mask & (1 << index))
        for index, (bit_name, *_) in enumerate(EventFilterParam.BIT_DEFINITIONS)
    }


def _factory_mask() -> int:
    return sum(1 << index for index, (*_, default) in enumerate(EventFilterParam.BIT_DEFINITIONS) if default)


def test_event_filter_param_pushbutton_schema_has_eight_switches():
    param = EventFilterParam(InstanceNumber(0))
    schema = param.get_schema(group_and_broadcast=False)

    assert "event_filter" in schema["properties"]
    card = schema["properties"]["event_filter"]
    assert card["title"] == "Event filter"
    assert card["format"] == "card"
    bit_props = card["properties"]
    expected_bits = [
        ("button_released", False),
        ("button_pressed", False),
        ("short_press", True),
        ("double_press", False),
        ("long_press_start", True),
        ("long_press_repeat", True),
        ("long_press_stop", True),
        ("button_stuck_free", True),
    ]
    assert list(bit_props.keys()) == [name for name, _ in expected_bits]
    for bit_name, default in expected_bits:
        prop = bit_props[bit_name]
        assert prop["type"] == "boolean"
        assert prop["format"] == "switch"
        assert prop["default"] is default

    ru = schema["translations"]["ru"]
    assert ru["Event filter"] == "Фильтр событий"
    assert ru["Short press"] == "Короткое нажатие"
    assert ru["Button stuck/free"] == "Кнопка залипла/освободилась"

    # Defaults assemble back to the factory mask computed from BIT_DEFINITIONS.
    assembled = 0
    for index, (name, _) in enumerate(expected_bits):
        if bit_props[name]["default"]:
            assembled |= 1 << index
    assert assembled == _factory_mask()


@pytest.mark.asyncio
async def test_event_filter_param_read_pushbutton_assembles_byte():
    param = EventFilterParam(InstanceNumber(2))
    driver = AsyncMock()
    response = MagicMock()
    # Mask: button_pressed (0x02) + short_press (0x04) + long_press_repeat (0x20) = 0x26
    response.raw_value.as_integer = 0x26
    response.raw_value.error = False
    driver.send.return_value = response

    short_address = DeviceShort(5)
    result = await param.read(driver, short_address)

    driver.send.assert_awaited_once()
    sent_cmd = driver.send.await_args.args[0]
    assert isinstance(sent_cmd, QueryEventFilterZeroToSeven)
    assert param.value == 0x26
    assert result == {
        "event_filter": {
            "button_released": False,
            "button_pressed": True,
            "short_press": True,
            "double_press": False,
            "long_press_start": False,
            "long_press_repeat": True,
            "long_press_stop": False,
            "button_stuck_free": False,
        }
    }


@pytest.mark.asyncio
async def test_event_filter_param_read_raises_when_device_unresponsive(monkeypatch):
    param = EventFilterParam(InstanceNumber(1))
    driver = AsyncMock()

    async def fake_query_int(*_args, **_kwargs):
        raise RuntimeError("no response")

    monkeypatch.setattr("wb.mqtt_dali.dali2_type1_parameters.query_int", fake_query_int)

    with pytest.raises(RuntimeError):
        await param.read(driver, DeviceShort(7))

    assert param.value is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [DeviceBroadcast(), GearBroadcast(), GearGroup(3), DeviceGroup(2)],
)
async def test_event_filter_param_read_for_group_address_returns_empty_without_io(address):
    param = EventFilterParam(InstanceNumber(0))
    driver = AsyncMock()

    result = await param.read(driver, address)

    driver.send.assert_not_called()
    driver.send_commands.assert_not_called()
    assert result == {}
    assert param.value is None


@pytest.mark.asyncio
async def test_event_filter_param_write_packs_dtr0_and_sends_set_event_filter():
    param = EventFilterParam(InstanceNumber(4))
    param.value = 0x00  # current state different from new value to force write

    new_mask = 0x14  # short_press (0x04) + long_press_start (0x10)
    new_value = {"event_filter": _bits_from_mask(new_mask)}

    driver = AsyncMock()
    set_resp = MagicMock()
    set_resp.raw_value.error = False
    set_resp.raw_value.as_integer = 0
    dtr_resp = MagicMock()
    dtr_resp.raw_value.error = False
    dtr_resp.raw_value.as_integer = 0
    query_resp = MagicMock()
    query_resp.raw_value.error = False
    query_resp.raw_value.as_integer = new_mask
    driver.send_commands.return_value = [dtr_resp, set_resp, query_resp]

    short_address = DeviceShort(11)
    result = await param.write(driver, short_address, new_value)

    driver.send_commands.assert_awaited_once()
    sent_commands = driver.send_commands.await_args.args[0]
    assert len(sent_commands) == 3
    assert isinstance(sent_commands[0], DTR0)
    assert sent_commands[0].param == new_mask
    assert isinstance(sent_commands[1], SetEventFilter)
    # No DTR1/DTR2 commands — push-button event filter is a single byte only
    assert all(type(cmd).__name__ not in ("DTR1", "DTR2") for cmd in sent_commands)
    assert isinstance(sent_commands[2], QueryEventFilterZeroToSeven)

    assert param.value == new_mask
    assert result == {"event_filter": _bits_from_mask(new_mask)}


@pytest.mark.asyncio
async def test_event_filter_param_write_raises_when_readback_fails(monkeypatch):
    param = EventFilterParam(InstanceNumber(0))
    param.value = 0x00

    new_mask = 0x14
    new_value = {"event_filter": _bits_from_mask(new_mask)}

    async def fake_query_responses(*_args, **_kwargs):
        raise RuntimeError("no response on readback")

    monkeypatch.setattr("wb.mqtt_dali.dali2_type1_parameters.query_responses", fake_query_responses)

    driver = AsyncMock()
    with pytest.raises(RuntimeError):
        await param.write(driver, DeviceShort(11), new_value)

    assert param.value == 0x00


@pytest.mark.asyncio
async def test_event_filter_param_write_skips_when_value_unchanged():
    param = EventFilterParam(InstanceNumber(0))
    param.value = _factory_mask()
    same_value = {"event_filter": _bits_from_mask(_factory_mask())}

    driver = AsyncMock()

    result = await param.write(driver, DeviceShort(1), same_value)

    assert result == {}
    driver.send.assert_not_called()
    driver.send_commands.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [DeviceBroadcast(), GearBroadcast(), GearGroup(0), DeviceGroup(15)],
)
async def test_event_filter_param_write_for_group_address_is_noop(address):
    param = EventFilterParam(InstanceNumber(2))
    param.value = 0x00
    new_value = {"event_filter": _bits_from_mask(0xFF)}

    driver = AsyncMock()
    result = await param.write(driver, address, new_value)

    assert result == {}
    driver.send.assert_not_called()
    driver.send_commands.assert_not_called()


def test_event_filter_param_has_changes_detects_single_bit_flip():
    param = EventFilterParam(InstanceNumber(0))
    param.value = _factory_mask()

    same = {"event_filter": _bits_from_mask(_factory_mask())}
    flipped_bits = _bits_from_mask(_factory_mask())
    flipped_bits["button_released"] = True  # flip a single off-bit
    flipped = {"event_filter": flipped_bits}

    assert param.has_changes(same) is False
    assert param.has_changes(flipped) is True
    assert param.has_changes({}) is False


@pytest.mark.asyncio
async def test_event_filter_param_write_round_trip_returns_actual_device_value():
    param = EventFilterParam(InstanceNumber(0))
    param.value = 0x00

    requested_mask = 0xFF  # client wants all bits on
    requested_value = {"event_filter": _bits_from_mask(requested_mask)}

    # Device accepts only some bits — return-back differs from request.
    actual_mask = 0xF4
    driver = AsyncMock()
    dtr_resp = MagicMock()
    dtr_resp.raw_value.error = False
    dtr_resp.raw_value.as_integer = 0
    set_resp = MagicMock()
    set_resp.raw_value.error = False
    set_resp.raw_value.as_integer = 0
    query_resp = MagicMock()
    query_resp.raw_value.error = False
    query_resp.raw_value.as_integer = actual_mask
    driver.send_commands.return_value = [dtr_resp, set_resp, query_resp]

    result = await param.write(driver, DeviceShort(3), requested_value)

    assert param.value == actual_mask
    assert result == {"event_filter": _bits_from_mask(actual_mask)}


def test_dali2_instance_parameters_pushbutton_includes_event_filter():
    instance_number = InstanceNumber(0)
    pushbutton_params = InstanceParameters(instance_number, pushbutton.instance_type)
    assert any(isinstance(p, EventFilterParam) for p in pushbutton_params._parameters)

    other_types = [
        0,
        absolute_input_device.instance_type,
        occupancy.instance_type,
        light.instance_type,
        general_purpose_sensor.instance_type,
        feedback.instance_type,
    ]
    for instance_type in other_types:
        params = InstanceParameters(instance_number, instance_type)
        assert not any(
            isinstance(p, EventFilterParam) for p in params._parameters
        ), f"EventFilterParam should not appear for instance_type={instance_type}"
