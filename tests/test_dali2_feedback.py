from __future__ import annotations

import logging
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
import pytest
from dali.address import (
    Device,
    DeviceShort,
    FeatureDevice,
    FeatureInstanceNumber,
    InstanceNumber,
)
from dali.command import Command, NumericResponse, Response
from dali.device.general import (
    DTR0,
    QueryDeviceGroupsEightToFifteen,
    QueryDeviceGroupsSixteenToTwentyThree,
    QueryDeviceGroupsTwentyFourToThirtyOne,
    QueryDeviceGroupsZeroToSeven,
    QueryFeatureType,
    QueryInstanceType,
    QueryNextFeatureType,
    QueryNumberOfInstances,
)
from dali.frame import BackwardFrame

from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali2_device import Dali2Device, InstanceParameters, InstanceTypeParam
from wb.mqtt_dali.dali2_type32_parameters import (
    pack_feedback_timing,
    unpack_feedback_timing,
)
from wb.mqtt_dali.device.feedback import (
    ActivateFeedback,
    QueryActiveFeedbackBrightness,
    QueryFeedbackActive,
    QueryFeedbackCapability,
    QueryFeedbackTiming,
    SetActiveFeedbackBrightness,
    SetFeedbackTiming,
    StopFeedback,
)
from wb.mqtt_dali.send_command import (
    build_command_registry,
    list_commands,
    parse_and_build_command,
)

# pylint: disable=protected-access

# Prevent file system access in DaliDeviceBase.__init__
DaliDeviceBase._common_schema = {"title": "test-schema", "properties": {}}


# Capability bits per IEC 62386-332:2017 Table 1
BIT_VISIBLE = 1 << 0
BIT_BRIGHTNESS = 1 << 1
BIT_COLOUR = 1 << 2
BIT_AUDIBLE = 1 << 3
BIT_VOLUME = 1 << 4
BIT_PITCH = 1 << 5

CAPABILITY_FULL = BIT_VISIBLE | BIT_BRIGHTNESS | BIT_COLOUR | BIT_AUDIBLE | BIT_VOLUME | BIT_PITCH
CAPABILITY_VISIBLE_BRIGHTNESS = BIT_VISIBLE | BIT_BRIGHTNESS
CAPABILITY_AUDIBLE_VOLUME_PITCH = BIT_AUDIBLE | BIT_VOLUME | BIT_PITCH


def _numeric(value: int) -> Response:
    return NumericResponse(BackwardFrame(value))


def _no_response() -> Response:
    return Response(None)


_DEVICE_GROUP_QUERIES = (
    QueryDeviceGroupsZeroToSeven,
    QueryDeviceGroupsEightToFifteen,
    QueryDeviceGroupsSixteenToTwentyThree,
    QueryDeviceGroupsTwentyFourToThirtyOne,
)


def _bare_dali2_device(short: int = 1) -> Dali2Device:
    return Dali2Device(
        DaliDeviceAddress(short=short, random=0x123456),
        bus_id="bus_test",
        gtin_db=MagicMock(),
    )


class FakeDriver:
    """Driver stub that dispatches commands to a scripted response table.

    Per-instance scripts are keyed by instance number; the device-level
    script is shared across `Device()` / `FeatureDevice()` (the device-level
    discovery scope uses the latter for capability/Set/Query and the former
    for feature-type queries).

    Each per-scope dict maps a command class to either a single Response
    (returned every call) or a list of Responses (consumed in order; once
    exhausted, returns no-answer).
    """

    def __init__(
        self,
        num_instances: int,
        instance_types: dict[int, int],
        per_instance: Optional[dict[int, dict[type, object]]] = None,
        device_level: Optional[dict[type, object]] = None,
    ) -> None:
        self.num_instances = num_instances
        self.instance_types = instance_types
        self.per_instance = per_instance or {}
        self.device_level = device_level or {}
        self.calls: list[Command] = []
        self._iters: dict[tuple[object, type], object] = {}

    async def send(self, cmd: Command, *_args, **_kwargs) -> Response:
        self.calls.append(cmd)
        return self._dispatch(cmd)

    def _dispatch(self, cmd: Command) -> Response:
        if isinstance(cmd, QueryNumberOfInstances):
            return _numeric(self.num_instances)
        if isinstance(cmd, QueryInstanceType):
            return _numeric(self.instance_types.get(cmd.instance.value, 0))
        if isinstance(cmd, _DEVICE_GROUP_QUERIES):
            return _numeric(0)
        instance_attr = getattr(cmd, "instance", None)
        if isinstance(instance_attr, (Device, FeatureDevice)):
            return self._dispatch_scripted("__device__", self.device_level, cmd)
        if instance_attr is None or instance_attr.value is None:
            return _no_response()
        inst = instance_attr.value
        return self._dispatch_scripted(inst, self.per_instance.get(inst, {}), cmd)

    def _dispatch_scripted(self, scope_key: object, scripted: dict[type, object], cmd: Command) -> Response:
        for cls, value in scripted.items():
            if not isinstance(cmd, cls):
                continue
            if not isinstance(value, list):
                return value
            key = (scope_key, cls)
            if key not in self._iters:
                self._iters[key] = iter(value)
            try:
                return next(self._iters[key])
            except StopIteration:
                return _no_response()
        return _no_response()

    async def send_commands(self, cmds, *_args, **_kwargs) -> list[Response]:
        return [await self.send(c) for c in cmds]


# ----------------------------------------------------------------------------
# Discovery on settings-read path (capability cached on init)
# ----------------------------------------------------------------------------


def _instance_params(instance_type: int = 0) -> InstanceParameters:
    return InstanceParameters(InstanceNumber(0), instance_type)


FEATURE_TYPE_NONE = 254
FEATURE_TYPE_MASK = 255


