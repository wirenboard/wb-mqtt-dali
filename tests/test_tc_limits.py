# pylint: disable=duplicate-code
# Tests for TC limits feature: reading limits, TcLimitsSettings handler,
# rebuild_mqtt_controls, ApplyResult propagation, sync_controls_after_broadcast

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.address import GearBroadcast, GearGroup, GearShort
from dali.gear.colour import (
    StoreColourTemperatureTcLimit,
    StoreColourTemperatureTcLimitDTR2,
    tc_kelvin_mirek,
)
from dali.gear.general import DTR0, DTR1, DTR2

from wb.mqtt_dali.common_dali_device import (
    ApplyResult,
    DaliDeviceAddress,
    DaliDeviceBase,
    InitializationResult,
    MqttControl,
    MqttControlBase,
    NotifyResult,
)
from wb.mqtt_dali.control_ids import CURRENT_COLOUR_TEMPERATURE, SET_COLOUR_TEMPERATURE
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.dali_type8_common import ColourComponent
from wb.mqtt_dali.dali_type8_parameters import Type8Parameters
from wb.mqtt_dali.dali_type8_tc import (
    MAX_TC_MIREK,
    MIN_TC_MIREK,
    UI_MAX_TC_K,
    UI_MAX_TC_MIREK,
    UI_MIN_TC_K,
    UI_MIN_TC_MIREK,
    ColourTemperatureValue,
    CurrentColourTemperatureControl,
    TcLimitsSettings,
    Type8TcLimits,
    get_wanted_mqtt_controls,
    read_colour_temperature_limits_mirek,
)
from wb.mqtt_dali.device_publisher import ControlInfo
from wb.mqtt_dali.events import ColourChanged, EventSource
from wb.mqtt_dali.settings import SettingsParamBase, SettingsParamName
from wb.mqtt_dali.wbdali_utils import MASK_2BYTES
from wb.mqtt_dali.wbmqtt import ControlError, ControlMeta, ControlState, TranslatedTitle

from ._control_stubs import ReadableControl


def _make_response(msb, lsb):
    """Create two mock responses for MSB/LSB pair."""
    msb_resp = MagicMock()
    if msb is not None:
        msb_resp.raw_value = MagicMock()
        msb_resp.raw_value.as_integer = msb
    else:
        msb_resp.raw_value = None
    lsb_resp = MagicMock()
    if lsb is not None:
        lsb_resp.raw_value = MagicMock()
        lsb_resp.raw_value.as_integer = lsb
    else:
        lsb_resp.raw_value = None
    return msb_resp, lsb_resp


# --- Tests for read_colour_temperature_limits_mirek ---


@pytest.mark.asyncio
async def test_read_limits_returns_four_values():
    driver = AsyncMock()
    actual_level = MagicMock()
    actual_level.raw_value = MagicMock()
    actual_level.raw_value.as_integer = 200
    # warmest = 0x0190 = 400, coolest = 0x0064 = 100
    # physical_warmest = 0x01F4 = 500, physical_coolest = 0x0032 = 50
    warmest_msb, warmest_lsb = _make_response(0x01, 0x90)
    coolest_msb, coolest_lsb = _make_response(0x00, 0x64)
    phys_warmest_msb, phys_warmest_lsb = _make_response(0x01, 0xF4)
    phys_coolest_msb, phys_coolest_lsb = _make_response(0x00, 0x32)

    # dtr0 responses are just acks (not checked)
    dtr0_resp = MagicMock()
    driver.send_commands = AsyncMock(
        return_value=[
            actual_level,  # 0: QueryActualLevel
            dtr0_resp,  # 1: DTR0 (warmest)
            warmest_msb,  # 2: QueryColourValue
            warmest_lsb,  # 3: QueryContentDTR0
            dtr0_resp,  # 4: DTR0 (coolest)
            coolest_msb,  # 5: QueryColourValue
            coolest_lsb,  # 6: QueryContentDTR0
            dtr0_resp,  # 7: DTR0 (phys warmest)
            phys_warmest_msb,  # 8: QueryColourValue
            phys_warmest_lsb,  # 9: QueryContentDTR0
            dtr0_resp,  # 10: DTR0 (phys coolest)
            phys_coolest_msb,  # 11: QueryColourValue
            phys_coolest_lsb,  # 12: QueryContentDTR0
        ]
    )
    result = await read_colour_temperature_limits_mirek(driver, GearShort(1))
    assert result.tc_min_mirek == 100
    assert result.tc_max_mirek == 400
    assert result.tc_phys_min_mirek == 50
    assert result.tc_phys_max_mirek == 500


