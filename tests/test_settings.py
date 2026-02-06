from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wb.mqtt_dali.settings import (
    NumberSettingsParam,
    SettingsParamGroup,
    SettingsParamName,
)


@pytest.fixture
def number_settings_param():
    return NumberSettingsParam(name=SettingsParamName("test_name"), property_name="test_property")


# NumberSettingsParam


def test_initialization(number_settings_param):
    assert number_settings_param.property_name == "test_property"
    assert number_settings_param.minimum == 0
    assert number_settings_param.maximum == 255
    assert number_settings_param.default is None
    assert number_settings_param.grid_columns is None
    assert number_settings_param.property_order is None
    assert number_settings_param.value is None


@pytest.mark.asyncio
async def test_read_not_implemented(number_settings_param):
    mock_driver = MagicMock()
    mock_address = MagicMock()
    with pytest.raises(NotImplementedError):
        await number_settings_param.read(mock_driver, mock_address)


@pytest.mark.asyncio
async def test_read(number_settings_param):
    mock_driver = AsyncMock()
    mock_address = MagicMock()
    mock_query_request = AsyncMock()
    mock_query_request.return_value = 100
    mock_commands_list = MagicMock()
    with (
        patch.object(number_settings_param, "get_read_command", return_value=mock_commands_list),
        patch("wb.mqtt_dali.settings.query_request", mock_query_request),
    ):
        result = await number_settings_param.read(mock_driver, mock_address)
        mock_query_request.assert_called_once_with(mock_driver, mock_commands_list)
        assert result == {"test_property": 100}
        assert number_settings_param.value == 100


@pytest.mark.asyncio
async def test_write(number_settings_param):
    mock_driver = AsyncMock()
    mock_driver.send_commands.return_value = [
        None,
        MagicMock(raw_value=MagicMock(as_integer=100, error=None)),
    ]
    mock_address = MagicMock()
    number_settings_param.value = 50
    value_to_set = {"test_property": 100}
    mock_write_command = MagicMock()
    mock_read_command = MagicMock()

    with (
        patch.object(number_settings_param, "get_write_commands", return_value=[mock_write_command]),
        patch.object(number_settings_param, "get_read_command", return_value=mock_read_command),
    ):
        result = await number_settings_param.write(mock_driver, mock_address, value_to_set)
        mock_driver.send_commands.assert_called_once_with([mock_write_command, mock_read_command])
        assert result == {"test_property": 100}
        assert number_settings_param.value == 100


@pytest.mark.asyncio
async def test_write_no_change(number_settings_param):
    mock_driver = MagicMock()
    mock_address = MagicMock()
    number_settings_param.value = 100
    value_to_set = {"test_property": 100}

    result = await number_settings_param.write(mock_driver, mock_address, value_to_set)
    assert result == {}
    assert number_settings_param.value == 100


@pytest.mark.asyncio
async def test_write_missing_property_name(number_settings_param):
    mock_driver = MagicMock()
    mock_address = MagicMock()
    number_settings_param.value = 50
    value_to_set = {}  # Missing property_name

    result = await number_settings_param.write(mock_driver, mock_address, value_to_set)
    assert result == {}
    assert number_settings_param.value == 50  # Value should remain unchanged


@pytest.mark.asyncio
async def test_write_no_write_commands(number_settings_param):
    mock_driver = MagicMock()
    mock_address = MagicMock()
    number_settings_param.value = 50
    value_to_set = {"test_property": 100}

    with pytest.raises(RuntimeError, match="Set commands for test_name are not defined"):
        await number_settings_param.write(mock_driver, mock_address, value_to_set)


def test_get_schema(number_settings_param):
    schema = number_settings_param.get_schema()
    assert "properties" in schema
    assert "translations" not in schema
    assert schema["properties"]["test_property"]["title"] == "test_name"