def _make_feature_script(features: list[int], capability: Optional[int]) -> dict:
    """Build a feature-discovery script per IEC 62386-103 §11.9.14.

    Empty list → QueryFeatureType returns 254 ("no features"). One feature →
    QueryFeatureType returns the feature number directly, no QueryNextFeatureType.
    Multiple features → QueryFeatureType returns MASK (255), then
    QueryNextFeatureType walks the rest until 254.

    The same shape works for instance-scope and device-level scope; pass it
    to FakeDriver as either `per_instance[N]` or `device_level`.
    """
    if not features:
        script: dict[type, object] = {
            QueryFeatureType: [_numeric(FEATURE_TYPE_NONE)],
            QueryNextFeatureType: [_numeric(FEATURE_TYPE_NONE)],
        }
    elif len(features) == 1:
        script = {
            QueryFeatureType: [_numeric(features[0])],
            QueryNextFeatureType: [_numeric(FEATURE_TYPE_NONE)],
        }
    else:
        next_responses = [_numeric(ft) for ft in features] + [_numeric(FEATURE_TYPE_NONE)]
        script = {
            QueryFeatureType: [_numeric(FEATURE_TYPE_MASK)],
            QueryNextFeatureType: next_responses,
        }
    if capability is not None:
        script[QueryFeedbackCapability] = _numeric(capability)
    return script


# Backwards-compatible alias for tests that read more naturally with the old name.
_make_per_instance_script = _make_feature_script


@pytest.mark.asyncio
async def test_discovery_adds_feedback_params_with_full_capability():
    params = _instance_params(instance_type=0)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script([32], CAPABILITY_FULL)},
    )

    schema_before = params.get_schema(group_and_broadcast=False)
    assert "active_feedback_brightness" not in schema_before["properties"]["instance0"]["properties"]

    await params.discover_feedback(driver, DeviceShort(5), logging.getLogger("test"))

    # Stub child param reads — we only care about discovery side effects.
    with patch.object(InstanceTypeParam, "read", new=AsyncMock(return_value={})):
        with patch(
            "wb.mqtt_dali.dali2_parameters.InstanceParam.read",
            new=AsyncMock(return_value={}),
        ), patch(
            "wb.mqtt_dali.dali2_device.InstanceActiveParam.read",
            new=AsyncMock(return_value={}),
        ), patch(
            "wb.mqtt_dali.dali2_device.EventPriorityParam.read",
            new=AsyncMock(return_value={}),
        ), patch(
            "wb.mqtt_dali.dali2_device.EventSchemeParam.read",
            new=AsyncMock(return_value={}),
        ), patch(
            "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read",
            new=AsyncMock(return_value={}),
        ):
            await params.read(driver, DeviceShort(5), logging.getLogger("test"))

    schema_after = params.get_schema(group_and_broadcast=False)
    instance_props = schema_after["properties"]["instance0"]["properties"]
    assert "active_feedback_brightness" in instance_props
    assert "inactive_feedback_brightness" in instance_props
    assert "active_feedback_colour" in instance_props
    assert "inactive_feedback_colour" in instance_props
    assert "active_feedback_volume" in instance_props
    assert "active_feedback_pitch" in instance_props


@pytest.mark.asyncio
async def test_dali2_feedback_force_reload_does_not_re_discover():
    """After cold-init discovery, a second load_info(force_reload=True) reads
    parameter values but does not re-issue QueryFeatureType /
    QueryNextFeatureType / QueryFeedbackCapability for any scope. Capability
    is ROM-stable; re-querying it would waste bus time."""
    device = _bare_dali2_device(short=2)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script([32], CAPABILITY_FULL)},
        device_level=_make_feature_script([32], CAPABILITY_FULL),
    )

    async def stub_read(*_args, **_kwargs):
        return {}

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as mock_general, patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.PowerCycleNotificationParam.read",
        new=AsyncMock(side_effect=stub_read),
    ), patch(
        "wb.mqtt_dali.dali2_device.DeviceGroupsParam.read", new=AsyncMock(side_effect=stub_read)
    ):
        general_handler = MagicMock()
        general_handler.read = AsyncMock(return_value={})
        general_handler.get_schema = MagicMock(return_value={})
        mock_general.return_value = general_handler

        await device.load_info(driver)
        first_discovery_calls = sum(
            1
            for c in driver.calls
            if isinstance(c, (QueryFeatureType, QueryNextFeatureType, QueryFeedbackCapability))
        )
        assert first_discovery_calls > 0

        before_calls = len(driver.calls)
        await device.load_info(driver, force_reload=True)
        new_calls = driver.calls[before_calls:]

    new_classes = [type(c) for c in new_calls]
    assert QueryFeatureType not in new_classes
    assert QueryNextFeatureType not in new_classes
    assert QueryFeedbackCapability not in new_classes


@pytest.mark.asyncio
async def test_dali2_feedback_repeated_load_info_no_extra_bus_traffic():
    """A second load_info without force_reload must NOT re-query feature
    types or capability — the device-level params cache short-circuits the
    whole read path. Regression test for the discovery contract."""
    device = _bare_dali2_device(short=8)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script([32], CAPABILITY_FULL)},
    )

    async def stub_read(*_args, **_kwargs):
        return {}

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as mock_general, patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.PowerCycleNotificationParam.read",
        new=AsyncMock(side_effect=stub_read),
    ), patch(
        "wb.mqtt_dali.dali2_device.DeviceGroupsParam.read", new=AsyncMock(side_effect=stub_read)
    ):
        general_handler = MagicMock()
        general_handler.read = AsyncMock(return_value={})
        general_handler.get_schema = MagicMock(return_value={})
        mock_general.return_value = general_handler

        await device.load_info(driver)
        first_discovery_calls = sum(
            1
            for c in driver.calls
            if isinstance(c, (QueryFeatureType, QueryNextFeatureType, QueryFeedbackCapability))
        )
        assert first_discovery_calls > 0

        before_calls = len(driver.calls)
        await device.load_info(driver)
        new_calls = driver.calls[before_calls:]

    new_classes = [type(c) for c in new_calls]
    assert QueryFeatureType not in new_classes
    assert QueryNextFeatureType not in new_classes
    assert QueryFeedbackCapability not in new_classes


# ----------------------------------------------------------------------------
# Capability filtering
# ----------------------------------------------------------------------------


async def _run_discovery_and_get_props(capability: Optional[int], features: list[int]) -> dict:
    params = _instance_params(instance_type=0)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script(features, capability)},
    )

    async def stub_read(*_args, **_kwargs):
        return {}

    await params.discover_feedback(driver, DeviceShort(1), logging.getLogger("test"))
    with patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub_read)
    ):
        await params.read(driver, DeviceShort(1), logging.getLogger("test"))

    return params.get_schema(False)["properties"]["instance0"]["properties"]


@pytest.mark.asyncio
async def test_dali2_feedback_no_feature_no_params():
    props = await _run_discovery_and_get_props(capability=None, features=[])
    for key in (
        "active_feedback_brightness",
        "inactive_feedback_brightness",
        "active_feedback_colour",
        "inactive_feedback_colour",
        "active_feedback_volume",
        "active_feedback_pitch",
    ):
        assert key not in props