@pytest.mark.asyncio
async def test_read_limits_defaults_none_to_extremes():
    driver = AsyncMock()
    actual_level = MagicMock()
    actual_level.raw_value = MagicMock()
    actual_level.raw_value.as_integer = 200
    # All None responses -> defaults
    none_resp = MagicMock()
    none_resp.raw_value = None
    dtr0_resp = MagicMock()
    driver.send_commands = AsyncMock(
        return_value=[
            actual_level,
            dtr0_resp,
            none_resp,
            none_resp,
            dtr0_resp,
            none_resp,
            none_resp,
            dtr0_resp,
            none_resp,
            none_resp,
            dtr0_resp,
            none_resp,
            none_resp,
        ]
    )
    result = await read_colour_temperature_limits_mirek(driver, GearShort(1))
    assert result.tc_min_mirek == MIN_TC_MIREK
    assert result.tc_max_mirek == MAX_TC_MIREK
    assert result.tc_phys_min_mirek == MIN_TC_MIREK
    assert result.tc_phys_max_mirek == MAX_TC_MIREK


# --- Tests for TcLimitsSettings ---


def _make_limits(cool=100, warm=400, phys_cool=50, phys_warm=500):
    return Type8TcLimits(
        tc_min_mirek=cool,
        tc_max_mirek=warm,
        tc_phys_min_mirek=phys_cool,
        tc_phys_max_mirek=phys_warm,
    )


def _make_reread_response(warmest, coolest, phys_warmest, phys_coolest):
    """Build 13-item response list for read_colour_temperature_limits_mirek."""
    actual_level = MagicMock()
    actual_level.raw_value = MagicMock()
    actual_level.raw_value.as_integer = 200
    dtr0_resp = MagicMock()

    def pair(val):
        return _make_response((val >> 8) & 0xFF, val & 0xFF)

    w_msb, w_lsb = pair(warmest)
    c_msb, c_lsb = pair(coolest)
    pw_msb, pw_lsb = pair(phys_warmest)
    pc_msb, pc_lsb = pair(phys_coolest)
    return [
        actual_level,
        dtr0_resp,
        w_msb,
        w_lsb,
        dtr0_resp,
        c_msb,
        c_lsb,
        dtr0_resp,
        pw_msb,
        pw_lsb,
        dtr0_resp,
        pc_msb,
        pc_lsb,
    ]


@pytest.mark.asyncio
async def test_tc_limits_read_mutates_shared_limits():
    limits = _make_limits()
    handler = TcLimitsSettings(limits)
    driver = AsyncMock()

    # New values: cool=120, warm=380, phys_cool=130, phys_warm=480
    driver.send_commands = AsyncMock(return_value=_make_reread_response(380, 120, 480, 130))
    result = await handler.read(driver, GearShort(1))
    assert result == {
        "tc_limits": {
            "tc_coolest": 120,
            "tc_warmest": 380,
            "tc_physical_coolest": 130,
            "tc_physical_warmest": 480,
        }
    }
    # Shared limits mutated in-place
    assert limits.tc_min_mirek == 120
    assert limits.tc_max_mirek == 380
    assert limits.tc_phys_min_mirek == 130
    assert limits.tc_phys_max_mirek == 480


async def _tc_handler(tc_min_mirek: int, tc_max_mirek: int) -> Type8Parameters:
    """A DT8 handler that identified its colour type as Tc and read back the given user limits
    (physical ones left at the extremes), the way ``read_mandatory_info`` discovers them."""
    status = MagicMock()
    status.raw_value = MagicMock(error=False)
    status.colour_type_xy_active = False
    status.colour_type_colour_temperature_Tc_active = True
    status.colour_type_primary_N_active = False
    driver = AsyncMock()
    driver.send = AsyncMock(return_value=status)
    driver.send_commands = AsyncMock(
        return_value=_make_reread_response(tc_max_mirek, tc_min_mirek, MAX_TC_MIREK, MIN_TC_MIREK)
    )
    handler = Type8Parameters()
    await handler.read_mandatory_info(driver, GearShort(1))
    return handler


def _predicted_tc(handler: Type8Parameters, mirek: int) -> int:
    """The mirek the handler puts in the event it predicts from a sniffed Tc command."""
    event = handler.apply_observed_colour({ColourComponent.COLOUR_TEMPERATURE: mirek}, settle_at=0.0)
    return event.components[ColourComponent.COLOUR_TEMPERATURE]


# --- Tests for the UI range the limits are declared within ---


