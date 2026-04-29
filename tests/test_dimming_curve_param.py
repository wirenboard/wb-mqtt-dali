"""Tests for DimmingCurveParam (editable + read-only fallback) and the
DaliDevice integration that injects the read-only fallback for device
types without dimming curve support.
"""

from contextlib import ExitStack
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wb.mqtt_dali import dali_device as _dd
from wb.mqtt_dali import settings as _settings
from wb.mqtt_dali.common_dali_device import (
    DaliDeviceAddress,
    DaliDeviceBase,
    PropertyStartOrder,
)
from wb.mqtt_dali.dali_common_parameters import GroupsParam
from wb.mqtt_dali.dali_device import DaliDevice, DaliDeviceType
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveState, DimmingCurveType
from wb.mqtt_dali.dali_parameters import DimmingCurveParam
from wb.mqtt_dali.dali_type5_parameters import Type5DimmingCurveParam, Type5Parameters
from wb.mqtt_dali.dali_type6_parameters import Type6DimmingCurveParam, Type6Parameters
from wb.mqtt_dali.dali_type17_parameters import (
    Type17DimmingCurveParam,
    Type17Parameters,
)

# pylint: disable=protected-access,redefined-outer-name


@pytest.fixture(autouse=True)
def _stub_common_schema(monkeypatch):
    # Prevent file system access in DaliDeviceBase.__init__ by providing a
    # non-empty common schema. monkeypatch restores the original value after
    # each test so we don't leak state into other tests in the same process.
    monkeypatch.setattr(
        DaliDeviceBase,
        "_common_schema",
        {"title": "test-schema", "properties": {}},
        raising=False,
    )


# ---------------------------------------------------------------------------
# DimmingCurveParam — editable variant
# ---------------------------------------------------------------------------


def test_editable_param_is_writable():
    state = DimmingCurveState()
    param = Type6DimmingCurveParam(state)
    assert param._is_read_only is False


def test_editable_param_property_order_and_description():
    state = DimmingCurveState()
    param = Type6DimmingCurveParam(state)
    assert param.property_order == PropertyStartOrder.COMMON.value
    assert param.description == "dimming_curve_desc"


def test_editable_schema_has_both_curve_options():
    state = DimmingCurveState()
    param = Type6DimmingCurveParam(state)
    schema = param.get_schema(group_and_broadcast=False)
    prop = schema["properties"][param.property_name]
    assert prop["enum"] == [DimmingCurveType.LOGARITHMIC, DimmingCurveType.LINEAR]
    assert prop["options"]["enum_titles"] == ["standard", "linear"]
    assert prop["propertyOrder"] == PropertyStartOrder.COMMON.value
    assert prop["description"] == "dimming_curve_desc"
    assert "grid_columns" not in prop.get("options", {})
    # Editable description must mention linear curve in translations
    ru = schema["translations"]["ru"]
    assert ru["standard"] == "стандартная"
    assert ru["linear"] == "линейная"
    assert "Линейная кривая" in ru["dimming_curve_desc"]
    assert "linear curve" in schema["translations"]["en"]["dimming_curve_desc"]


def test_editable_schema_for_type17():
    state = DimmingCurveState()
    param = Type17DimmingCurveParam(state)
    schema = param.get_schema(group_and_broadcast=False)
    prop = schema["properties"][param.property_name]
    assert prop["enum"] == [DimmingCurveType.LOGARITHMIC, DimmingCurveType.LINEAR]
    assert prop["propertyOrder"] == PropertyStartOrder.COMMON.value


# ---------------------------------------------------------------------------
# DimmingCurveParam — read-only fallback variant
# ---------------------------------------------------------------------------


def test_read_only_param_is_marked_read_only():
    state = DimmingCurveState()
    param = DimmingCurveParam(state)
    assert param._is_read_only is True


def test_read_only_param_property_order():
    state = DimmingCurveState()
    param = DimmingCurveParam(state)
    assert param.property_order == PropertyStartOrder.COMMON.value


@pytest.mark.asyncio
async def test_read_only_param_read_does_not_touch_bus(monkeypatch):
    # NumberSettingsParam.read goes through query_int, not driver.send
    # directly. Stub query_int (as imported into the settings module) so
    # that any bus access would be observable, and assert it was never
    # invoked for the read-only fallback.
    query_int_spy = AsyncMock(side_effect=AssertionError("Bus access forbidden in read-only mode"))
    monkeypatch.setattr(_settings, "query_int", query_int_spy)

    state = DimmingCurveState()
    param = DimmingCurveParam(state)
    driver = AsyncMock()
    address = MagicMock()
    result = await param.read(driver, address)

    assert result == {param.property_name: DimmingCurveType.LOGARITHMIC}
    assert param.value == DimmingCurveType.LOGARITHMIC
    assert state.curve_type == DimmingCurveType.LOGARITHMIC
    query_int_spy.assert_not_called()