@pytest.mark.asyncio
async def test_dali2_feedback_unknown_feature_ignored():
    # Both 32 and 33 in the list — 33 must be ignored, 32 still produces feedback params.
    props = await _run_discovery_and_get_props(capability=BIT_BRIGHTNESS, features=[32, 33])
    assert "active_feedback_brightness" in props
    assert "inactive_feedback_brightness" in props


@pytest.mark.asyncio
async def test_dali2_feedback_capability_filters_visible_only():
    props = await _run_discovery_and_get_props(capability=CAPABILITY_VISIBLE_BRIGHTNESS, features=[32])
    assert "active_feedback_brightness" in props
    assert "inactive_feedback_brightness" in props
    for key in (
        "active_feedback_colour",
        "inactive_feedback_colour",
        "active_feedback_volume",
        "active_feedback_pitch",
    ):
        assert key not in props


@pytest.mark.asyncio
async def test_dali2_feedback_capability_filters_audible_only():
    props = await _run_discovery_and_get_props(capability=CAPABILITY_AUDIBLE_VOLUME_PITCH, features=[32])
    assert "active_feedback_volume" in props
    assert "active_feedback_pitch" in props
    for key in (
        "active_feedback_brightness",
        "inactive_feedback_brightness",
        "active_feedback_colour",
        "inactive_feedback_colour",
    ):
        assert key not in props


@pytest.mark.asyncio
async def test_dali2_feedback_capability_full():
    props = await _run_discovery_and_get_props(capability=CAPABILITY_FULL, features=[32])
    for key in (
        "active_feedback_brightness",
        "inactive_feedback_brightness",
        "active_feedback_colour",
        "inactive_feedback_colour",
        "active_feedback_volume",
        "active_feedback_pitch",
    ):
        assert key in props


@pytest.mark.asyncio
async def test_dali2_feedback_capability_zero_hides_all_params():
    props = await _run_discovery_and_get_props(capability=0, features=[32])
    for key in (
        "active_feedback_brightness",
        "inactive_feedback_brightness",
        "active_feedback_colour",
        "inactive_feedback_colour",
        "active_feedback_volume",
        "active_feedback_pitch",
    ):
        assert key not in props


@pytest.mark.asyncio
async def test_dali2_feedback_capability_query_no_answer(caplog):
    # Force discovery to find feature 32, then return no answer for capability.
    params = _instance_params(instance_type=0)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={
            0: {
                QueryFeatureType: [_numeric(32)],
                QueryNextFeatureType: [_numeric(0)],
                # No QueryFeedbackCapability entry → falls through to _no_response()
            }
        },
    )

    async def stub_read(*_args, **_kwargs):
        return {}

    with caplog.at_level(logging.DEBUG, logger="test"):
        await params.discover_feedback(driver, DeviceShort(3), logging.getLogger("test"))

    with patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub_read)
    ):
        # Should not raise even though capability has no answer.
        await params.read(driver, DeviceShort(3), logging.getLogger("test"))

    schema = params.get_schema(False)["properties"]["instance0"]["properties"]
    for key in (
        "active_feedback_brightness",
        "inactive_feedback_brightness",
        "active_feedback_colour",
        "inactive_feedback_colour",
        "active_feedback_volume",
        "active_feedback_pitch",
    ):
        assert key not in schema
    assert any("capability" in record.message.lower() for record in caplog.records)


# ----------------------------------------------------------------------------
# Range / default assertions on JSON schema
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dali2_feedback_brightness_range():
    props = await _run_discovery_and_get_props(capability=CAPABILITY_FULL, features=[32])
    active = props["active_feedback_brightness"]
    inactive = props["inactive_feedback_brightness"]
    assert active["minimum"] == 0
    assert active["maximum"] == 255
    assert active["default"] == 255
    assert inactive["minimum"] == 0
    assert inactive["maximum"] == 255
    assert inactive["default"] == 0


@pytest.mark.asyncio
async def test_dali2_feedback_colour_range():
    props = await _run_discovery_and_get_props(capability=CAPABILITY_FULL, features=[32])
    active = props["active_feedback_colour"]
    inactive = props["inactive_feedback_colour"]
    assert active["minimum"] == 1
    assert active["maximum"] == 63
    assert active["default"] == 63
    assert inactive["minimum"] == 1
    assert inactive["maximum"] == 63
    assert inactive["default"] == 63


@pytest.mark.asyncio
async def test_dali2_feedback_volume_range():
    props = await _run_discovery_and_get_props(capability=CAPABILITY_FULL, features=[32])
    volume = props["active_feedback_volume"]
    assert volume["minimum"] == 0
    assert volume["maximum"] == 255
    assert volume["default"] == 255


@pytest.mark.asyncio
async def test_dali2_feedback_pitch_range():
    props = await _run_discovery_and_get_props(capability=CAPABILITY_FULL, features=[32])
    pitch = props["active_feedback_pitch"]
    assert pitch["minimum"] == 0
    assert pitch["maximum"] == 255
    assert pitch["default"] == 128


# ----------------------------------------------------------------------------
# End-to-end load_info path
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dali2_feedback_feature_discovered_in_load_info():
    """After load_info() the device.schema reflects feedback params for instance with feature 32."""
    device = _bare_dali2_device(short=6)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script([32], CAPABILITY_FULL)},
    )

    async def stub_read(*_args, **_kwargs):
        return {}

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as mock_general, patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.PowerCycleNotificationParam.read",
        new=AsyncMock(side_effect=stub_read),
    ), patch(
        "wb.mqtt_dali.dali2_device.DeviceGroupsParam.read", new=AsyncMock(side_effect=stub_read)
    ):
        general_handler = MagicMock()
        general_handler.read = AsyncMock(return_value={})
        general_handler.get_schema = MagicMock(return_value={})
        mock_general.return_value = general_handler
        await device.load_info(driver)

    instance_props = device.schema["properties"]["instance0"]["properties"]
    assert "active_feedback_brightness" in instance_props
    assert "active_feedback_pitch" in instance_props


# ----------------------------------------------------------------------------
# Schema enum / MQTT controls hygiene
# ----------------------------------------------------------------------------