def _slider_range_k(tc_min_mirek: int, tc_max_mirek: int) -> tuple[int, int]:
    """The min and max, in K, of the wanted-colour-temperature control built for these limits."""
    limits = Type8TcLimits(tc_min_mirek=tc_min_mirek, tc_max_mirek=tc_max_mirek)
    meta = get_wanted_mqtt_controls(limits)[0].control_info.state.meta
    return meta.minimum, meta.maximum


async def _card(warmest, coolest, phys_warmest, phys_coolest) -> dict:
    """The limits as the settings card declares them after reading these raw mirek words."""
    driver = AsyncMock()
    driver.send_commands = AsyncMock(
        return_value=_make_reread_response(warmest, coolest, phys_warmest, phys_coolest)
    )
    return (await TcLimitsSettings(_make_limits()).read(driver, GearShort(1)))["tc_limits"]


def test_slider_keeps_limits_inside_the_ui_range():
    assert _slider_range_k(tc_kelvin_mirek(6500), tc_kelvin_mirek(2700)) == (2702, 6535)


def test_slider_clamps_a_limit_outside_the_ui_range():
    """Gear naming 800 K as its warmest: the slider starts at the UI edge instead."""
    assert _slider_range_k(tc_kelvin_mirek(6500), tc_kelvin_mirek(800)) == (UI_MIN_TC_K, 6535)


def test_slider_spans_the_ui_range_for_the_extreme_limits():
    """The 1 and 65534 mirek gear without limits of its own answers, and the values the read
    falls back to when it gets no answer: 1000...10000 K, not 15 K...1000000 K."""
    assert _slider_range_k(MIN_TC_MIREK, MAX_TC_MIREK) == (UI_MIN_TC_K, UI_MAX_TC_K)


def test_slider_spans_the_ui_range_for_unset_limits():
    assert _slider_range_k(MASK_2BYTES, MASK_2BYTES) == (UI_MIN_TC_K, UI_MAX_TC_K)


def test_slider_spans_the_ui_range_when_the_gear_span_lies_outside_it():
    """Gear that only does 800...900 K: both ends land on the same UI edge, so the slider takes
    the whole range rather than collapsing to a point."""
    assert _slider_range_k(tc_kelvin_mirek(900), tc_kelvin_mirek(800)) == (UI_MIN_TC_K, UI_MAX_TC_K)


def test_slider_spans_the_ui_range_for_inverted_limits():
    assert _slider_range_k(tc_kelvin_mirek(2700), tc_kelvin_mirek(6500)) == (UI_MIN_TC_K, UI_MAX_TC_K)


def test_slider_survives_a_zero_limit():
    """A zero answer used to raise in the Kelvin conversion and leave the device uninitialised."""
    assert _slider_range_k(0, tc_kelvin_mirek(2700)) == (2702, UI_MAX_TC_K)


def test_slider_takes_the_ui_edge_only_on_the_side_with_the_unset_limit():
    """An unset limit is the largest mirek there is, so clamping it would move the coolest end
    to the warm edge instead of the cool one."""
    assert _slider_range_k(MASK_2BYTES, tc_kelvin_mirek(2700)) == (2702, UI_MAX_TC_K)


def test_slider_takes_the_ui_edge_only_on_the_side_with_the_impossible_limit():
    assert _slider_range_k(tc_kelvin_mirek(6500), 0) == (UI_MIN_TC_K, 6535)


@pytest.mark.asyncio
async def test_card_clamps_limits_outside_the_ui_range():
    """The gear's 1 and 65534 mirek reach the card as the UI bounds, not as 1000000 K and 15 K."""
    card = await _card(MAX_TC_MIREK, MIN_TC_MIREK, MAX_TC_MIREK, MIN_TC_MIREK)
    assert card == {
        "tc_coolest": UI_MIN_TC_MIREK,
        "tc_warmest": UI_MAX_TC_MIREK,
        "tc_physical_coolest": UI_MIN_TC_MIREK,
        "tc_physical_warmest": UI_MAX_TC_MIREK,
    }


@pytest.mark.asyncio
async def test_card_keeps_limits_inside_the_ui_range():
    card = await _card(370, 153, 500, 110)
    assert (card["tc_coolest"], card["tc_warmest"]) == (153, 370)


@pytest.mark.asyncio
async def test_card_keeps_an_unset_limit_unset():
    """MASK is what the card's switch reads, so it must not become a colour temperature."""
    card = await _card(MASK_2BYTES, MASK_2BYTES, MASK_2BYTES, MASK_2BYTES)
    assert set(card.values()) == {MASK_2BYTES}


