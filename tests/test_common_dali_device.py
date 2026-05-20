from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.address import GearShort

from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.common_dali_device import (
    DaliDeviceAddress,
    DaliDeviceBase,
    MqttControl,
    MqttControlBase,
)
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.device_publisher import ControlInfo
from wb.mqtt_dali.wbmqtt import ControlMeta

# pylint: disable=invalid-name

# Prevent file system access in __init__ by providing a non-empty common schema
DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access


@pytest.mark.asyncio
def test_default_mqtt_id_when_no_custom():
    addr = DaliDeviceAddress(5, 0x00)
    # compat and gtin_db are not used for mqtt_id tests; simple dummies suffice
    d = DaliDeviceBase(
        address=addr,
        bus_id="bus1",
        default_name_prefix="n",
        default_mqtt_id_part="dev",
        compat=DaliCommandsCompatibilityLayer(),
        gtin_db=object(),
    )
    # default mqtt id is composed from bus_id, default_mqtt_id_part and short address
    assert d.default_mqtt_id == "bus1_dev5"
    # since no custom mqtt_id was provided, mqtt_id property returns default
    assert d.mqtt_id == "bus1_dev5"
    assert d.has_custom_mqtt_id is False


@pytest.mark.asyncio
def test_custom_mqtt_id_at_init_and_flag():
    d = DaliDeviceBase(
        address=DaliDeviceAddress(short=2, random=0),
        bus_id="b",
        default_name_prefix="n",
        default_mqtt_id_part="p",
        compat=DaliCommandsCompatibilityLayer(),
        gtin_db=object(),
        mqtt_id="custom_id",
    )
    assert d.mqtt_id == "custom_id"
    assert d.has_custom_mqtt_id is True


@pytest.mark.asyncio
def test_mqtt_id_setter_changes_internal_state():
    addr = DaliDeviceAddress(10, 0x00)
    # compat and gtin_db are not used for mqtt_id tests; simple dummies suffice
    d = DaliDeviceBase(
        address=addr,
        bus_id="busX",
        default_name_prefix="Dev",
        default_mqtt_id_part="x",
        compat=DaliCommandsCompatibilityLayer(),
        gtin_db=object(),
    )
    default = d.default_mqtt_id
    # set to a custom value
    d.mqtt_id = "my_custom"
    assert d.mqtt_id == "my_custom"
    assert d.has_custom_mqtt_id is True
    # set back to default => internal _mqtt_id should be cleared and property returns default
    d.mqtt_id = default
    assert d.mqtt_id == default
    assert d.has_custom_mqtt_id is False


@pytest.mark.asyncio
def test_default_name_when_no_custom():
    addr = DaliDeviceAddress(short=3, random=0xAB)
    d = DaliDeviceBase(
        address=addr,
        bus_id="bus1",
        default_name_prefix="Lamp",
        default_mqtt_id_part="dev",
        compat=DaliCommandsCompatibilityLayer(),
        gtin_db=object(),
    )
    assert d.default_name == "Lamp 3"
    assert d.name == "Lamp 3"
    assert d.has_custom_name is False


@pytest.mark.asyncio
def test_custom_name_at_init_and_flag():
    addr = DaliDeviceAddress(short=1, random=0xFF)
    d = DaliDeviceBase(
        address=addr,
        bus_id="b",
        default_name_prefix="Dev",
        default_mqtt_id_part="p",
        compat=DaliCommandsCompatibilityLayer(),
        gtin_db=object(),
        name="My Custom Name",
    )
    assert d.name == "My Custom Name"
    assert d.has_custom_name is True


@pytest.mark.asyncio
def test_name_equal_to_default_at_init_is_not_custom():
    addr = DaliDeviceAddress(short=7, random=0x10)
    default_name = "Prefix 7"
    d = DaliDeviceBase(
        address=addr,
        bus_id="b",
        default_name_prefix="Prefix",
        default_mqtt_id_part="p",
        compat=object(),
        gtin_db=object(),
        name=default_name,
    )
    assert d.name == default_name
    assert d.has_custom_name is False