def test_instance_type_names_does_not_contain_32():
    assert 32 not in InstanceTypeParam.INSTANCE_TYPE_NAMES
    schema = InstanceTypeParam(0).get_schema(False)
    enum_values = schema["properties"]["instance_type"]["enum"]
    assert 32 not in enum_values
    titles = schema["properties"]["instance_type"]["options"]["enum_titles"]
    assert "Feedback" not in titles


# ----------------------------------------------------------------------------
# Feedback timing structure
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dali2_feedback_timing_three_subfields():
    props = await _run_discovery_and_get_props(capability=CAPABILITY_FULL, features=[32])
    assert "feedback_timing" in props
    timing_card = props["feedback_timing"]
    assert timing_card["format"] == "card"
    assert "feedback_timing_duty_cycle" in timing_card["properties"]
    assert "feedback_timing_period" in timing_card["properties"]
    assert "feedback_timing_cycles" in timing_card["properties"]
    assert "feedback_timing" not in timing_card["properties"]
    duty = timing_card["properties"]["feedback_timing_duty_cycle"]
    period = timing_card["properties"]["feedback_timing_period"]
    cycles = timing_card["properties"]["feedback_timing_cycles"]
    assert duty["enum"] == list(range(8))
    assert period["enum"] == list(range(8))
    assert cycles["enum"] == list(range(4))


@pytest.mark.asyncio
async def test_dali2_feedback_timing_card_shown_with_zero_capability():
    """Plan: timing card is always shown if capability was retrieved (any value)."""
    props = await _run_discovery_and_get_props(capability=0, features=[32])
    assert "feedback_timing" in props


@pytest.mark.asyncio
async def test_dali2_feedback_timing_card_hidden_without_capability():
    """If capability query has no answer, no feedback params (including timing) appear."""
    props = await _run_discovery_and_get_props(capability=None, features=[32])
    assert "feedback_timing" not in props


async def _load_device_with_feedback(device: Dali2Device) -> None:
    """Run device.load_info with all non-feedback handlers stubbed out."""
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script([32], CAPABILITY_FULL)},
    )

    async def stub(*_args, **_kwargs):
        return {}

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as mock_general, patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub)
    ), patch("wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub)), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub)
    ), patch(
        "wb.mqtt_dali.dali2_device.PowerCycleNotificationParam.read",
        new=AsyncMock(side_effect=stub),
    ), patch(
        "wb.mqtt_dali.dali2_device.DeviceGroupsParam.read", new=AsyncMock(side_effect=stub)
    ):
        general_handler = MagicMock()
        general_handler.read = AsyncMock(return_value={})
        general_handler.write = AsyncMock(return_value={})
        general_handler.get_schema = MagicMock(return_value={})
        general_handler.requires_mqtt_controls_refresh = False
        general_handler.name = MagicMock(en="general")
        mock_general.return_value = general_handler
        await device.load_info(driver)


def _make_capture_driver() -> tuple[MagicMock, list]:
    """Return a driver mock that records every command and answers
    QueryFeedbackTiming with 0xFF; everything else gets a no-response."""
    sent: list = []

    async def capture_send(cmd, *_args, **_kwargs):
        sent.append(cmd)
        if isinstance(cmd, QueryFeedbackTiming):
            return _numeric(0xFF)
        return _no_response()

    async def capture_send_commands(cmds, *_args, **_kwargs):
        return [await capture_send(cmd) for cmd in cmds]

    driver = MagicMock()
    driver.send = AsyncMock(side_effect=capture_send)
    driver.send_commands = AsyncMock(side_effect=capture_send_commands)
    return driver, sent


@pytest.mark.asyncio
async def test_dali2_feedback_timing_pack_unpack_through_apply_parameters():
    """End-to-end: dispatched dict shape goes through device.apply_parameters,
    pin the wire-level encoding (DTR0(packed) + SetFeedbackTiming) and
    decoding (QueryFeedbackTiming -> three values)."""

    device = _bare_dali2_device(short=7)
    await _load_device_with_feedback(device)

    capture_driver, sent = _make_capture_driver()

    payload = {
        "instance0": {
            "feedback_timing": {
                "feedback_timing_duty_cycle": 3,
                "feedback_timing_period": 2,
                "feedback_timing_cycles": 1,
            }
        }
    }

    async def stub(*_args, **_kwargs):
        return {}

    with patch("wb.mqtt_dali.dali2_parameters.InstanceParam.write", new=AsyncMock(side_effect=stub)), patch(
        "wb.mqtt_dali.dali2_device.InstanceActiveParam.write", new=AsyncMock(side_effect=stub)
    ), patch("wb.mqtt_dali.dali2_device.EventPriorityParam.write", new=AsyncMock(side_effect=stub)), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.write", new=AsyncMock(side_effect=stub)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.write", new=AsyncMock(side_effect=stub)
    ), patch(
        "wb.mqtt_dali.dali2_device.PowerCycleNotificationParam.write",
        new=AsyncMock(side_effect=stub),
    ), patch(
        "wb.mqtt_dali.dali2_device.DeviceGroupsParam.write", new=AsyncMock(side_effect=stub)
    ), patch(
        "wb.mqtt_dali.common_dali_device.jsonschema.validate"
    ), patch.object(
        type(device), "_apply_common_parameters", new=AsyncMock()
    ):
        await device.apply_parameters(capture_driver, payload)

    # DTR0 carries the packed byte: cycles<<6 | period<<3 | duty.
    # duty=3 (0b011), period=2 (0b010), cycles=1 (0b01) = 0b01_010_011 = 0x53.
    expected_packed = (1 << 6) | (2 << 3) | 3
    dtr0_cmds = [c for c in sent if isinstance(c, DTR0)]
    assert dtr0_cmds and any(getattr(c, "param", None) == expected_packed for c in dtr0_cmds)
    assert any(isinstance(c, SetFeedbackTiming) for c in sent)
    assert any(isinstance(c, QueryFeedbackTiming) for c in sent)
    # The read-back QueryFeedbackTiming responded with 0xFF -> (7, 7, 3).
    timing_param = device.instances[0]._parameters[-1]
    assert timing_param.value == {
        "feedback_timing_duty_cycle": 7,
        "feedback_timing_period": 7,
        "feedback_timing_cycles": 3,
    }