@pytest.mark.asyncio
async def test_tc_limits_injected_into_control():
    """The limits a Tc handler reads bound the set_colour_temperature slider and clamp what it
    predicts from a sniffed command, while a read is published exactly as the gear answered
    it."""
    handler = await _tc_handler(tc_min_mirek=120, tc_max_mirek=380)
    controls = {c.control_info.id: c for c in handler.get_mqtt_controls()}

    wanted_meta = controls[SET_COLOUR_TEMPERATURE].control_info.state.meta
    assert (wanted_meta.minimum, wanted_meta.maximum) == (tc_kelvin_mirek(380), tc_kelvin_mirek(120))

    control = controls[CURRENT_COLOUR_TEMPERATURE]
    predicted = handler.apply_observed_colour({ColourComponent.COLOUR_TEMPERATURE: 100}, settle_at=0.0)
    assert control.notify(predicted) is NotifyResult.PUBLISH_STATE
    assert control.control_info.state.value == str(tc_kelvin_mirek(120))  # below the coolest limit

    assert control.notify(_tc_read(100)) is NotifyResult.PUBLISH_STATE
    assert control.control_info.state.value == str(tc_kelvin_mirek(100))


def _tc_read(mirek: int) -> ColourChanged:
    return ColourChanged({ColourComponent.COLOUR_TEMPERATURE: mirek}, EventSource.READ)


@pytest.mark.asyncio
async def test_tc_prediction_clamped_to_the_ui_range_when_a_limit_is_not_implemented():
    """Gear without the Tc limit registers answers MASK, and clamping to that verbatim would
    report 15 K for every colour temperature: the UI range stands in for the unknown bound,
    while a bound the gear did give still clamps. A read passes through either way."""
    both_unknown = await _tc_handler(tc_min_mirek=MASK_2BYTES, tc_max_mirek=MASK_2BYTES)
    assert _predicted_tc(both_unknown, 250) == 250  # inside the stand-in range, not clamped to MASK
    assert _predicted_tc(both_unknown, UI_MAX_TC_MIREK + 500) == UI_MAX_TC_MIREK

    warmest_known = await _tc_handler(tc_min_mirek=MASK_2BYTES, tc_max_mirek=500)
    assert _predicted_tc(warmest_known, 250) == 250
    assert _predicted_tc(warmest_known, 600) == 500  # past the warmest limit the gear did report

    control = CurrentColourTemperatureControl()
    assert control.notify(_tc_read(600)) is NotifyResult.PUBLISH_STATE
    assert control.control_info.state.value == str(tc_kelvin_mirek(600))


def test_a_zero_colour_temperature_read_is_a_read_error():
    """0 mirek has no Kelvin: the control must flag the read instead of raising while it
    converts, and it must not overwrite the temperature it last had."""
    control = CurrentColourTemperatureControl()
    control.notify(_tc_read(250))
    assert control.notify(_tc_read(0)) is NotifyResult.PUBLISH_STATE
    assert control.control_info.state.error is ControlError.READ
    assert control.control_info.state.value == str(tc_kelvin_mirek(250))


@pytest.mark.asyncio
async def test_tc_limits_write_empty_input_returns_empty():
    limits = _make_limits()
    handler = TcLimitsSettings(limits)
    driver = AsyncMock()
    result = await handler.write(driver, GearShort(1), {})
    assert result == {}
    driver.send_commands.assert_not_called()


@pytest.mark.asyncio
async def test_tc_limits_write_no_changes_returns_empty():
    limits = _make_limits(cool=100, warm=400, phys_cool=110, phys_warm=500)
    handler = TcLimitsSettings(limits)
    driver = AsyncMock()
    result = await handler.write(
        driver,
        GearShort(1),
        {
            "tc_limits": {
                "tc_coolest": 100,
                "tc_warmest": 400,
                "tc_physical_coolest": 110,
                "tc_physical_warmest": 500,
            }
        },
    )
    assert result == {}
    driver.send_commands.assert_not_called()