@pytest.mark.asyncio
async def test_get_schema_with_options(number_settings_param):
    number_settings_param.grid_columns = 2
    schema = number_settings_param.get_schema()
    assert "properties" in schema
    assert "options" in schema["properties"]["test_property"]
    assert schema["properties"]["test_property"]["options"]["grid_columns"] == 2


@pytest.mark.asyncio
async def test_get_schema_read_only(number_settings_param):
    number_settings_param._is_read_only = True
    schema = number_settings_param.get_schema()
    assert "options" in schema["properties"]["test_property"]
    assert schema["properties"]["test_property"]["options"] == {"wb": {"read_only": True}}


@pytest.mark.asyncio
async def test_get_schema_translations(number_settings_param):
    number_settings_param.name.ru = "тестовое_имя"
    schema = number_settings_param.get_schema()
    assert "translations" in schema
    assert schema["translations"]["ru"]["test_name"] == "тестовое_имя"


@pytest.mark.asyncio
async def test_get_schema_default_value(number_settings_param):
    number_settings_param.default = 10
    schema = number_settings_param.get_schema()
    assert "default" in schema["properties"]["test_property"]
    assert schema["properties"]["test_property"]["default"] == 10


@pytest.mark.asyncio
async def test_get_schema_property_order(number_settings_param):
    number_settings_param.property_order = 1
    schema = number_settings_param.get_schema()
    assert "propertyOrder" in schema["properties"]["test_property"]
    assert schema["properties"]["test_property"]["propertyOrder"] == 1


# SettingsParamGroup


@pytest.mark.asyncio
async def test_settings_param_group_read(number_settings_param):
    mock_driver = AsyncMock()
    mock_address = MagicMock()

    group = SettingsParamGroup(name=SettingsParamName("group_name"), property_name="group_property")
    group._parameters = [number_settings_param]

    with patch.object(number_settings_param, "read", return_value={"test_property": 50}):
        result = await group.read(mock_driver, mock_address)
        assert result == {"group_property": {"test_property": 50}}


@pytest.mark.asyncio
async def test_settings_param_group_write(number_settings_param):
    mock_driver = AsyncMock()
    mock_address = MagicMock()

    group = SettingsParamGroup(name=SettingsParamName("group_name"), property_name="group_property")
    group._parameters = [number_settings_param]

    value = {"group_property": {"test_property": 100}}
    with patch.object(number_settings_param, "write", return_value={"test_property": 100}):
        result = await group.write(mock_driver, mock_address, value)
        assert result == {"group_property": {"test_property": 100}}


@pytest.mark.asyncio
async def test_settings_param_group_write_missing_property(number_settings_param):
    mock_driver = AsyncMock()
    mock_address = MagicMock()

    group = SettingsParamGroup(name=SettingsParamName("group_name"), property_name="group_property")
    group._parameters = [number_settings_param]

    result = await group.write(mock_driver, mock_address, {})
    assert result == {}


@pytest.mark.asyncio
async def test_settings_param_group_read_exception(number_settings_param):
    mock_driver = AsyncMock()
    mock_address = MagicMock()

    group = SettingsParamGroup(name=SettingsParamName("group_name"), property_name="group_property")
    group._parameters = [number_settings_param]

    with patch.object(number_settings_param, "read", side_effect=ValueError("Test error")):
        with pytest.raises(RuntimeError, match='Error reading "test_name"'):
            await group.read(mock_driver, mock_address)


@pytest.mark.asyncio
async def test_settings_param_group_get_schema(number_settings_param):

    number_settings_param.name.ru = "тестовое_имя"
    group = SettingsParamGroup(
        name=SettingsParamName("group_name", "группа_имя"), property_name="group_property"
    )
    group._parameters = [number_settings_param]

    schema = group.get_schema()
    assert "properties" in schema
    assert "group_property" in schema["properties"]
    assert schema["properties"]["group_property"]["title"] == "group_name"
    assert "translations" in schema