@pytest.mark.asyncio
def test_name_setter_changes_internal_state():
    addr = DaliDeviceAddress(short=4, random=0x1234)
    d = DaliDeviceBase(
        address=addr,
        bus_id="bus",
        default_name_prefix="Light",
        default_mqtt_id_part="l",
        compat=DaliCommandsCompatibilityLayer(),
        gtin_db=object(),
    )
    default = d.default_name
    assert d.name == default
    assert d.has_custom_name is False

    # set to a custom value
    d.name = "Kitchen Light"
    assert d.name == "Kitchen Light"
    assert d.has_custom_name is True

    # set back to default => internal _name should be cleared
    d.name = default
    assert d.name == default
    assert d.has_custom_name is False


@pytest.mark.asyncio
def test_name_setter_empty_string_is_custom():
    # pylint: disable=protected-access
    addr = DaliDeviceAddress(short=0, random=0x00)
    d = DaliDeviceBase(
        address=addr,
        bus_id="b",
        default_name_prefix="P",
        default_mqtt_id_part="d",
        compat=DaliCommandsCompatibilityLayer(),
        gtin_db=object(),
    )
    # Empty string is falsy but should still be treated as custom if different from default
    d.name = ""
    # Because empty string is falsy, `self._name or self.default_name` returns default_name
    # This tests the actual behavior of the property
    assert d.name == d.default_name
    # The setter stores "" since it differs from default
    assert d._name == ""


@pytest.mark.asyncio
def test_name_none_at_init_uses_default():
    # pylint: disable=protected-access
    addr = DaliDeviceAddress(short=9, random=0xDEAD)
    d = DaliDeviceBase(
        address=addr,
        bus_id="bus",
        default_name_prefix="Sensor",
        default_mqtt_id_part="s",
        compat=DaliCommandsCompatibilityLayer(),
        gtin_db=object(),
        name=None,
    )
    assert d.name == "Sensor 9"
    assert d.has_custom_name is False
    # Prevent file system access in __init__ by providing a non-empty common schema
    DaliDeviceBase._common_schema = {"title": "test-schema", "properties": {}}


class ConcreteDaliDevice(DaliDeviceBase):  # pylint: disable=too-many-instance-attributes
    """Concrete subclass that implements the abstract method."""

    def __init__(self, *args, extra_param_handlers=None, **kwargs):
        self._extra_param_handlers = extra_param_handlers or []
        super().__init__(*args, **kwargs)

    async def _initialize_impl(self, driver):
        return (self._extra_param_handlers, [])


def _make_device(**kwargs):
    defaults = {
        "address": DaliDeviceAddress(short=1, random=0x00),
        "bus_id": "bus",
        "default_name_prefix": "Dev",
        "default_mqtt_id_part": "d",
        "compat": DaliCommandsCompatibilityLayer(),
        "gtin_db": MagicMock(),
    }
    defaults.update(kwargs)
    return ConcreteDaliDevice(**defaults)


def _make_mock_param_handler(read_return=None, schema_return=None):
    handler = MagicMock()
    handler.read = AsyncMock(return_value=read_return or {})
    handler.get_schema = MagicMock(return_value=schema_return)
    return handler


@pytest.mark.asyncio
async def test_load_info_populates_params_and_schema():
    d = _make_device()
    driver = AsyncMock()
    driver.send = AsyncMock(return_value=None)
    driver.run_sequence = AsyncMock(return_value={})

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler(read_return={"gtin": 123})
        MockGMP.return_value = mock_gmp_instance

        await d.load_info(driver)

    assert d.params["short_address"] == 1
    assert d.params["random_address"] == "0x0"
    assert d.params["name"] == d.name
    assert d.params["mqtt_id"] == d.mqtt_id
    assert d.params.get("gtin") == 123
    assert d.schema is not None


@pytest.mark.asyncio
async def test_load_info_skips_if_params_already_loaded():
    d = _make_device()
    d.params = {"short_address": 1}  # pre-populate
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        await d.load_info(driver)
        MockGMP.assert_not_called()

    # params remain unchanged
    assert d.params == {"short_address": 1}


@pytest.mark.asyncio
async def test_load_info_force_reload_reloads_even_if_params_present():
    d = _make_device()
    d.params = {"short_address": 999}
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler(read_return={"firmware_version": "1.0"})
        MockGMP.return_value = mock_gmp_instance

        await d.load_info(driver, force_reload=True)

    assert d.params["short_address"] == 1
    assert d.params.get("firmware_version") == "1.0"