@pytest.mark.asyncio
async def test_dali2_feedback_timing_card_schema_accepts_three_subfield_payload():
    """Pin the timing card schema shape: a dict keyed by the three suffixed
    sub-fields under `feedback_timing` validates against device.schema.
    Catches schema/data shape regressions that the apply_parameters test
    can't see because it patches jsonschema.validate."""
    device = _bare_dali2_device(short=11)
    await _load_device_with_feedback(device)

    instance_schema = device.schema["properties"]["instance0"]
    timing_card = instance_schema["properties"]["feedback_timing"]
    timing_payload = {
        "feedback_timing_duty_cycle": 3,
        "feedback_timing_period": 2,
        "feedback_timing_cycles": 1,
    }
    jsonschema.validate(instance=timing_payload, schema=timing_card)


def test_dali2_feedback_timing_unpack_byte_helper():
    assert unpack_feedback_timing(0xFF) == (7, 7, 3)
    assert unpack_feedback_timing(0x00) == (0, 0, 0)
    assert pack_feedback_timing(3, 2, 1) == 0b01_010_011
    assert pack_feedback_timing(7, 7, 3) == 0xFF
    # Round-trip
    for duty in range(8):
        for period in range(8):
            for cycles in range(4):
                assert unpack_feedback_timing(pack_feedback_timing(duty, period, cycles)) == (
                    duty,
                    period,
                    cycles,
                )


# ----------------------------------------------------------------------------
# Feature-type discovery markers (IEC 62386-103 §11.9.14 / §11.9.15)
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dali2_feature_type_254_treated_as_no_features():
    """254 is the "no feature" marker — list is empty, no QueryNextFeatureType."""
    params = _instance_params(instance_type=0)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={
            0: {
                QueryFeatureType: [_numeric(FEATURE_TYPE_NONE)],
                QueryNextFeatureType: [_numeric(FEATURE_TYPE_NONE)],
            }
        },
    )

    await params.discover_feedback(driver, DeviceShort(1), logging.getLogger("test"))

    sent_classes = [type(c) for c in driver.calls]
    assert QueryFeatureType in sent_classes
    assert QueryNextFeatureType not in sent_classes


@pytest.mark.asyncio
async def test_dali2_feature_type_mask_iterates_next_until_254():
    """MASK (255) means "multiple features"; iterate via QueryNextFeatureType
    until 254 (terminator). All returned numbers go into the list."""
    params = _instance_params(instance_type=0)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={
            0: {
                QueryFeatureType: [_numeric(FEATURE_TYPE_MASK)],
                QueryNextFeatureType: [
                    _numeric(32),
                    _numeric(33),
                    _numeric(FEATURE_TYPE_NONE),
                ],
                QueryFeedbackCapability: _numeric(BIT_BRIGHTNESS),
            }
        },
    )

    async def stub_read(*_args, **_kwargs):
        return {}

    await params.discover_feedback(driver, DeviceShort(1), logging.getLogger("test"))
    with patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub_read)
    ):
        await params.read(driver, DeviceShort(1), logging.getLogger("test"))

    next_calls = [c for c in driver.calls if isinstance(c, QueryNextFeatureType)]
    # Three QueryNextFeatureType calls: 32, 33, then 254 terminator.
    assert len(next_calls) == 3
    # Feature 32 → feedback discovered → capability queried → params installed.
    instance_props = params.get_schema(False)["properties"]["instance0"]["properties"]
    assert "active_feedback_brightness" in instance_props


@pytest.mark.asyncio
async def test_dali2_feature_type_single_value():
    """A response in [32..96] is a single feature; QueryNextFeatureType is not called."""
    params = _instance_params(instance_type=0)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={
            0: {
                QueryFeatureType: [_numeric(32)],
                QueryNextFeatureType: [_numeric(FEATURE_TYPE_NONE)],
                QueryFeedbackCapability: _numeric(BIT_BRIGHTNESS),
            }
        },
    )

    async def stub_read(*_args, **_kwargs):
        return {}

    await params.discover_feedback(driver, DeviceShort(1), logging.getLogger("test"))
    with patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub_read)
    ):
        await params.read(driver, DeviceShort(1), logging.getLogger("test"))

    next_calls = [c for c in driver.calls if isinstance(c, QueryNextFeatureType)]
    assert next_calls == []
    instance_props = params.get_schema(False)["properties"]["instance0"]["properties"]
    assert "active_feedback_brightness" in instance_props


# ----------------------------------------------------------------------------
# Heuristic capability fallback for firmware that violates Part 103
# ----------------------------------------------------------------------------


async def _run_with_script(
    script: dict,
    instance: int = 0,
) -> tuple[InstanceParameters, FakeDriver]:
    params = _instance_params(instance_type=0)
    driver = FakeDriver(
        num_instances=1,
        instance_types={instance: 0},
        per_instance={instance: script},
    )

    async def stub_read(*_args, **_kwargs):
        return {}

    await params.discover_feedback(driver, DeviceShort(1), logging.getLogger("test"))
    with patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub_read)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub_read)
    ):
        await params.read(driver, DeviceShort(1), logging.getLogger("test"))
    return params, driver


@pytest.mark.asyncio
async def test_dali2_feedback_heuristic_fallback_finds_feedback():
    """QueryFeatureType=254 (firmware ignores standard discovery) but capability
    answers via FeatureInstanceNumber → feedback gets discovered anyway."""
    params, _driver = await _run_with_script(
        {
            QueryFeatureType: [_numeric(FEATURE_TYPE_NONE)],
            QueryNextFeatureType: [_numeric(FEATURE_TYPE_NONE)],
            QueryFeedbackCapability: _numeric(BIT_BRIGHTNESS),
        }
    )
    instance_props = params.get_schema(False)["properties"]["instance0"]["properties"]
    assert "active_feedback_brightness" in instance_props


@pytest.mark.asyncio
async def test_dali2_feedback_heuristic_fallback_silent_keeps_no_params():
    """Both standard discovery and the heuristic capability probe return
    no answer → no feedback params, no exception."""
    params, _driver = await _run_with_script(
        {
            QueryFeatureType: [_numeric(FEATURE_TYPE_NONE)],
            QueryNextFeatureType: [_numeric(FEATURE_TYPE_NONE)],
            # No QueryFeedbackCapability entry → falls through to no-response.
        }
    )
    instance_props = params.get_schema(False)["properties"]["instance0"]["properties"]
    assert "active_feedback_brightness" not in instance_props