@pytest.mark.asyncio
async def test_read_only_param_write_is_noop_and_does_not_touch_bus(monkeypatch):
    # Writing to a read-only fallback must be a safe no-op. It must not
    # call the underlying NumberSettingsParam.write machinery (which would
    # eventually hit get_write_commands and raise RuntimeError), and it
    # must not touch the bus.
    query_responses_spy = AsyncMock(side_effect=AssertionError("Bus access forbidden in read-only mode"))
    monkeypatch.setattr(_settings, "query_responses", query_responses_spy)

    state = DimmingCurveState()
    param = DimmingCurveParam(state)
    driver = AsyncMock()
    address = MagicMock()

    # Value dict containing our property — must not raise.
    res_with_key = await param.write(driver, address, {param.property_name: DimmingCurveType.LINEAR})
    assert res_with_key == {}

    # Value dict without our property — also no-op.
    res_without_key = await param.write(driver, address, {"other": 1})
    assert res_without_key == {}

    # State must remain at LOGARITHMIC — write on a read-only param cannot
    # change the curve.
    assert state.curve_type == DimmingCurveType.LOGARITHMIC
    query_responses_spy.assert_not_called()


def test_read_only_schema_is_read_only_and_value_is_standard():
    state = DimmingCurveState()
    param = DimmingCurveParam(state)
    schema = param.get_schema(group_and_broadcast=False)
    prop = schema["properties"][param.property_name]
    # Enum and translations are identical between editable/read-only variants;
    # the field is disabled in the UI via the read_only option.
    assert prop["enum"] == [DimmingCurveType.LOGARITHMIC, DimmingCurveType.LINEAR]
    assert prop["options"]["enum_titles"] == ["standard", "linear"]
    assert prop["options"]["wb"] == {"read_only": True}
    assert prop["propertyOrder"] == PropertyStartOrder.COMMON.value
    assert prop["description"] == "dimming_curve_desc"
    # The read-only fallback always reports the standard (logarithmic) curve.
    assert param._is_read_only is True
    assert "grid_columns" not in prop.get("options", {})
    # Read-only description must NOT mention the linear curve
    ru = schema["translations"]["ru"]["dimming_curve_desc"]
    en = schema["translations"]["en"]["dimming_curve_desc"]
    assert "Линейная" not in ru and "линейная кривая" not in ru.lower()
    assert "linear curve" not in en.lower()
    assert "фиксированную стандартную" in ru
    assert "fixed standard" in en


def test_read_only_schema_omitted_for_group_and_broadcast():
    state = DimmingCurveState()
    param = DimmingCurveParam(state)
    # Read-only NumberSettingsParam returns {} for group/broadcast schemas
    schema = param.get_schema(group_and_broadcast=True)
    assert not schema


# ---------------------------------------------------------------------------
# DaliDevice fallback integration
# ---------------------------------------------------------------------------


def _make_dali_device(short: int = 1) -> DaliDevice:
    return DaliDevice(
        DaliDeviceAddress(short=short, random=0x123456),
        bus_id="bus_test",
        gtin_db=MagicMock(),
    )


async def _initialize(
    device: DaliDevice,
    types: list[int],
    type5_supports_nonlog: Optional[bool] = None,
):
    """Run DaliDevice._initialize_impl with the given list of DALI types.

    Stubs out the per-type-handler ``read_mandatory_info`` and
    ``GroupsParam.read`` so that no real bus traffic is required.

    Patches are entered via ``ExitStack`` so that a failure in the middle
    of starting them cleanly tears down every patcher that was actually
    started, instead of calling ``stop()`` on unstarted patchers.
    """
    driver = AsyncMock()
    driver.run_sequence = AsyncMock(return_value=types)

    other_type_classes = [
        _dd.Type1Parameters,
        _dd.Type4Parameters,
        _dd.Type7Parameters,
        _dd.Type16Parameters,
        _dd.Type20Parameters,
        _dd.Type21Parameters,
        _dd.Type49Parameters,
        _dd.Type50Parameters,
        _dd.Type52Parameters,
        _dd.Type8Parameters,
    ]

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(Type6Parameters, "read_mandatory_info", new=AsyncMock(return_value=None))
        )
        stack.enter_context(
            patch.object(Type17Parameters, "read_mandatory_info", new=AsyncMock(return_value=None))
        )

        if type5_supports_nonlog is not None:

            async def _type5_read(self, _driver, _address, _logger=None):
                if type5_supports_nonlog:
                    # Mimic Type5Parameters when feature bit is True: install the param
                    self._dimming_curve_parameter = Type5DimmingCurveParam(self._dimming_curve_state)
                    self._parameters = [self._dimming_curve_parameter]
                else:
                    self._dimming_curve_parameter = None

            stack.enter_context(patch.object(Type5Parameters, "read_mandatory_info", new=_type5_read))

        for cls in other_type_classes:
            stack.enter_context(patch.object(cls, "read_mandatory_info", new=AsyncMock(return_value=None)))

        # Stub GroupsParam.read so we don't need to mock the bus protocol
        stack.enter_context(patch.object(GroupsParam, "read", new=AsyncMock(return_value={"groups": []})))

        return await device._initialize_impl(driver)