@pytest.mark.asyncio
async def test_load_info_merges_params_from_multiple_handlers():
    extra_handler = _make_mock_param_handler(read_return={"extra_key": "extra_value"})
    d = _make_device(extra_param_handlers=[extra_handler])
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler(read_return={"gtin": 456})
        MockGMP.return_value = mock_gmp_instance

        await d.load_info(driver)

    assert d.params["gtin"] == 456
    assert d.params["extra_key"] == "extra_value"
    assert d.params["short_address"] == 1


@pytest.mark.asyncio
async def test_load_info_later_handler_overrides_earlier():
    extra_handler = _make_mock_param_handler(read_return={"gtin": 999})
    d = _make_device(extra_param_handlers=[extra_handler])
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler(read_return={"gtin": 123})
        MockGMP.return_value = mock_gmp_instance

        await d.load_info(driver)

    # extra_handler comes after GeneralMemoryParams, so its value wins
    assert d.params["gtin"] == 999


@pytest.mark.asyncio
async def test_load_info_merges_schemas_from_handlers():
    extra_schema = {"properties": {"extra_prop": {"type": "string"}}}
    extra_handler = _make_mock_param_handler(read_return={}, schema_return=extra_schema)
    d = _make_device(extra_param_handlers=[extra_handler])
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        gmp_schema = {"properties": {"gmp_prop": {"type": "integer"}}}
        mock_gmp_instance = _make_mock_param_handler(read_return={}, schema_return=gmp_schema)
        MockGMP.return_value = mock_gmp_instance

        with patch("wb.mqtt_dali.common_dali_device.merge_json_schemas") as mock_merge:
            await d.load_info(driver)
            # merge_json_schemas should be called for each non-None schema
            assert mock_merge.call_count == 2


@pytest.mark.asyncio
async def test_load_info_skips_none_schemas():
    extra_handler = _make_mock_param_handler(read_return={}, schema_return=None)
    d = _make_device(extra_param_handlers=[extra_handler])
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler(read_return={}, schema_return=None)
        MockGMP.return_value = mock_gmp_instance

        with patch("wb.mqtt_dali.common_dali_device.merge_json_schemas") as mock_merge:
            await d.load_info(driver)
            mock_merge.assert_not_called()


@pytest.mark.asyncio
async def test_load_info_stores_parameter_handlers():
    # pylint: disable=protected-access
    extra_handler = _make_mock_param_handler()
    d = _make_device(extra_param_handlers=[extra_handler])
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler()
        MockGMP.return_value = mock_gmp_instance

        await d.load_info(driver)

    assert len(d._parameter_handlers) == 2
    assert d._parameter_handlers[0] is mock_gmp_instance
    assert d._parameter_handlers[1] is extra_handler


@pytest.mark.asyncio
async def test_load_info_schema_is_deepcopy_of_common():
    # pylint: disable=protected-access
    d = _make_device()
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler()
        MockGMP.return_value = mock_gmp_instance

        await d.load_info(driver)

    # schema should not be the same object as _common_schema
    assert d.schema is not DaliDeviceBase._common_schema
    assert d.schema["title"] == "test-schema"


@pytest.mark.asyncio
async def test_load_info_uses_correct_short_address_for_read():
    addr = DaliDeviceAddress(short=42, random=0xBEEF)
    d = _make_device(address=addr)
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler()
        MockGMP.return_value = mock_gmp_instance

        await d.load_info(driver)

    mock_gmp_instance.read.assert_awaited_once_with(driver, GearShort(42), d.logger)


@pytest.mark.asyncio
async def test_load_info_includes_custom_name_and_mqtt_id():
    d = _make_device(
        address=DaliDeviceAddress(short=5, random=0x10),
        mqtt_id="custom_mqtt",
        name="Custom Name",
    )
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler()
        MockGMP.return_value = mock_gmp_instance

        await d.load_info(driver)

    assert d.params["name"] == "Custom Name"
    assert d.params["mqtt_id"] == "custom_mqtt"