@pytest.mark.asyncio
async def test_dali2_feedback_heuristic_fallback_skipped_when_features_found_without_32():
    """Standard discovery returned features but feature 32 is not among them
    → the heuristic capability probe must NOT be issued. Even if the firmware
    would have answered (capability is wired here just to make a false
    positive observable), no QueryFeedbackCapability hits the wire."""
    _params, driver = await _run_with_script(_make_per_instance_script([33], capability=BIT_BRIGHTNESS))
    capability_calls = [c for c in driver.calls if isinstance(c, QueryFeedbackCapability)]
    assert capability_calls == []


# ----------------------------------------------------------------------------
# Feature-instance addressing on Part 332 commands
# ----------------------------------------------------------------------------


def _instance_byte(cmd: Command) -> int:
    """Extract the second byte (instance byte) of the 24-bit forward frame."""
    return (cmd.frame.as_integer >> 8) & 0xFF


def test_dali2_feedback_commands_use_feature_instance_addressing():
    """All Part 332 Set/Query/Activate/Stop commands built for instance N use
    instance byte 0x20+N (FeatureInstanceNumber), not 0x00+N (InstanceNumber).
    """
    addr = DeviceShort(5)
    # FeatureInstanceNumber instance byte: 0x20 | N.
    for instance_n in (0, 3, 7):
        feature_addr = FeatureInstanceNumber(instance_n)
        for command_cls in (
            ActivateFeedback,
            StopFeedback,
            SetFeedbackTiming,
            SetActiveFeedbackBrightness,
            QueryFeedbackCapability,
            QueryFeedbackTiming,
            QueryActiveFeedbackBrightness,
        ):
            cmd = command_cls(addr, feature_addr)
            assert _instance_byte(cmd) == 0x20 | instance_n, (
                f"{command_cls.__name__} with FeatureInstanceNumber({instance_n}) "
                f"produced instance byte {_instance_byte(cmd):#x}, expected {0x20 | instance_n:#x}"
            )


@pytest.mark.asyncio
async def test_dali2_feedback_capability_command_uses_feature_addressing_on_wire():
    """End-to-end check on the wire: the capability query that the discovery
    actually sends carries FeatureInstanceNumber instance byte 0x20+N."""
    params = _instance_params(instance_type=0)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={
            0: {
                QueryFeatureType: [_numeric(32)],
                QueryNextFeatureType: [_numeric(FEATURE_TYPE_NONE)],
                QueryFeedbackCapability: _numeric(BIT_BRIGHTNESS),
            }
        },
    )

    await params.discover_feedback(driver, DeviceShort(1), logging.getLogger("test"))

    capability_calls = [c for c in driver.calls if isinstance(c, QueryFeedbackCapability)]
    assert capability_calls, "expected a QueryFeedbackCapability call on the wire"
    assert _instance_byte(capability_calls[0]) == 0x20  # FeatureInstanceNumber(0)


# ----------------------------------------------------------------------------
# Device-level (top-level `feedback` card) discovery
# ----------------------------------------------------------------------------


async def _run_load_info(device: Dali2Device, driver: FakeDriver) -> None:
    async def stub(*_args, **_kwargs):
        return {}

    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as mock_general, patch(
        "wb.mqtt_dali.dali2_parameters.InstanceParam.read", new=AsyncMock(side_effect=stub)
    ), patch("wb.mqtt_dali.dali2_device.InstanceActiveParam.read", new=AsyncMock(side_effect=stub)), patch(
        "wb.mqtt_dali.dali2_device.EventPriorityParam.read", new=AsyncMock(side_effect=stub)
    ), patch(
        "wb.mqtt_dali.dali2_device.EventSchemeParam.read", new=AsyncMock(side_effect=stub)
    ), patch(
        "wb.mqtt_dali.dali2_device.InstanceGroupParamBase.read", new=AsyncMock(side_effect=stub)
    ), patch(
        "wb.mqtt_dali.dali2_device.PowerCycleNotificationParam.read",
        new=AsyncMock(side_effect=stub),
    ), patch(
        "wb.mqtt_dali.dali2_device.DeviceGroupsParam.read", new=AsyncMock(side_effect=stub)
    ):
        general_handler = MagicMock()
        general_handler.read = AsyncMock(return_value={})
        general_handler.get_schema = MagicMock(return_value={})
        mock_general.return_value = general_handler
        await device.load_info(driver)


@pytest.mark.asyncio
async def test_dali2_feedback_device_level_discovered_in_load_info():
    """Device with a device-level Part 332 feature gets a top-level `feedback`
    card alongside `power_cycle_notification`, not nested under any instance."""
    device = _bare_dali2_device(short=20)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_feature_script([], None)},
        device_level=_make_feature_script([32], CAPABILITY_FULL),
    )
    await _run_load_info(device, driver)

    top_level_props = device.schema["properties"]
    assert "feedback" in top_level_props
    feedback_card = top_level_props["feedback"]
    # The card should expose the same feedback param keys.
    assert "active_feedback_brightness" in feedback_card["properties"]
    assert "feedback_timing" in feedback_card["properties"]
    # And the card lives at the top, not nested inside instance0.
    instance0 = top_level_props["instance0"]["properties"]
    assert "feedback" not in instance0


@pytest.mark.asyncio
async def test_dali2_feedback_device_level_no_feature_keeps_card_absent():
    """Without device-level feature 32 the top-level `feedback` card is absent."""
    device = _bare_dali2_device(short=21)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_feature_script([], None)},
        device_level=_make_feature_script([], None),
    )
    await _run_load_info(device, driver)

    assert "feedback" not in device.schema["properties"]


@pytest.mark.asyncio
async def test_dali2_feedback_device_level_heuristic_fallback():
    """Standard QueryFeatureType(Device()) returns 254, but
    QueryFeedbackCapability(FeatureDevice()) answers — card still appears."""
    device = _bare_dali2_device(short=22)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_feature_script([], None)},
        device_level={
            QueryFeatureType: [_numeric(FEATURE_TYPE_NONE)],
            QueryNextFeatureType: [_numeric(FEATURE_TYPE_NONE)],
            QueryFeedbackCapability: _numeric(BIT_VOLUME | BIT_PITCH),
        },
    )
    await _run_load_info(device, driver)

    feedback_card = device.schema["properties"].get("feedback")
    assert feedback_card is not None
    assert "active_feedback_volume" in feedback_card["properties"]
    assert "active_feedback_pitch" in feedback_card["properties"]