@pytest.mark.asyncio
async def test_tc_limits_write_single_field_sends_correct_commands():
    limits = _make_limits(cool=100, warm=400, phys_cool=110, phys_warm=500)
    handler = TcLimitsSettings(limits)
    driver = AsyncMock()
    reread_resp = _make_reread_response(400, 150, 500, 110)

    async def mock_send_commands(cmds, _source=None, priority=None):
        del priority
        if len(cmds) == 4:
            return [MagicMock() for _ in cmds]
        return reread_resp

    driver.send_commands = AsyncMock(side_effect=mock_send_commands)

    result = await handler.write(
        driver,
        GearShort(1),
        {
            "tc_limits": {
                "tc_coolest": 150,
                "tc_warmest": 400,
                "tc_physical_coolest": 110,
                "tc_physical_warmest": 500,
            }
        },
    )
    # Should have sent one store command batch + one re-read batch
    assert driver.send_commands.call_count == 2
    # First call should be 4 commands: DTR0, DTR1, DTR2, Store
    first_call_cmds = driver.send_commands.call_args_list[0][0][0]
    assert len(first_call_cmds) == 4
    assert isinstance(first_call_cmds[0], DTR0)
    assert isinstance(first_call_cmds[1], DTR1)
    assert isinstance(first_call_cmds[2], DTR2)
    assert isinstance(first_call_cmds[3], StoreColourTemperatureTcLimit)
    assert result == {
        "tc_limits": {
            "tc_coolest": 150,
            "tc_warmest": 400,
            "tc_physical_coolest": 110,
            "tc_physical_warmest": 500,
        }
    }


@pytest.mark.asyncio
async def test_tc_limits_write_physical_before_user():
    """Physical limits are sent before user limits (per plan section 4)."""
    limits = _make_limits(cool=100, warm=400, phys_cool=50, phys_warm=500)
    handler = TcLimitsSettings(limits)
    driver = AsyncMock()
    reread_resp = _make_reread_response(420, 80, 520, 40)
    sent_selectors = []

    async def mock_send_commands(cmds, _source=None, priority=None):
        del priority
        if len(cmds) == 4 and isinstance(cmds[3], StoreColourTemperatureTcLimit):
            sent_selectors.append(cmds[2].param)
            return [MagicMock() for _ in cmds]
        return reread_resp

    driver.send_commands = AsyncMock(side_effect=mock_send_commands)

    # Change all 4 limits
    await handler.write(
        driver,
        GearShort(1),
        {
            "tc_limits": {
                "tc_coolest": 80,
                "tc_warmest": 420,
                "tc_physical_coolest": 40,
                "tc_physical_warmest": 520,
            }
        },
    )
    # Physical limits should be sent first
    assert sent_selectors == [
        StoreColourTemperatureTcLimitDTR2.TcPhysicalCoolest,
        StoreColourTemperatureTcLimitDTR2.TcPhysicalWarmest,
        StoreColourTemperatureTcLimitDTR2.TcCoolest,
        StoreColourTemperatureTcLimitDTR2.TcWarmest,
    ]


@pytest.mark.asyncio
async def test_tc_limits_write_broadcast_returns_empty():
    limits = _make_limits(cool=100, warm=400, phys_cool=50, phys_warm=500)
    handler = TcLimitsSettings(limits)
    driver = AsyncMock()
    driver.send_commands = AsyncMock(return_value=[MagicMock() for _ in range(4)])

    result = await handler.write(
        driver,
        GearBroadcast(),
        {
            "tc_limits": {
                "tc_coolest": 150,
                "tc_warmest": 400,
                "tc_physical_coolest": 50,
                "tc_physical_warmest": 500,
            }
        },
    )
    assert result == {}


@pytest.mark.asyncio
async def test_tc_limits_write_group_returns_empty():
    limits = _make_limits(cool=100, warm=400, phys_cool=50, phys_warm=500)
    handler = TcLimitsSettings(limits)
    driver = AsyncMock()
    driver.send_commands = AsyncMock(return_value=[MagicMock() for _ in range(4)])

    result = await handler.write(
        driver,
        GearGroup(0),
        {
            "tc_limits": {
                "tc_coolest": 150,
                "tc_warmest": 400,
                "tc_physical_coolest": 50,
                "tc_physical_warmest": 500,
            }
        },
    )
    assert result == {}


# --- Tests for has_changes ---


def test_has_changes_returns_false_when_missing():
    limits = _make_limits()
    handler = TcLimitsSettings(limits)
    assert handler.has_changes({}) is False


def test_has_changes_returns_false_when_same():
    limits = _make_limits(cool=100, warm=400, phys_cool=110, phys_warm=500)
    handler = TcLimitsSettings(limits)
    assert (
        handler.has_changes(
            {
                "tc_limits": {
                    "tc_coolest": 100,
                    "tc_warmest": 400,
                    "tc_physical_coolest": 110,
                    "tc_physical_warmest": 500,
                }
            }
        )
        is False
    )