def _find_dimming_curve_param(handlers) -> Optional[DimmingCurveParam]:
    for h in handlers:
        if isinstance(h, DimmingCurveParam):
            return h
    return None


@pytest.mark.asyncio
async def test_dt6_keeps_editable_dimming_curve():
    device = _make_dali_device()
    parameter_handlers, group_parameter_handlers = await _initialize(
        device, types=[DaliDeviceType.LED_MODULES.value]
    )
    param = _find_dimming_curve_param(parameter_handlers)
    assert isinstance(param, Type6DimmingCurveParam)
    assert param._is_read_only is False
    assert param.property_order == PropertyStartOrder.COMMON.value

    schema = param.get_schema(group_and_broadcast=False)
    assert schema["properties"][param.property_name]["enum"] == [
        DimmingCurveType.LOGARITHMIC,
        DimmingCurveType.LINEAR,
    ]
    # Editable param remains in group schema (Type6Parameters owns it).
    group_param = _find_dimming_curve_param(group_parameter_handlers)
    assert isinstance(group_param, Type6DimmingCurveParam)


@pytest.mark.asyncio
async def test_dt17_keeps_editable_dimming_curve():
    device = _make_dali_device()
    parameter_handlers, _ = await _initialize(device, types=[DaliDeviceType.DIMMING_CURVE_SELECTION.value])
    param = _find_dimming_curve_param(parameter_handlers)
    assert isinstance(param, Type17DimmingCurveParam)
    assert param._is_read_only is False
    assert param.property_order == PropertyStartOrder.COMMON.value


@pytest.mark.parametrize(
    "device_type",
    [
        DaliDeviceType.FLUORESCENT_LAMP_BALLAST.value,
        DaliDeviceType.SELF_CONTAINED_EMERGENCY_LIGHTING.value,
        DaliDeviceType.DISCHARGE_LAMPS.value,
        DaliDeviceType.SWITCHING_FUNCTION.value,
    ],
)
@pytest.mark.asyncio
async def test_unsupported_types_get_read_only_fallback(device_type):
    device = _make_dali_device()
    parameter_handlers, group_parameter_handlers = await _initialize(device, types=[device_type])
    param = _find_dimming_curve_param(parameter_handlers)
    assert isinstance(param, DimmingCurveParam)
    assert param._is_read_only is True
    assert param.property_order == PropertyStartOrder.COMMON.value

    schema = param.get_schema(group_and_broadcast=False)
    prop = schema["properties"][param.property_name]
    assert prop["enum"] == [DimmingCurveType.LOGARITHMIC, DimmingCurveType.LINEAR]
    assert prop["options"]["wb"] == {"read_only": True}
    assert "grid_columns" not in prop.get("options", {})

    # Reading the read-only fallback returns LOGARITHMIC without bus access.
    driver = AsyncMock()
    driver.send = AsyncMock(side_effect=AssertionError("Bus access forbidden"))
    address = MagicMock()
    result = await param.read(driver, address)
    assert result == {"dimming_curve": DimmingCurveType.LOGARITHMIC}
    assert param.value == DimmingCurveType.LOGARITHMIC

    # Read-only fallback must NOT appear in group/broadcast handlers.
    assert _find_dimming_curve_param(group_parameter_handlers) is None


@pytest.mark.asyncio
async def test_dt5_without_nonlog_support_gets_fallback():
    device = _make_dali_device()
    parameter_handlers, group_parameter_handlers = await _initialize(
        device,
        types=[DaliDeviceType.CONVERSION_FROM_DIGITAL_SIGNAL_INTO_DC_VOLTAGE.value],
        type5_supports_nonlog=False,
    )
    param = _find_dimming_curve_param(parameter_handlers)
    assert isinstance(param, DimmingCurveParam)
    assert param._is_read_only is True

    schema = param.get_schema(group_and_broadcast=False)
    prop = schema["properties"][param.property_name]
    assert prop["enum"] == [DimmingCurveType.LOGARITHMIC, DimmingCurveType.LINEAR]
    assert prop["options"]["wb"] == {"read_only": True}

    # Read-only fallback is not added to the group parameters.
    assert _find_dimming_curve_param(group_parameter_handlers) is None


@pytest.mark.asyncio
async def test_dt5_with_nonlog_support_keeps_editable_param():
    device = _make_dali_device()
    parameter_handlers, _ = await _initialize(
        device,
        types=[DaliDeviceType.CONVERSION_FROM_DIGITAL_SIGNAL_INTO_DC_VOLTAGE.value],
        type5_supports_nonlog=True,
    )
    param = _find_dimming_curve_param(parameter_handlers)
    assert isinstance(param, DimmingCurveParam)
    assert param._is_read_only is False