@pytest.mark.asyncio
async def test_load_info_empty_params_dict_triggers_load():
    d = _make_device()
    d.params = {}  # empty dict is falsy
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as MockGMP:
        mock_gmp_instance = _make_mock_param_handler(read_return={"key": "val"})
        MockGMP.return_value = mock_gmp_instance

        await d.load_info(driver)

    assert d.params.get("key") == "val"
    assert d.params["short_address"] == 1


def _make_readable_alarm_control(control_id: str, query, value_formatter, title_formatter) -> MqttControlBase:
    class _AlarmControl(MqttControlBase):
        def is_readable(self) -> bool:
            return True

        def get_query(self, short_address):  # type: ignore[override]
            del short_address
            return query

        def format_response(self, response) -> str:  # type: ignore[override]
            return value_formatter(response)

        def format_title(self, response):  # type: ignore[override]
            return title_formatter(response)

    return _AlarmControl(ControlInfo(control_id, ControlMeta(control_type="alarm"), "0"))


def _make_readable_value_control(control_id, query, value_formatter) -> MqttControl:
    return MqttControl(
        control_info=ControlInfo(control_id, ControlMeta(control_type="value"), "0"),
        query_builder=lambda short_address, q=query: q,
        value_formatter=value_formatter,
    )


@pytest.mark.asyncio
async def test_run_single_query_returns_error_when_response_is_none():
    # pylint: disable=protected-access
    control = _make_readable_value_control("c1", "Q1", lambda r: "formatted")
    driver = AsyncMock()
    driver.send_commands = AsyncMock(return_value=[None])

    res = await control._run_single_query(driver, GearShort(1))

    driver.send_commands.assert_awaited_once_with(["Q1"], BusTrafficSource.WB)
    assert len(res) == 1
    assert res[0].control_id == "c1"
    assert res[0].value == ""
    assert res[0].error == "r"


@pytest.mark.asyncio
async def test_run_single_query_returns_error_when_raw_value_is_none():
    # pylint: disable=protected-access
    formatter = MagicMock(return_value="formatted")
    control = _make_readable_value_control("c2", "Q2", formatter)
    driver = AsyncMock()

    response = MagicMock()
    response.raw_value = None
    driver.send_commands = AsyncMock(return_value=[response])

    res = await control._run_single_query(driver, GearShort(1))

    assert len(res) == 1
    assert res[0].control_id == "c2"
    assert res[0].value == ""
    assert res[0].error == "r"
    formatter.assert_not_called()


@pytest.mark.asyncio
async def test_run_single_query_returns_error_when_raw_value_has_error():
    # pylint: disable=protected-access
    formatter = MagicMock(return_value="formatted")
    control = _make_readable_value_control("c3", "Q3", formatter)
    driver = AsyncMock()

    response = MagicMock()
    response.raw_value = MagicMock()
    response._expected = True
    response._error_acceptable = False
    response.raw_value.error = True
    driver.send_commands = AsyncMock(return_value=[response])

    res = await control._run_single_query(driver, GearShort(1))

    assert len(res) == 1
    assert res[0].control_id == "c3"
    assert res[0].value == ""
    assert res[0].error == "r"
    formatter.assert_not_called()


@pytest.mark.asyncio
async def test_run_single_query_formats_regular_control_value():
    # pylint: disable=protected-access
    formatter = MagicMock(return_value="77")
    query_builder = MagicMock(return_value="Q_BRIGHT")
    control = MqttControl(
        control_info=ControlInfo("brightness", ControlMeta(control_type="value"), "0"),
        query_builder=query_builder,
        value_formatter=formatter,
    )
    driver = AsyncMock()

    response = MagicMock()
    response.raw_value = MagicMock()
    response.raw_value.error = False
    driver.send_commands = AsyncMock(return_value=[response])

    res = await control._run_single_query(driver, GearShort(7))

    query_builder.assert_called_once_with(GearShort(7))
    formatter.assert_called_once_with(response)
    assert len(res) == 1
    assert res[0].control_id == "brightness"
    assert res[0].value == "77"
    assert res[0].error is None
    assert res[0].title is None