def test_has_changes_returns_true_when_different():
    limits = _make_limits(cool=100, warm=400, phys_cool=50, phys_warm=500)
    handler = TcLimitsSettings(limits)
    assert (
        handler.has_changes(
            {
                "tc_limits": {
                    "tc_coolest": 150,
                    "tc_warmest": 400,
                    "tc_physical_coolest": 50,
                    "tc_physical_warmest": 500,
                }
            }
        )
        is True
    )


def test_requires_mqtt_controls_refresh_is_true():
    limits = _make_limits()
    handler = TcLimitsSettings(limits)
    assert handler.requires_mqtt_controls_refresh is True


def test_get_schema_contains_all_fields():
    limits = _make_limits(cool=100, warm=400, phys_cool=50, phys_warm=500)
    handler = TcLimitsSettings(limits)
    schema = handler.get_schema(False)
    props = schema["properties"]["tc_limits"]["properties"]
    assert "tc_coolest" in props
    assert "tc_warmest" in props
    assert "tc_physical_coolest" in props
    assert "tc_physical_warmest" in props
    # Physical limits keep the absolute UI range as their outer static bound
    assert props["tc_physical_coolest"]["options"]["wb"]["dali_tc"]["minimum"] == UI_MIN_TC_MIREK
    assert props["tc_physical_coolest"]["options"]["wb"]["dali_tc"]["maximum"] == UI_MAX_TC_MIREK
    # User limits carry no static bounds — their range comes from the cross-referenced
    # live limits (see test_get_schema_limits_cross_reference).
    assert "minimum" not in props["tc_coolest"]["options"]["wb"]["dali_tc"]
    assert "maximum" not in props["tc_coolest"]["options"]["wb"]["dali_tc"]


def test_get_schema_limits_cross_reference():
    """The four limit sliders cross-reference each other so the nesting
    physical_coolest <= coolest <= warmest <= physical_warmest cannot invert: each
    field's inner bound points at its neighbour, while the physical sliders keep the
    static UI range on their outer side."""
    props = TcLimitsSettings(_make_limits()).get_schema(False)["properties"]["tc_limits"]["properties"]
    cool = props["tc_coolest"]["options"]["wb"]["dali_tc"]
    assert cool["min_limit"] == "tc_limits.tc_physical_coolest"
    assert cool["max_limit"] == "tc_limits.tc_warmest"
    warm = props["tc_warmest"]["options"]["wb"]["dali_tc"]
    assert warm["min_limit"] == "tc_limits.tc_coolest"
    assert warm["max_limit"] == "tc_limits.tc_physical_warmest"
    phys_cool = props["tc_physical_coolest"]["options"]["wb"]["dali_tc"]
    assert phys_cool["max_limit"] == "tc_limits.tc_physical_warmest"
    assert "min_limit" not in phys_cool  # outer side stays the static UI range
    phys_warm = props["tc_physical_warmest"]["options"]["wb"]["dali_tc"]
    assert phys_warm["min_limit"] == "tc_limits.tc_physical_coolest"
    assert "max_limit" not in phys_warm


def test_colour_value_schema_tracks_user_limits_live():
    """The main colour-temperature slider points at the live user-limit params and
    carries no static bounds; the frontend derives the range from them."""
    dali_tc = ColourTemperatureValue().get_schema(_make_limits())["properties"]["tc"]["options"]["wb"][
        "dali_tc"
    ]
    assert dali_tc["min_limit"] == "tc_limits.tc_coolest"
    assert dali_tc["max_limit"] == "tc_limits.tc_warmest"
    assert "minimum" not in dali_tc
    assert "maximum" not in dali_tc


# --- Helpers for rebuild_mqtt_controls / ApplyResult / sync_controls_after_broadcast ---

# Prevent file system access in DaliDeviceBase.__init__
DaliDeviceBase._common_schema = {"title": "test-schema", "properties": {}}  # pylint: disable=protected-access


class _TestDevice(DaliDeviceBase):  # pylint: disable=too-many-instance-attributes
    """Concrete subclass that implements _build_mqtt_controls for testing."""

    def __init__(self, *args, mqtt_controls_factory=None, extra_param_handlers=None, **kwargs):
        self._mqtt_controls_factory = mqtt_controls_factory or (lambda: [])
        self._extra_param_handlers = extra_param_handlers or []
        super().__init__(*args, **kwargs)

    def _build_mqtt_controls(self) -> list[MqttControlBase]:
        return self._mqtt_controls_factory()

    async def _initialize_impl(self, driver):
        return InitializationResult(self._extra_param_handlers, [], self._build_mqtt_controls())


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
    return _TestDevice(**defaults)