@pytest.mark.asyncio
async def test_dali2_feedback_device_level_uses_feature_device_addressing():
    """Device-level Set/Query/capability commands carry instance byte 0xFC
    (FeatureDevice). FeatureType discovery uses 0xFE (Device)."""
    device = _bare_dali2_device(short=23)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_feature_script([], None)},
        device_level=_make_feature_script([32], CAPABILITY_FULL),
    )
    await _run_load_info(device, driver)

    feature_type_calls = [c for c in driver.calls if isinstance(c, QueryFeatureType)]
    capability_calls = [c for c in driver.calls if isinstance(c, QueryFeedbackCapability)]
    # At least one device-level feature-type query (instance byte 0xFE) and
    # one device-level capability query (instance byte 0xFC).
    assert any(_instance_byte(c) == 0xFE for c in feature_type_calls)
    assert any(_instance_byte(c) == 0xFC for c in capability_calls)


@pytest.mark.asyncio
async def test_dali2_feedback_device_and_instance_coexist():
    """A device with both per-instance (visible) and device-level (audible)
    feedback shows both cards independently with their own capability bits."""
    device = _bare_dali2_device(short=24)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_feature_script([32], CAPABILITY_VISIBLE_BRIGHTNESS)},
        device_level=_make_feature_script([32], CAPABILITY_AUDIBLE_VOLUME_PITCH),
    )
    await _run_load_info(device, driver)

    top_level = device.schema["properties"]
    assert "feedback" in top_level  # device-level audible
    device_card = top_level["feedback"]["properties"]
    assert "active_feedback_volume" in device_card
    assert "active_feedback_pitch" in device_card
    assert "active_feedback_brightness" not in device_card
    instance0 = top_level["instance0"]["properties"]
    assert "active_feedback_brightness" in instance0
    assert "active_feedback_volume" not in instance0


# ----------------------------------------------------------------------------
# S3: cold-init feature-type discovery + runtime pushbutton controls
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dali2_initialize_runs_feature_type_discovery():
    """Cold init queries QueryFeatureType for every instance and for the
    device-level scope, plus QueryFeedbackCapability for confirmed scopes."""
    device = _bare_dali2_device(short=30)
    driver = FakeDriver(
        num_instances=2,
        instance_types={0: 0, 1: 0},
        per_instance={
            0: _make_per_instance_script([32], CAPABILITY_FULL),
            1: _make_per_instance_script([32], CAPABILITY_FULL),
        },
        device_level=_make_feature_script([32], CAPABILITY_FULL),
    )

    await device._initialize_impl(driver)

    feature_type_calls = [c for c in driver.calls if isinstance(c, QueryFeatureType)]
    capability_calls = [c for c in driver.calls if isinstance(c, QueryFeedbackCapability)]
    feature_type_bytes = {_instance_byte(c) for c in feature_type_calls}
    capability_bytes = {_instance_byte(c) for c in capability_calls}
    # Per-instance QueryFeatureType uses InstanceNumber (0x00, 0x01) and the
    # device-level scope uses Device() (0xFE).
    assert {0x00, 0x01, 0xFE}.issubset(feature_type_bytes)
    # Capability is feature-addressed: FeatureInstanceNumber (0x20, 0x21) and
    # FeatureDevice (0xFC).
    assert {0x20, 0x21, 0xFC}.issubset(capability_bytes)


@pytest.mark.asyncio
async def test_dali2_feedback_runtime_controls_published_at_init():
    """Feature 32 + non-zero capability → activate_feedback{N} and
    stop_feedback{N} appear in MQTT controls right after _initialize_impl."""
    device = _bare_dali2_device(short=31)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script([32], CAPABILITY_FULL)},
    )

    await device._initialize_impl(driver)
    controls = device._build_mqtt_controls()
    ids = [c.control_info.id for c in controls]
    assert "activate_feedback0" in ids
    assert "stop_feedback0" in ids


@pytest.mark.asyncio
async def test_dali2_feedback_runtime_controls_not_published_if_capability_zero():
    """Feature 32 reported but capability bits = 0 → no runtime controls."""
    device = _bare_dali2_device(short=32)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script([32], capability=0)},
    )

    await device._initialize_impl(driver)
    controls = device._build_mqtt_controls()
    ids = [c.control_info.id for c in controls]
    assert all(not cid.startswith("activate_feedback") for cid in ids)
    assert all(not cid.startswith("stop_feedback") for cid in ids)


@pytest.mark.asyncio
async def test_dali2_feedback_runtime_controls_not_published_without_feature():
    """Instance without feature 32 (and capability silent) → no runtime controls."""
    device = _bare_dali2_device(short=33)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script([], capability=None)},
    )

    await device._initialize_impl(driver)
    controls = device._build_mqtt_controls()
    ids = [c.control_info.id for c in controls]
    assert all(not cid.startswith("activate_feedback") for cid in ids)
    assert all(not cid.startswith("stop_feedback") for cid in ids)


@pytest.mark.asyncio
async def test_dali2_feedback_runtime_device_level_controls_published():
    """Device-level feature 32 → top-level activate_feedback / stop_feedback
    (no numeric suffix) appear after init."""
    device = _bare_dali2_device(short=34)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: _make_per_instance_script([], capability=None)},
        device_level=_make_feature_script([32], CAPABILITY_FULL),
    )

    await device._initialize_impl(driver)
    controls = device._build_mqtt_controls()
    ids = [c.control_info.id for c in controls]
    assert "activate_feedback" in ids
    assert "stop_feedback" in ids


def _captured_command_driver() -> tuple[MagicMock, list]:
    """Driver mock that records every command sent through send/send_commands.
    No retries because _no_response() is not a transmission error."""
    sent: list = []

    async def capture_send(cmd, *_args, **_kwargs):
        sent.append(cmd)
        return _no_response()

    async def capture_send_commands(cmds, *_args, **_kwargs):
        return [await capture_send(cmd) for cmd in cmds]

    driver = MagicMock()
    driver.send = AsyncMock(side_effect=capture_send)
    driver.send_commands = AsyncMock(side_effect=capture_send_commands)
    return driver, sent


async def _build_initialised_device(
    short: int,
    per_instance: Optional[dict[int, dict]] = None,
    device_level: Optional[dict] = None,
) -> Dali2Device:
    """Helper: run _initialize_impl with a FakeDriver, returning the device
    with controls built. Avoids the load_info path so we can pin the post-init
    state directly."""
    device = _bare_dali2_device(short=short)
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance=per_instance,
        device_level=device_level,
    )
    await device._initialize_impl(driver)
    device.rebuild_mqtt_controls()
    device.is_initialized = True
    return device