@pytest.mark.asyncio
async def test_run_single_query_alarm_control_active_when_response_error_true():
    # pylint: disable=protected-access
    format_response = MagicMock(return_value="1")
    format_title = MagicMock(return_value="Lamp failure")
    control = _make_readable_alarm_control("alarm1", "Q_ALARM", format_response, format_title)
    driver = AsyncMock()

    response = MagicMock()
    response.raw_value = MagicMock()
    response.raw_value.error = False
    response.error = True
    driver.send_commands = AsyncMock(return_value=[response])

    res = await control._run_single_query(driver, GearShort(1))

    assert len(res) == 1
    assert res[0].control_id == "alarm1"
    assert res[0].value == "1"
    assert res[0].title == "Lamp failure"
    assert res[0].error is None
    format_response.assert_called_once_with(response)


@pytest.mark.asyncio
async def test_run_single_query_alarm_control_inactive_when_response_error_false_or_missing():
    # pylint: disable=protected-access
    format_response = MagicMock(return_value="0")
    format_title = MagicMock(return_value="No alarms")
    control = _make_readable_alarm_control("alarm2", "Q_ALARM2", format_response, format_title)
    driver = AsyncMock()

    response = MagicMock()
    response.raw_value = MagicMock()
    response.raw_value.error = False
    if hasattr(response, "error"):
        del response.error
    driver.send_commands = AsyncMock(return_value=[response])

    res = await control._run_single_query(driver, GearShort(1))

    assert len(res) == 1
    assert res[0].control_id == "alarm2"
    assert res[0].value == "0"
    assert res[0].title == "No alarms"
    assert res[0].error is None


def _build_ok_response():
    r = MagicMock()
    r.raw_value = MagicMock()
    r.raw_value.error = False
    return r


@pytest.mark.asyncio
async def test_poll_controls_multiple_controls_and_queries_order():
    # pylint: disable=protected-access
    d = _make_device(mqtt_id="dev_multi")
    c3_format = MagicMock(return_value="should_not_be_used")
    controls = [
        _make_readable_value_control("regular", "Q1", MagicMock(return_value="11")),
        _make_readable_alarm_control(
            "alarm", "Q2", MagicMock(return_value="0"), MagicMock(return_value="Alarm text")
        ),
        _make_readable_value_control("bad", "Q3", c3_format),
    ]
    r1 = _build_ok_response()
    r2 = _build_ok_response()
    r2.error = False

    # send_commands is called once per control; bulking happens in wbdali on a real bus.
    d._controls = {c.control_info.id: c for c in controls}
    d._pollables = list(controls)
    d._current_round = list(controls)
    d.is_initialized = True
    for ctrl in controls:
        ctrl.last_poll_time = 0.0

    responses_per_call = {"Q1": [r1], "Q2": [r2], "Q3": [None]}
    issued_queries: list[str] = []

    async def fake_send(cmds, _src=BusTrafficSource.WB):
        assert len(cmds) == 1
        issued_queries.append(cmds[0])
        return responses_per_call[cmds[0]]

    driver = AsyncMock()
    driver.send_commands = AsyncMock(side_effect=fake_send)

    res_request = d.poll_controls(
        driver, now=0.0, max_commands=3, default_max_commands=3, default_poll_interval=5.0
    )
    assert res_request.commands_count == 3
    assert res_request.poll_coroutine is not None
    res = await res_request.poll_coroutine()

    assert sorted(issued_queries) == ["Q1", "Q2", "Q3"]
    by_id = {r.control_id: r for r in res}
    assert by_id["regular"].value == "11"
    assert by_id["alarm"].value == "0"
    assert by_id["alarm"].title == "Alarm text"
    assert by_id["bad"].value == ""
    assert by_id["bad"].error == "r"
    c3_format.assert_not_called()


@pytest.mark.asyncio
async def test_apply_parameters_raises_when_not_initialized():
    d = _make_device()
    driver = AsyncMock()

    with pytest.raises(RuntimeError, match="not initialized"):
        await d.apply_parameters(driver, {"name": "New Name"})


@pytest.mark.asyncio
async def test_apply_parameters_raises_when_params_not_loaded():
    d = _make_device()
    d.is_initialized = True
    d.params = {}
    driver = AsyncMock()

    with pytest.raises(RuntimeError, match="info is not loaded"):
        await d.apply_parameters(driver, {"name": "New Name"})