def _make_readable_control(control_id):
    return ReadableControl(
        ControlInfo(
            control_id, ControlState(ControlMeta("range", TranslatedTitle(control_id, control_id)), "0")
        )
    )


def _make_writable_control(control_id):
    return MqttControl(
        ControlInfo(
            control_id, ControlState(ControlMeta("range", TranslatedTitle(control_id, control_id)), "0")
        ),
        commands_builder=lambda addr, val: [],
    )


def _make_handler(
    name="handler",
    requires_refresh=False,
    has_changes_val=False,
    read_return=None,
    write_return=None,
):
    handler = MagicMock(spec=SettingsParamBase)
    handler.name = SettingsParamName(name)
    handler.requires_mqtt_controls_refresh = requires_refresh
    handler.has_changes = MagicMock(return_value=has_changes_val)
    handler.read = AsyncMock(return_value=read_return or {})
    handler.write = AsyncMock(return_value=write_return or {})
    handler.get_schema = MagicMock(return_value=None)
    return handler


# --- Tests for rebuild_mqtt_controls ---


def test_rebuild_mqtt_controls_does_not_call_driver():
    """rebuild_mqtt_controls must not perform any I/O."""
    c1 = _make_readable_control("ctrl_a")
    c2 = _make_writable_control("ctrl_b")
    d = _make_device(mqtt_controls_factory=lambda: [c1, c2])
    d.is_initialized = True
    d._parameter_handlers = []  # pylint: disable=protected-access

    with patch("wb.mqtt_dali.common_dali_device.WBDALIDriver", autospec=True) as mock_driver_cls:
        d.rebuild_mqtt_controls()
        mock_driver_cls.assert_not_called()


def test_rebuild_mqtt_controls_updates_controls_dict():
    c1 = _make_readable_control("ctrl_x")
    c2 = _make_writable_control("ctrl_y")
    d = _make_device(mqtt_controls_factory=lambda: [c1, c2])
    d.is_initialized = True
    d._parameter_handlers = []  # pylint: disable=protected-access

    d.rebuild_mqtt_controls()

    controls = d._controls  # pylint: disable=protected-access
    assert "ctrl_x" in controls
    assert "ctrl_y" in controls
    assert len(controls) == 2


def test_rebuild_mqtt_controls_populates_pollables():
    readable = _make_readable_control("poll_me")
    writable = _make_writable_control("write_only")
    d = _make_device(mqtt_controls_factory=lambda: [readable, writable])
    d.is_initialized = True
    d._parameter_handlers = []  # pylint: disable=protected-access

    d.rebuild_mqtt_controls()

    pollables = d._pollables  # pylint: disable=protected-access
    assert len(pollables) == 1
    assert pollables[0] is readable


def test_rebuild_mqtt_controls_does_not_touch_schema():
    d = _make_device(mqtt_controls_factory=lambda: [])
    d.is_initialized = True
    d.schema = {"old": "schema"}

    d.rebuild_mqtt_controls()

    assert d.schema == {"old": "schema"}


# --- Tests for apply_parameters returning ApplyResult ---


@pytest.mark.asyncio
async def test_apply_parameters_returns_refresh_true_when_handler_with_flag_returns_nonempty():
    handler = _make_handler(
        name="tc_handler",
        requires_refresh=True,
        write_return={"tc_limits": {"tc_coolest": 100}},
    )
    d = _make_device(mqtt_controls_factory=lambda: [])
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = [handler]  # pylint: disable=protected-access
    d._apply_common_parameters = AsyncMock()  # pylint: disable=protected-access
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.jsonschema.validate"):
        result = await d.apply_parameters(driver, {"any": "value"})

    assert isinstance(result, ApplyResult)
    assert result.needs_mqtt_controls_refresh is True


@pytest.mark.asyncio
async def test_apply_parameters_returns_refresh_false_when_handler_with_flag_returns_empty():
    handler = _make_handler(
        name="tc_handler",
        requires_refresh=True,
        write_return={},
    )
    d = _make_device(mqtt_controls_factory=lambda: [])
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = [handler]  # pylint: disable=protected-access
    d._apply_common_parameters = AsyncMock()  # pylint: disable=protected-access
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.jsonschema.validate"):
        result = await d.apply_parameters(driver, {"any": "value"})

    assert isinstance(result, ApplyResult)
    assert result.needs_mqtt_controls_refresh is False