@pytest.mark.asyncio
async def test_dali2_feedback_runtime_activate_pushbutton_sends_activate_under_feature_addressing():
    """Pressing activate_feedback{N} sends ActivateFeedback with instance byte 0x20+N."""
    device = await _build_initialised_device(
        short=35,
        per_instance={0: _make_per_instance_script([32], CAPABILITY_FULL)},
    )

    capture_driver, sent = _captured_command_driver()
    await device.execute_control(capture_driver, "activate_feedback0", "1")

    activate_cmds = [c for c in sent if isinstance(c, ActivateFeedback)]
    assert activate_cmds, "expected ActivateFeedback to be emitted"
    assert _instance_byte(activate_cmds[0]) == 0x20  # FeatureInstanceNumber(0)


@pytest.mark.asyncio
async def test_dali2_feedback_runtime_stop_pushbutton_sends_stop_under_feature_addressing():
    """Pressing stop_feedback{N} sends StopFeedback with instance byte 0x20+N."""
    device = await _build_initialised_device(
        short=36,
        per_instance={0: _make_per_instance_script([32], CAPABILITY_FULL)},
    )

    capture_driver, sent = _captured_command_driver()
    await device.execute_control(capture_driver, "stop_feedback0", "1")

    stop_cmds = [c for c in sent if isinstance(c, StopFeedback)]
    assert stop_cmds, "expected StopFeedback to be emitted"
    assert _instance_byte(stop_cmds[0]) == 0x20


@pytest.mark.asyncio
async def test_dali2_feedback_runtime_device_level_pushbuttons_use_feature_device_addressing():
    """Device-level activate_feedback / stop_feedback use instance byte 0xFC (FeatureDevice)."""
    device = await _build_initialised_device(
        short=37,
        per_instance={0: _make_per_instance_script([], None)},
        device_level=_make_feature_script([32], CAPABILITY_FULL),
    )

    capture_driver, sent = _captured_command_driver()
    await device.execute_control(capture_driver, "activate_feedback", "1")
    await device.execute_control(capture_driver, "stop_feedback", "1")

    activate_cmds = [c for c in sent if isinstance(c, ActivateFeedback)]
    stop_cmds = [c for c in sent if isinstance(c, StopFeedback)]
    assert activate_cmds and _instance_byte(activate_cmds[0]) == 0xFC
    assert stop_cmds and _instance_byte(stop_cmds[0]) == 0xFC


@pytest.mark.asyncio
async def test_dali2_feedback_runtime_no_query_feedback_active_polling():
    """Build mqtt controls + polling list never includes QueryFeedbackActive —
    runtime feedback state is not observable, by design."""
    device = await _build_initialised_device(
        short=38,
        per_instance={0: _make_per_instance_script([32], CAPABILITY_FULL)},
        device_level=_make_feature_script([32], CAPABILITY_FULL),
    )

    capture_driver, sent = _captured_command_driver()
    await device.poll_controls(capture_driver)

    assert all(not isinstance(c, QueryFeedbackActive) for c in sent)


@pytest.mark.asyncio
async def test_dali2_feedback_runtime_init_survives_discovery_no_answer():
    """No answer on either standard discovery or the heuristic-fallback
    capability probe: cold init still completes, is_initialized=True, no
    runtime feedback controls are published."""
    device = _bare_dali2_device(short=39)
    silent_script = {
        QueryFeatureType: [_numeric(FEATURE_TYPE_NONE)],
        QueryNextFeatureType: [_numeric(FEATURE_TYPE_NONE)],
        # No QueryFeedbackCapability entry — falls through to no-response.
    }
    driver = FakeDriver(
        num_instances=1,
        instance_types={0: 0},
        per_instance={0: silent_script},
        device_level=silent_script,
    )

    # Use the full initialize() so is_initialized flips to True via the public path.
    with patch("wb.mqtt_dali.common_dali_device.GeneralMemoryParams") as mock_general:
        general_handler = MagicMock()
        general_handler.read = AsyncMock(return_value={})
        general_handler.get_schema = MagicMock(return_value={})
        mock_general.return_value = general_handler
        await device.initialize(driver)

    assert device.is_initialized is True
    ids = [c.control_info.id for c in device._build_mqtt_controls()]
    assert all(not cid.startswith("activate_feedback") for cid in ids)
    assert all(not cid.startswith("stop_feedback") for cid in ids)


# ----------------------------------------------------------------------------
# CLI registry / parser (FF24.F32 prefix)
# ----------------------------------------------------------------------------


def test_send_command_registry_uses_f32_for_feedback():
    """Plan §S2: Part 332 commands are registered under FF24.F32.* — the old
    DT32 keys must be gone. DT-marker stays only on Part 30x instance types."""
    registry = build_command_registry()
    f32_keys = [k for k in registry if k.startswith("FF24.F32.")]
    dt32_keys = [k for k in registry if k.startswith("FF24.DT32")]
    assert f32_keys, "expected FF24.F32.* keys for feedback commands"
    assert not dt32_keys, f"unexpected DT32 keys still in registry: {dt32_keys}"
    # Spot-check a few representative commands.
    assert "FF24.F32.QueryFeedbackCapability" in registry
    assert "FF24.F32.Ix.QueryFeedbackCapability" in registry
    assert "FF24.F32.Ix.ActivateFeedback" in registry
    # list_commands renders an FF24.F32 section with a Feedback description.
    listing = list_commands(registry)
    assert "FF24.F32 (Feedback" in listing


def test_send_command_parses_f_prefix_with_instance():
    """FF24.F32.I<K>.<cmd> resolves to FeatureInstanceNumber(K) → instance byte 0x20+K."""
    registry = build_command_registry()
    cmd = parse_and_build_command("FF24.F32.I3.QueryFeedbackCapability", registry, address=5)
    assert isinstance(cmd, QueryFeedbackCapability)
    assert _instance_byte(cmd) == 0x20 | 3


def test_send_command_parses_f_prefix_without_instance():
    """FF24.F32.<cmd> with no I<K> resolves to FeatureDevice() → instance byte 0xFC."""
    registry = build_command_registry()
    cmd = parse_and_build_command("FF24.F32.QueryFeedbackCapability", registry, address=5)
    assert isinstance(cmd, QueryFeedbackCapability)
    assert _instance_byte(cmd) == 0xFC