@pytest.mark.asyncio
async def test_apply_parameters_does_not_call_load_info_when_params_present():
    # pylint: disable=protected-access
    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = []
    d.load_info = AsyncMock()
    d._apply_common_parameters = AsyncMock()
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.jsonschema.validate"):
        await d.apply_parameters(driver, {"mqtt_id": "abc"})

    d.load_info.assert_not_awaited()
    d._apply_common_parameters.assert_awaited_once_with(driver, {"mqtt_id": "abc"})


@pytest.mark.asyncio
async def test_apply_parameters_validates_with_current_schema():
    # pylint: disable=protected-access
    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    d._parameter_handlers = []
    d._apply_common_parameters = AsyncMock()
    driver = AsyncMock()
    new_values = {"name": "Device 1"}

    with patch("wb.mqtt_dali.common_dali_device.jsonschema.validate") as mock_validate:
        await d.apply_parameters(driver, new_values)

    mock_validate.assert_called_once_with(
        instance=new_values,
        schema=d.schema,
        format_checker=pytest.importorskip("jsonschema").draft4_format_checker,
    )


@pytest.mark.asyncio
async def test_apply_parameters_calls_write_for_each_handler_and_updates_params():
    # pylint: disable=protected-access
    h1 = MagicMock()
    h1.write = AsyncMock(return_value={"p1": "v1", "shared": "from_h1"})
    h2 = MagicMock()
    h2.write = AsyncMock(return_value={"p2": 2, "shared": "from_h2"})

    d = _make_device(address=DaliDeviceAddress(short=33, random=0x00))
    d.is_initialized = True
    d.params = {"existing": "keep"}
    d.schema = {"type": "object"}
    d._parameter_handlers = [h1, h2]
    d._apply_common_parameters = AsyncMock()
    driver = AsyncMock()
    new_values = {"any": "value"}

    with patch("wb.mqtt_dali.common_dali_device.jsonschema.validate"):
        await d.apply_parameters(driver, new_values)

    h1.write.assert_awaited_once_with(driver, GearShort(33), new_values, d.logger)
    h2.write.assert_awaited_once_with(driver, GearShort(33), new_values, d.logger)
    assert d.params["existing"] == "keep"
    assert d.params["p1"] == "v1"
    assert d.params["p2"] == 2
    assert d.params["shared"] == "from_h2"
    d._apply_common_parameters.assert_awaited_once_with(driver, new_values)


@pytest.mark.asyncio
async def test_apply_parameters_calls_apply_common_even_with_no_handlers():
    # pylint: disable=protected-access
    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = []
    d._apply_common_parameters = AsyncMock()
    driver = AsyncMock()
    new_values = {"short_address": 2}

    with patch("wb.mqtt_dali.common_dali_device.jsonschema.validate"):
        await d.apply_parameters(driver, new_values)

    d._apply_common_parameters.assert_awaited_once_with(driver, new_values)


@pytest.mark.asyncio
async def test_apply_parameters_propagates_validation_error_and_stops_processing():
    # pylint: disable=protected-access
    h = MagicMock()
    h.write = AsyncMock(return_value={"x": 1})

    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = [h]
    d._apply_common_parameters = AsyncMock()
    driver = AsyncMock()

    with patch(
        "wb.mqtt_dali.common_dali_device.jsonschema.validate",
        side_effect=ValueError("invalid"),
    ):
        with pytest.raises(ValueError, match="invalid"):
            await d.apply_parameters(driver, {"bad": "value"})

    h.write.assert_not_awaited()
    d._apply_common_parameters.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_parameters_propagates_write_error_and_does_not_apply_common():
    # pylint: disable=protected-access
    h1 = MagicMock()
    h1.write = AsyncMock(side_effect=RuntimeError("write failed"))
    h2 = MagicMock()
    h2.write = AsyncMock(return_value={"ok": True})

    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = [h1, h2]
    d._apply_common_parameters = AsyncMock()
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.jsonschema.validate"):
        with pytest.raises(RuntimeError, match="write failed"):
            await d.apply_parameters(driver, {"name": "n"})

    h1.write.assert_awaited_once()
    h2.write.assert_not_awaited()
    d._apply_common_parameters.assert_not_awaited()