@pytest.mark.asyncio
async def test_apply_parameters_returns_refresh_false_when_handler_without_flag():
    handler = _make_handler(
        name="plain_handler",
        requires_refresh=False,
        write_return={"some_key": 42},
    )
    d = _make_device(mqtt_controls_factory=lambda: [])
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = [handler]  # pylint: disable=protected-access
    d._apply_common_parameters = AsyncMock()  # pylint: disable=protected-access
    driver = AsyncMock()

    with patch("wb.mqtt_dali.common_dali_device.jsonschema.validate"):
        result = await d.apply_parameters(driver, {"any": "value"})

    assert result.needs_mqtt_controls_refresh is False


# --- Tests for sync_controls_after_broadcast ---


@pytest.mark.asyncio
async def test_sync_reads_only_handlers_with_both_flags():
    """read() is called only on handlers with requires_mqtt_controls_refresh=True
    AND has_changes=True."""
    h_refresh_changed = _make_handler(name="refresh_changed", requires_refresh=True, has_changes_val=True)
    h_refresh_unchanged = _make_handler(
        name="refresh_unchanged", requires_refresh=True, has_changes_val=False
    )
    h_no_refresh_changed = _make_handler(
        name="no_refresh_changed", requires_refresh=False, has_changes_val=True
    )
    h_no_refresh_unchanged = _make_handler(
        name="no_refresh_unchanged", requires_refresh=False, has_changes_val=False
    )

    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = [  # pylint: disable=protected-access
        h_refresh_changed,
        h_refresh_unchanged,
        h_no_refresh_changed,
        h_no_refresh_unchanged,
    ]
    d.rebuild_mqtt_controls = MagicMock()
    driver = AsyncMock()

    await d.sync_controls_after_broadcast(driver, {"tc_limits": {"tc_coolest": 200}})

    h_refresh_changed.read.assert_awaited_once()
    h_refresh_unchanged.read.assert_not_awaited()
    h_no_refresh_changed.read.assert_not_awaited()
    h_no_refresh_unchanged.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_skips_handlers_without_requires_mqtt_controls_refresh():
    """Handlers that have has_changes=True but requires_mqtt_controls_refresh=False
    do not get read() called."""
    h_changed_no_refresh = _make_handler(
        name="changed_no_refresh", requires_refresh=False, has_changes_val=True
    )

    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = [h_changed_no_refresh]  # pylint: disable=protected-access
    d.rebuild_mqtt_controls = MagicMock()
    driver = AsyncMock()

    await d.sync_controls_after_broadcast(driver, {"some": "params"})

    h_changed_no_refresh.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_calls_rebuild_when_controls_updated():
    h = _make_handler(name="refresh_h", requires_refresh=True, has_changes_val=True)

    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1}
    d.schema = {"type": "object"}
    d._parameter_handlers = [h]  # pylint: disable=protected-access
    d.rebuild_mqtt_controls = MagicMock()
    driver = AsyncMock()

    result = await d.sync_controls_after_broadcast(driver, {"tc_limits": {}})

    assert result is True
    d.rebuild_mqtt_controls.assert_called_once()


@pytest.mark.asyncio
async def test_sync_invalidates_params_when_any_changed():
    h = _make_handler(name="changed_h", requires_refresh=False, has_changes_val=True)

    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1, "extra": "data"}
    d.schema = {"type": "object", "props": "here"}
    d._parameter_handlers = [h]  # pylint: disable=protected-access
    d.rebuild_mqtt_controls = MagicMock()
    driver = AsyncMock()

    await d.sync_controls_after_broadcast(driver, {"some": "params"})

    assert not d.params
    assert not d.schema


@pytest.mark.asyncio
async def test_sync_no_changes_no_read_no_invalidation():
    """When has_changes=False for all handlers, read() is not called
    and params are not invalidated."""
    h1 = _make_handler(name="h1", requires_refresh=True, has_changes_val=False)
    h2 = _make_handler(name="h2", requires_refresh=False, has_changes_val=False)

    d = _make_device()
    d.is_initialized = True
    d.params = {"short_address": 1, "keep": "me"}
    d.schema = {"type": "object"}
    d._parameter_handlers = [h1, h2]  # pylint: disable=protected-access
    d.rebuild_mqtt_controls = MagicMock()
    driver = AsyncMock()

    result = await d.sync_controls_after_broadcast(driver, {"tc_limits": {}})

    assert result is False
    h1.read.assert_not_awaited()
    h2.read.assert_not_awaited()
    d.rebuild_mqtt_controls.assert_not_called()
    assert d.params == {"short_address": 1, "keep": "me"}
    assert d.schema == {"type": "object"}
