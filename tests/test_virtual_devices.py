"""Tests for capability-aware virtual device logic.

Covers:
- aggregate_capabilities
- build_virtual_device_controls
- DaliDevice.dt8_tc_limits
- _refresh_broadcast_device
- _refresh_group_virtual_devices (rebuild on capability change)
- group state source (composition, candidates, source pinning, error)
"""

import itertools
import logging
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.address import GearBroadcast
from dali.gear.colour import tc_kelvin_mirek
from dali.gear.general import DAPC

from wb.mqtt_dali.application_controller import ApplicationController
from wb.mqtt_dali.common_dali_device import ControlPollResult, MqttControlBase
from wb.mqtt_dali.dali_controls import ActualLevelControl, ErrorStatusControl
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveState, DimmingCurveType
from wb.mqtt_dali.dali_type8_parameters import ColourType
from wb.mqtt_dali.dali_type8_rgbwaf import get_mqtt_controls as rgbwaf_mqtt_controls
from wb.mqtt_dali.dali_type8_tc import MAX_TC_MIREK, MIN_TC_MIREK, Type8TcLimits
from wb.mqtt_dali.dali_type8_tc import get_mqtt_controls as tc_mqtt_controls
from wb.mqtt_dali.virtual_devices import (
    AggregatedCapabilities,
    BroadcastVirtualDevice,
    CandidatePollStatus,
    GroupStateUpdate,
    GroupStateUpdateKind,
    GroupVirtualDevice,
    aggregate_capabilities,
    build_virtual_device_controls,
    collect_group_state_controls,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_MQTT_ID_COUNTER = itertools.count(1)
_MOCK_SHORT_ADDRESS_COUNTER = itertools.count(1)
_MOCK_UID_COUNTER = itertools.count(1)


def _eligible_state_controls_for(colour_type, is_initialized):
    if not is_initialized:
        return []
    controls: list[MqttControlBase] = [ActualLevelControl(DimmingCurveState())]
    if colour_type == ColourType.RGBWAF:
        controls.extend(
            c for c in rgbwaf_mqtt_controls(only_setup_controls=False) if c.is_group_state_control
        )
    elif colour_type == ColourType.COLOUR_TEMPERATURE:
        controls.extend(c for c in tc_mqtt_controls(MIN_TC_MIREK, MAX_TC_MIREK) if c.is_group_state_control)
    return controls


def _make_device(  # pylint: disable=too-many-arguments, R0917
    *,
    is_initialized=True,
    groups=None,
    colour_type=None,
    tc_limits=None,
    dimming_curve_type=DimmingCurveType.LOGARITHMIC,
    mqtt_id=None,
    short_address=None,
    uid=None,
    state_controls=None,
):
    """Return a minimal mock DaliDevice-like object."""
    d = MagicMock()
    d.is_initialized = is_initialized
    d.groups = set(groups or [])
    d.dt8_colour_type = colour_type
    d.dt8_tc_limits = tc_limits
    d.dimming_curve_type = dimming_curve_type
    d.mqtt_id = mqtt_id if mqtt_id is not None else f"dev_{next(_MOCK_MQTT_ID_COUNTER)}"
    d.uid = uid if uid is not None else f"uid-{next(_MOCK_UID_COUNTER)}"
    d.address = MagicMock()
    d.address.short = short_address if short_address is not None else next(_MOCK_SHORT_ADDRESS_COUNTER)
    if state_controls is None:
        state_controls = _eligible_state_controls_for(colour_type, is_initialized)
    d.get_group_state_controls = MagicMock(return_value=list(state_controls))
    return d


def _make_publisher():
    pub = MagicMock()
    pub.add_device = AsyncMock()
    pub.remove_device = AsyncMock()
    pub.register_control_handler = AsyncMock()
    pub.set_control_value = AsyncMock()
    pub.set_control_error = AsyncMock()
    pub.has_device = MagicMock(return_value=False)
    return pub


def _make_controller(dali_devices=None):
    """Create a bare ApplicationController instance with minimal state."""
    ctrl = ApplicationController.__new__(ApplicationController)
    ctrl.logger = logging.getLogger("test")
    ctrl.uid = "bus_1"
    ctrl.bus_name = "Bus 1"
    ctrl.dali_devices = list(dali_devices or [])
    ctrl._device_publisher = _make_publisher()  # pylint: disable=protected-access
    ctrl._devices_by_mqtt_id = {}  # pylint: disable=protected-access
    ctrl._group_devices_by_number = {}  # pylint: disable=protected-access
    ctrl._broadcast_device = BroadcastVirtualDevice(  # pylint: disable=protected-access
        capabilities=AggregatedCapabilities(),
        mqtt_id_prefix=ctrl.uid,
        bus_name=ctrl.bus_name,
    )
    ctrl._handle_on_topic = AsyncMock()  # pylint: disable=protected-access
    return ctrl


# ---------------------------------------------------------------------------
# build_virtual_device_controls
# ---------------------------------------------------------------------------


class TestBuildVirtualDeviceControls:
    def test_no_capabilities_returns_base_controls(self):
        caps = AggregatedCapabilities()
        controls = build_virtual_device_controls(caps)
        # Basic controls come from make_controls() – dapc, on, off, up, down …
        assert "dapc" in controls
        assert "go_to_last_active_level" in controls
        assert "off" in controls

    def test_no_extra_colour_controls_without_dt8(self):
        caps = AggregatedCapabilities()
        controls = build_virtual_device_controls(caps)
        assert "set_colour_temperature" not in controls
        assert "set_rgb" not in controls

    def test_tc_caps_add_tc_control(self):
        caps = AggregatedCapabilities(has_dt8_tc=True, tc_min_mirek=153, tc_max_mirek=370)
        controls = build_virtual_device_controls(caps)
        assert "set_colour_temperature" in controls

    def test_rgbwaf_caps_add_rgb_controls(self):
        caps = AggregatedCapabilities(has_dt8_rgbwaf=True)
        controls = build_virtual_device_controls(caps)
        assert "set_rgb" in controls
        assert "set_white" in controls

    def test_both_caps_add_both_colour_controls(self):
        caps = AggregatedCapabilities(
            has_dt8_tc=True,
            has_dt8_rgbwaf=True,
            tc_min_mirek=153,
            tc_max_mirek=370,
        )
        controls = build_virtual_device_controls(caps)
        assert "set_colour_temperature" in controls
        assert "set_rgb" in controls

    def test_tc_limits_propagated_to_control_meta(self):
        """TC control min/max Kelvin values come from the aggregated limits."""
        caps = AggregatedCapabilities(has_dt8_tc=True, tc_min_mirek=200, tc_max_mirek=400)
        controls = build_virtual_device_controls(caps)
        tc_ctrl = controls["set_colour_temperature"]
        meta = tc_ctrl.control_info.meta
        # minimum Kelvin = kelvin of max mirek (coldest colour temperature)
        assert meta.minimum == tc_kelvin_mirek(400)
        # maximum Kelvin = kelvin of min mirek (warmest colour temperature)
        assert meta.maximum == tc_kelvin_mirek(200)

    def test_wanted_level_control_present(self):
        """wanted_level is present with default (empty) capabilities."""
        caps = AggregatedCapabilities()
        controls = build_virtual_device_controls(caps)
        assert "wanted_level" in controls

    def test_wanted_level_uses_linear_curve(self):
        """With LINEAR dimming curve, 50% yields DAPC raw close to 127."""
        caps = AggregatedCapabilities(dimming_curve_type=DimmingCurveType.LINEAR)
        controls = build_virtual_device_controls(caps)
        wanted_level = controls["wanted_level"]
        commands = wanted_level.get_setup_commands(GearBroadcast(), "50")
        assert len(commands) == 1
        assert isinstance(commands[0], DAPC)
        assert abs(commands[0].power - 127) <= 1

    def test_wanted_level_uses_log_curve_by_default(self):
        """With the default LOGARITHMIC curve, 50% yields DAPC raw around 229.

        The IEC 62386-102 logarithmic curve maps 50% light output to raw
        ~229 (inverse of 10^((level-1)/253*3 - 1) at level=50). This is
        markedly different from the linear 50% → raw 127, confirming the
        aggregated curve is applied to percent→DAPC conversion.
        """
        caps = AggregatedCapabilities()
        controls = build_virtual_device_controls(caps)
        wanted_level = controls["wanted_level"]
        commands = wanted_level.get_setup_commands(GearBroadcast(), "50")
        assert len(commands) == 1
        assert isinstance(commands[0], DAPC)
        assert abs(commands[0].power - 229) <= 1
        # And definitely not the linear value
        assert commands[0].power != 127


# ---------------------------------------------------------------------------
# aggregate_capabilities
# ---------------------------------------------------------------------------


class TestAggregateCapabilities:
    def _agg(self, devices):
        return aggregate_capabilities(devices)

    def test_empty_list_returns_defaults(self):
        caps = self._agg([])
        assert caps == AggregatedCapabilities()

    def test_uninitialised_devices_ignored(self):
        d = _make_device(is_initialized=False, colour_type=ColourType.RGBWAF)
        caps = self._agg([d])
        assert not caps.has_dt8_rgbwaf

    def test_rgbwaf_detected(self):
        d = _make_device(colour_type=ColourType.RGBWAF)
        caps = self._agg([d])
        assert caps.has_dt8_rgbwaf
        assert not caps.has_dt8_tc

    def test_tc_detected(self):
        limits = Type8TcLimits(
            tc_min_mirek=153, tc_max_mirek=370, tc_phys_min_mirek=MIN_TC_MIREK, tc_phys_max_mirek=MAX_TC_MIREK
        )
        d = _make_device(colour_type=ColourType.COLOUR_TEMPERATURE, tc_limits=limits)
        caps = self._agg([d])
        assert caps.has_dt8_tc
        assert not caps.has_dt8_rgbwaf

    def test_tc_limits_aggregated_across_devices(self):
        """tc_min_mirek = min across devices; tc_max_mirek = max across devices."""
        d1 = _make_device(
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=100,
                tc_max_mirek=300,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        d2 = _make_device(
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=150,
                tc_max_mirek=500,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        caps = self._agg([d1, d2])
        assert caps.tc_min_mirek == 100  # min of 100, 150
        assert caps.tc_max_mirek == 500  # max of 300, 500

    def test_tc_device_with_none_limits_uses_zero(self):
        """Device with TC type but no limits object contributes no limit values."""
        d = _make_device(colour_type=ColourType.COLOUR_TEMPERATURE, tc_limits=None)
        caps = self._agg([d])
        assert caps.has_dt8_tc
        assert caps.tc_min_mirek == 0
        assert caps.tc_max_mirek == 0

    def test_mixed_types_detected(self):
        d_rgb = _make_device(colour_type=ColourType.RGBWAF)
        d_tc = _make_device(
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=153,
                tc_max_mirek=370,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        caps = self._agg([d_rgb, d_tc])
        assert caps.has_dt8_rgbwaf
        assert caps.has_dt8_tc

    def test_unknown_colour_type_ignored(self):
        d = _make_device(colour_type=None)
        caps = self._agg([d])
        assert not caps.has_dt8_rgbwaf
        assert not caps.has_dt8_tc


# ---------------------------------------------------------------------------
# aggregate_capabilities – dimming curve aggregation
# ---------------------------------------------------------------------------


class TestAggregateCapabilitiesCurve:
    def test_all_logarithmic_aggregates_to_logarithmic(self):
        devices = [
            _make_device(dimming_curve_type=DimmingCurveType.LOGARITHMIC),
            _make_device(dimming_curve_type=DimmingCurveType.LOGARITHMIC),
        ]
        caps = aggregate_capabilities(devices)
        assert caps.dimming_curve_type == DimmingCurveType.LOGARITHMIC

    def test_all_linear_aggregates_to_linear(self):
        devices = [
            _make_device(dimming_curve_type=DimmingCurveType.LINEAR),
            _make_device(dimming_curve_type=DimmingCurveType.LINEAR),
        ]
        caps = aggregate_capabilities(devices)
        assert caps.dimming_curve_type == DimmingCurveType.LINEAR

    def test_mixed_curves_fall_back_to_logarithmic(self):
        devices = [
            _make_device(dimming_curve_type=DimmingCurveType.LINEAR),
            _make_device(dimming_curve_type=DimmingCurveType.LOGARITHMIC),
        ]
        caps = aggregate_capabilities(devices)
        assert caps.dimming_curve_type == DimmingCurveType.LOGARITHMIC

    def test_empty_list_falls_back_to_logarithmic(self):
        caps = aggregate_capabilities([])
        assert caps.dimming_curve_type == DimmingCurveType.LOGARITHMIC

    def test_uninitialised_devices_ignored_for_curve(self):
        """Uninitialised LINEAR device is ignored; result is default LOGARITHMIC."""
        devices = [
            _make_device(is_initialized=False, dimming_curve_type=DimmingCurveType.LINEAR),
        ]
        caps = aggregate_capabilities(devices)
        assert caps.dimming_curve_type == DimmingCurveType.LOGARITHMIC


# ---------------------------------------------------------------------------
# DaliDevice.dt8_tc_limits
# ---------------------------------------------------------------------------


class TestDaliDeviceDt8TcLimits:
    # pylint: disable=protected-access
    def _make_dali_device_with_handler(self, colour_type, limits=None):
        """Return a DaliDevice with a pre-configured _type8_handler mock."""
        dev = DaliDevice.__new__(DaliDevice)
        handler = MagicMock()
        handler.default_colour_type = colour_type
        handler.tc_limits = limits or Type8TcLimits(
            tc_min_mirek=0, tc_max_mirek=0, tc_phys_min_mirek=MIN_TC_MIREK, tc_phys_max_mirek=MAX_TC_MIREK
        )
        dev._type8_handler = handler
        return dev

    def test_returns_none_when_no_handler(self):
        dev = DaliDevice.__new__(DaliDevice)
        dev._type8_handler = None
        assert dev.dt8_tc_limits is None

    def test_returns_none_when_not_tc_type(self):
        dev = self._make_dali_device_with_handler(ColourType.RGBWAF)
        assert dev.dt8_tc_limits is None

    def test_returns_limits_when_tc_type(self):
        limits = Type8TcLimits(
            tc_min_mirek=153, tc_max_mirek=370, tc_phys_min_mirek=MIN_TC_MIREK, tc_phys_max_mirek=MAX_TC_MIREK
        )
        dev = self._make_dali_device_with_handler(ColourType.COLOUR_TEMPERATURE, limits)
        result = dev.dt8_tc_limits
        assert result is limits


# ---------------------------------------------------------------------------
# _refresh_broadcast_device
# ---------------------------------------------------------------------------


class TestRefreshBroadcastDevice:
    # pylint: disable=protected-access
    @pytest.mark.asyncio
    async def test_no_change_when_capabilities_unchanged(self):
        """If capabilities match, device is not republished."""
        ctrl = _make_controller()
        # Capabilities already empty; bus has no DT8 devices
        await ctrl._refresh_broadcast_device()
        ctrl._device_publisher.remove_device.assert_not_awaited()
        ctrl._device_publisher.add_device.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rebuilds_when_capabilities_change(self):
        """Adding a TC device triggers a broadcast device rebuild."""
        tc_device = _make_device(
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=153,
                tc_max_mirek=370,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        ctrl = _make_controller(dali_devices=[tc_device])

        await ctrl._refresh_broadcast_device()

        ctrl._device_publisher.remove_device.assert_awaited_once_with("bus_1_broadcast")
        ctrl._device_publisher.add_device.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rebuilt_device_has_tc_control(self):
        """Rebuilt broadcast device exposes the TC colour control."""
        tc_device = _make_device(
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=153,
                tc_max_mirek=370,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        ctrl = _make_controller(dali_devices=[tc_device])

        await ctrl._refresh_broadcast_device()

        control_ids = {c.id for c in ctrl._broadcast_device.get_mqtt_controls()}
        assert "set_colour_temperature" in control_ids

    @pytest.mark.asyncio
    async def test_rebuilt_device_registered_in_devices_map(self):
        """After rebuild, the new broadcast device is in _devices_by_mqtt_id."""
        tc_device = _make_device(
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=153,
                tc_max_mirek=370,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        ctrl = _make_controller(dali_devices=[tc_device])

        await ctrl._refresh_broadcast_device()

        assert ctrl._broadcast_device.mqtt_id in ctrl._devices_by_mqtt_id

    @pytest.mark.asyncio
    async def test_mqtt_id_preserved_after_rebuild(self):
        """The broadcast device keeps the same mqtt_id after a rebuild."""
        tc_device = _make_device(
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=153,
                tc_max_mirek=370,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        ctrl = _make_controller(dali_devices=[tc_device])
        old_id = ctrl._broadcast_device.mqtt_id

        await ctrl._refresh_broadcast_device()

        assert ctrl._broadcast_device.mqtt_id == old_id


# ---------------------------------------------------------------------------
# _refresh_group_virtual_devices – rebuild on capability change
# ---------------------------------------------------------------------------


class TestRefreshGroupVirtualDevices:
    # pylint: disable=protected-access
    @pytest.mark.asyncio
    async def test_new_group_is_published(self):
        """A device in group 1 causes a group-1 virtual device to be created."""
        dev = _make_device(groups=[1])
        ctrl = _make_controller(dali_devices=[dev])

        await ctrl._refresh_group_virtual_devices()

        assert 1 in ctrl._group_devices_by_number
        ctrl._device_publisher.add_device.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_removed_group_is_unpublished(self):
        """When no device belongs to group 2 any more, its virtual device is removed."""
        dev = _make_device(groups=[1])
        ctrl = _make_controller(dali_devices=[dev])
        # Manually pre-populate group 2
        old_device = GroupVirtualDevice(
            mqtt_id="bus_1_group_02",
            name="Bus 1 Group 2",
            capabilities=AggregatedCapabilities(),
            group_number=2,
            state_control_templates={},
            state_candidates={},
        )
        ctrl._group_devices_by_number[2] = old_device
        ctrl._devices_by_mqtt_id[old_device.mqtt_id] = old_device

        await ctrl._refresh_group_virtual_devices()

        assert 2 not in ctrl._group_devices_by_number
        ctrl._device_publisher.remove_device.assert_any_await("bus_1_group_02")

    @pytest.mark.asyncio
    async def test_unchanged_capabilities_no_rebuild(self):
        """Group device is not rebuilt when its capabilities have not changed."""
        dev = _make_device(groups=[1])
        ctrl = _make_controller(dali_devices=[dev])
        # First pass: create the group device
        await ctrl._refresh_group_virtual_devices()
        ctrl._device_publisher.reset_mock()

        # Second pass: same devices, same capabilities
        await ctrl._refresh_group_virtual_devices()

        ctrl._device_publisher.remove_device.assert_not_awaited()
        # add_device called 0 times (no rebuild, no new groups)
        ctrl._device_publisher.add_device.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_changed_capabilities_trigger_rebuild(self):
        """Group device is rebuilt when a new TC device joins the group."""
        dev_plain = _make_device(groups=[1])
        ctrl = _make_controller(dali_devices=[dev_plain])
        # Create initial group 1 device (plain, no DT8)
        await ctrl._refresh_group_virtual_devices()
        ctrl._device_publisher.reset_mock()

        # Add a TC device to group 1
        tc_dev = _make_device(
            groups=[1],
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=153,
                tc_max_mirek=370,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        ctrl.dali_devices.append(tc_dev)

        await ctrl._refresh_group_virtual_devices()

        # Old device removed, new one added
        ctrl._device_publisher.remove_device.assert_awaited_once()
        ctrl._device_publisher.add_device.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rebuilt_group_device_has_tc_control(self):
        """After capability change, rebuilt group exposes the TC control."""
        tc_dev = _make_device(
            groups=[3],
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=153,
                tc_max_mirek=370,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        ctrl = _make_controller(dali_devices=[tc_dev])

        await ctrl._refresh_group_virtual_devices()

        group_device = ctrl._group_devices_by_number[3]
        control_ids = {c.id for c in group_device.get_mqtt_controls()}
        assert "set_colour_temperature" in control_ids

    @pytest.mark.asyncio
    async def test_curve_type_change_triggers_rebuild(self):
        """Group device is rebuilt when a member's dimming curve type changes."""
        dev = _make_device(groups=[1], dimming_curve_type=DimmingCurveType.LOGARITHMIC)
        ctrl = _make_controller(dali_devices=[dev])
        await ctrl._refresh_group_virtual_devices()
        ctrl._device_publisher.reset_mock()

        # Flip the device's dimming curve type
        dev.dimming_curve_type = DimmingCurveType.LINEAR

        await ctrl._refresh_group_virtual_devices()

        ctrl._device_publisher.remove_device.assert_awaited_once()
        ctrl._device_publisher.add_device.assert_awaited_once()
        rebuilt = ctrl._group_devices_by_number[1]
        assert rebuilt.capabilities.dimming_curve_type == DimmingCurveType.LINEAR


# ---------------------------------------------------------------------------
# Helpers for group state-source tests
# ---------------------------------------------------------------------------


def _control_ids_of(virtual_device):
    return {c.id for c in virtual_device.get_mqtt_controls()}


# ---------------------------------------------------------------------------
# Group state-control composition
# ---------------------------------------------------------------------------


class TestGroupStateControlComposition:
    # pylint: disable=protected-access
    @pytest.mark.asyncio
    async def test_group_has_actual_level_when_any_member_initialized(self):
        dev = _make_device(groups=[1], mqtt_id="dev_1")
        ctrl = _make_controller(dali_devices=[dev])

        await ctrl._refresh_group_virtual_devices()

        assert "actual_level" in _control_ids_of(ctrl._group_devices_by_number[1])

    @pytest.mark.asyncio
    async def test_group_has_no_state_controls_when_no_members_initialized(self):
        dev = _make_device(groups=[1], mqtt_id="dev_1", is_initialized=False)
        ctrl = _make_controller(dali_devices=[dev])

        await ctrl._refresh_group_virtual_devices()

        ctrl_ids = _control_ids_of(ctrl._group_devices_by_number[1])
        assert "actual_level" not in ctrl_ids
        assert "current_rgb" not in ctrl_ids
        assert "current_white" not in ctrl_ids
        assert "current_colour_temperature" not in ctrl_ids

    @pytest.mark.asyncio
    async def test_group_has_tc_state_only_when_tc_member_present(self):
        plain = _make_device(groups=[1], mqtt_id="plain")
        ctrl = _make_controller(dali_devices=[plain])
        await ctrl._refresh_group_virtual_devices()
        plain_only_ids = _control_ids_of(ctrl._group_devices_by_number[1])
        assert "current_colour_temperature" not in plain_only_ids

        tc = _make_device(
            groups=[1],
            mqtt_id="tc",
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=153,
                tc_max_mirek=370,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        ctrl.dali_devices.append(tc)
        await ctrl._refresh_group_virtual_devices()

        with_tc_ids = _control_ids_of(ctrl._group_devices_by_number[1])
        assert "current_colour_temperature" in with_tc_ids

    @pytest.mark.asyncio
    async def test_group_has_rgbwaf_state_only_when_rgbwaf_member_present(self):
        plain = _make_device(groups=[1], mqtt_id="plain")
        ctrl = _make_controller(dali_devices=[plain])
        await ctrl._refresh_group_virtual_devices()
        plain_only_ids = _control_ids_of(ctrl._group_devices_by_number[1])
        assert "current_rgb" not in plain_only_ids
        assert "current_white" not in plain_only_ids

        rgb = _make_device(groups=[1], mqtt_id="rgb", colour_type=ColourType.RGBWAF)
        ctrl.dali_devices.append(rgb)
        await ctrl._refresh_group_virtual_devices()

        with_rgb_ids = _control_ids_of(ctrl._group_devices_by_number[1])
        assert "current_rgb" in with_rgb_ids
        assert "current_white" in with_rgb_ids

    @pytest.mark.asyncio
    async def test_group_has_no_error_status(self):
        dev = _make_device(groups=[1], mqtt_id="dev_1")
        ctrl = _make_controller(dali_devices=[dev])

        await ctrl._refresh_group_virtual_devices()

        assert "error_status" not in _control_ids_of(ctrl._group_devices_by_number[1])

    def test_group_excludes_non_group_eligible_readonly_controls(self):
        eligible = ActualLevelControl(DimmingCurveState())
        non_eligible_readonly = ErrorStatusControl()
        dev = DaliDevice.__new__(DaliDevice)
        dev.is_initialized = True
        dev._controls = {  # pylint: disable=protected-access
            eligible.control_info.id: eligible,
            non_eligible_readonly.control_info.id: non_eligible_readonly,
        }
        controls = dev.get_group_state_controls()
        ids = {c.control_info.id for c in controls}
        assert "actual_level" in ids
        assert "error_status" not in ids
        assert non_eligible_readonly.is_readable()
        assert not non_eligible_readonly.is_group_state_control


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


class TestGroupStateCandidates:
    # pylint: disable=protected-access
    @pytest.mark.asyncio
    async def test_group_skips_uninitialized_members_in_candidates(self):
        init_dev = _make_device(groups=[1], mqtt_id="init", short_address=1, uid="uid-1", is_initialized=True)
        uninit_dev = _make_device(
            groups=[1], mqtt_id="uninit", short_address=2, uid="uid-2", is_initialized=False
        )
        ctrl = _make_controller(dali_devices=[init_dev, uninit_dev])

        await ctrl._refresh_group_virtual_devices()

        source = ctrl._group_devices_by_number[1].state_source
        assert source is not None
        assert source.candidates_for("actual_level") == ("uid-1",)

    @pytest.mark.asyncio
    async def test_group_tc_candidates_only_include_tc_members(self):
        plain = _make_device(groups=[1], mqtt_id="plain", short_address=1, uid="uid-1")
        tc = _make_device(
            groups=[1],
            mqtt_id="tc",
            short_address=2,
            uid="uid-2",
            colour_type=ColourType.COLOUR_TEMPERATURE,
            tc_limits=Type8TcLimits(
                tc_min_mirek=153,
                tc_max_mirek=370,
                tc_phys_min_mirek=MIN_TC_MIREK,
                tc_phys_max_mirek=MAX_TC_MIREK,
            ),
        )
        ctrl = _make_controller(dali_devices=[plain, tc])

        await ctrl._refresh_group_virtual_devices()

        source = ctrl._group_devices_by_number[1].state_source
        assert set(source.candidates_for("actual_level")) == {"uid-1", "uid-2"}
        assert source.candidates_for("current_colour_temperature") == ("uid-2",)

    @pytest.mark.asyncio
    async def test_group_rgbwaf_candidates_only_include_rgbwaf_members(self):
        plain = _make_device(groups=[1], mqtt_id="plain", short_address=1, uid="uid-1")
        rgb = _make_device(
            groups=[1], mqtt_id="rgb", short_address=2, uid="uid-2", colour_type=ColourType.RGBWAF
        )
        ctrl = _make_controller(dali_devices=[plain, rgb])

        await ctrl._refresh_group_virtual_devices()

        source = ctrl._group_devices_by_number[1].state_source
        assert set(source.candidates_for("actual_level")) == {"uid-1", "uid-2"}
        assert source.candidates_for("current_rgb") == ("uid-2",)
        assert source.candidates_for("current_white") == ("uid-2",)


# ---------------------------------------------------------------------------
# Source pinning, value forwarding, error semantics
# ---------------------------------------------------------------------------


class TestGroupStateSourceSemantics:
    # pylint: disable=protected-access
    def _build_group_with_two_actual_level_candidates(self):
        d1 = _make_device(groups=[1], mqtt_id="d1", short_address=1, uid="uid-1")
        d2 = _make_device(groups=[1], mqtt_id="d2", short_address=2, uid="uid-2")
        ctrl = _make_controller(dali_devices=[d1, d2])
        return ctrl, d1, d2

    async def _refresh(self, ctrl):
        await ctrl._refresh_group_virtual_devices()

    @pytest.mark.asyncio
    async def test_group_pins_first_successful_candidate_as_source(self):
        ctrl, d1, _ = self._build_group_with_two_actual_level_candidates()
        await self._refresh(ctrl)
        source = ctrl._group_devices_by_number[1].state_source

        action = source.record_poll(d1.uid, "actual_level", success=True, value="42")

        assert source.pinned_source("actual_level") == d1.uid
        assert action == GroupStateUpdate(
            kind=GroupStateUpdateKind.VALUE, control_id="actual_level", payload="42"
        )

    @pytest.mark.asyncio
    async def test_group_publishes_source_polls_only(self):
        ctrl, d1, d2 = self._build_group_with_two_actual_level_candidates()
        await self._refresh(ctrl)
        source = ctrl._group_devices_by_number[1].state_source

        source.record_poll(d1.uid, "actual_level", success=True, value="10")
        action_success = source.record_poll(d2.uid, "actual_level", success=True, value="99")
        action_error = source.record_poll(d2.uid, "actual_level", success=False, value=None)

        assert action_success is None
        assert action_error is None
        assert source.pinned_source("actual_level") == d1.uid
        assert not source.is_err_set("actual_level")

    @pytest.mark.asyncio
    async def test_group_repins_on_next_successful_poll_after_source_error(self):
        ctrl, d1, d2 = self._build_group_with_two_actual_level_candidates()
        await self._refresh(ctrl)
        source = ctrl._group_devices_by_number[1].state_source

        source.record_poll(d1.uid, "actual_level", success=True, value="10")
        unpin_action = source.record_poll(d1.uid, "actual_level", success=False, value=None)
        assert source.pinned_source("actual_level") is None
        # No err yet: d2's last status is None, so not all candidates errored.
        assert unpin_action is None
        assert not source.is_err_set("actual_level")

        action = source.record_poll(d2.uid, "actual_level", success=True, value="55")
        assert source.pinned_source("actual_level") == d2.uid
        assert action == GroupStateUpdate(
            kind=GroupStateUpdateKind.VALUE, control_id="actual_level", payload="55"
        )

    @pytest.mark.asyncio
    async def test_group_does_not_emit_err_when_some_candidate_not_polled_yet(self):
        ctrl, d1, _ = self._build_group_with_two_actual_level_candidates()
        await self._refresh(ctrl)
        source = ctrl._group_devices_by_number[1].state_source

        action = source.record_poll(d1.uid, "actual_level", success=False, value=None)

        assert action is None
        assert not source.is_err_set("actual_level")

    @pytest.mark.asyncio
    async def test_group_emits_err_only_when_every_candidate_last_polled_with_error(self):
        ctrl, d1, d2 = self._build_group_with_two_actual_level_candidates()
        await self._refresh(ctrl)
        source = ctrl._group_devices_by_number[1].state_source

        source.record_poll(d1.uid, "actual_level", success=False, value=None)
        assert not source.is_err_set("actual_level")

        action = source.record_poll(d2.uid, "actual_level", success=False, value=None)

        assert action == GroupStateUpdate(
            kind=GroupStateUpdateKind.ERROR, control_id="actual_level", payload="r"
        )
        assert source.is_err_set("actual_level")

    @pytest.mark.asyncio
    async def test_group_clears_err_on_next_successful_candidate_poll(self):
        ctrl, d1, d2 = self._build_group_with_two_actual_level_candidates()
        await self._refresh(ctrl)
        source = ctrl._group_devices_by_number[1].state_source

        source.record_poll(d1.uid, "actual_level", success=False, value=None)
        source.record_poll(d2.uid, "actual_level", success=False, value=None)
        assert source.is_err_set("actual_level")

        action = source.record_poll(d1.uid, "actual_level", success=True, value="73")

        assert action == GroupStateUpdate(
            kind=GroupStateUpdateKind.VALUE, control_id="actual_level", payload="73"
        )
        assert not source.is_err_set("actual_level")
        assert source.pinned_source("actual_level") == d1.uid


# ---------------------------------------------------------------------------
# Rebuild triggers
# ---------------------------------------------------------------------------


class TestGroupRebuildOnStateSetChange:
    # pylint: disable=protected-access
    @pytest.mark.asyncio
    async def test_group_rebuilt_when_state_control_set_changes(self):
        plain = _make_device(groups=[1], mqtt_id="plain")
        ctrl = _make_controller(dali_devices=[plain])
        await ctrl._refresh_group_virtual_devices()
        cast(MagicMock, ctrl._device_publisher).reset_mock()

        rgb = _make_device(groups=[1], mqtt_id="rgb", colour_type=ColourType.RGBWAF)
        ctrl.dali_devices.append(rgb)

        await ctrl._refresh_group_virtual_devices()

        cast(AsyncMock, ctrl._device_publisher.remove_device).assert_awaited_once()
        cast(AsyncMock, ctrl._device_publisher.add_device).assert_awaited_once()
        ids = _control_ids_of(ctrl._group_devices_by_number[1])
        assert {"current_rgb", "current_white"} <= ids

    @pytest.mark.asyncio
    async def test_group_candidate_set_updates_when_member_leaves_group(self):
        """Candidate-only change keeps the same MQTT topics — update in place."""
        d1 = _make_device(groups=[1], mqtt_id="d1", short_address=1, uid="uid-1")
        d2 = _make_device(groups=[1], mqtt_id="d2", short_address=2, uid="uid-2")
        ctrl = _make_controller(dali_devices=[d1, d2])
        await ctrl._refresh_group_virtual_devices()
        device_before = ctrl._group_devices_by_number[1]
        source_before = device_before.state_source
        assert set(source_before.candidates_for("actual_level")) == {"uid-1", "uid-2"}
        cast(MagicMock, ctrl._device_publisher).reset_mock()

        d1.groups = set()

        await ctrl._refresh_group_virtual_devices()

        device_after = ctrl._group_devices_by_number[1]
        # Same device instance — no rebuild, no publisher activity
        assert device_after is device_before
        cast(AsyncMock, ctrl._device_publisher.remove_device).assert_not_awaited()
        cast(AsyncMock, ctrl._device_publisher.add_device).assert_not_awaited()
        source_after = device_after.state_source
        assert source_after is source_before
        assert source_after.candidates_for("actual_level") == ("uid-2",)

    @pytest.mark.asyncio
    async def test_broadcast_has_no_state_controls_after_change(self):
        ctrl = _make_controller(dali_devices=[])
        await ctrl._refresh_broadcast_device()
        ids_before = _control_ids_of(ctrl._broadcast_device)
        assert "actual_level" not in ids_before

        rgb = _make_device(groups=[1], mqtt_id="rgb", colour_type=ColourType.RGBWAF)
        ctrl.dali_devices.append(rgb)
        await ctrl._refresh_broadcast_device()

        ids_after = _control_ids_of(ctrl._broadcast_device)
        assert "actual_level" not in ids_after
        assert "current_rgb" not in ids_after
        assert "current_white" not in ids_after
        assert "current_colour_temperature" not in ids_after


# ---------------------------------------------------------------------------
# In-place candidate updates (no rebuild when topic layout is unchanged)
# ---------------------------------------------------------------------------


class TestGroupCandidateInPlaceUpdate:
    # pylint: disable=protected-access
    @pytest.mark.asyncio
    async def test_new_initialized_candidate_does_not_republish_group(self):
        d1 = _make_device(groups=[1], mqtt_id="d1", short_address=1, uid="uid-1")
        ctrl = _make_controller(dali_devices=[d1])
        await ctrl._refresh_group_virtual_devices()
        device_before = ctrl._group_devices_by_number[1]
        cast(MagicMock, ctrl._device_publisher).reset_mock()

        # Add another initialized member with the same state-control set.
        d2 = _make_device(groups=[1], mqtt_id="d2", short_address=2, uid="uid-2")
        ctrl.dali_devices.append(d2)

        await ctrl._refresh_group_virtual_devices()

        device_after = ctrl._group_devices_by_number[1]
        assert device_after is device_before
        cast(AsyncMock, ctrl._device_publisher.remove_device).assert_not_awaited()
        cast(AsyncMock, ctrl._device_publisher.add_device).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_candidate_added_to_last_status_with_none(self):
        d1 = _make_device(groups=[1], mqtt_id="d1", short_address=1, uid="uid-1")
        ctrl = _make_controller(dali_devices=[d1])
        await ctrl._refresh_group_virtual_devices()
        source = ctrl._group_devices_by_number[1].state_source
        # Drive d1 to a SUCCESS status to make the surviving entry distinguishable.
        source.record_poll("uid-1", "actual_level", success=True, value="42")
        assert source._state["actual_level"].candidate_statuses["uid-1"] == CandidatePollStatus.SUCCESS

        d2 = _make_device(groups=[1], mqtt_id="d2", short_address=2, uid="uid-2")
        ctrl.dali_devices.append(d2)

        await ctrl._refresh_group_virtual_devices()

        statuses = source._state["actual_level"].candidate_statuses
        assert statuses == {"uid-1": CandidatePollStatus.SUCCESS, "uid-2": None}

    @pytest.mark.asyncio
    async def test_pinned_source_lost_unpins_and_drops_status(self):
        d1 = _make_device(groups=[1], mqtt_id="d1", short_address=1, uid="uid-1")
        d2 = _make_device(groups=[1], mqtt_id="d2", short_address=2, uid="uid-2")
        ctrl = _make_controller(dali_devices=[d1, d2])
        await ctrl._refresh_group_virtual_devices()
        source = ctrl._group_devices_by_number[1].state_source
        # Pin d1 as the source.
        source.record_poll("uid-1", "actual_level", success=True, value="10")
        assert source.pinned_source("actual_level") == "uid-1"

        d1.groups = set()

        await ctrl._refresh_group_virtual_devices()

        assert source.pinned_source("actual_level") is None
        assert "uid-1" not in source._state["actual_level"].candidate_statuses
        assert source.candidates_for("actual_level") == ("uid-2",)

    @pytest.mark.asyncio
    async def test_pinned_source_kept_when_remaining(self):
        d1 = _make_device(groups=[1], mqtt_id="d1", short_address=1, uid="uid-1")
        d2 = _make_device(groups=[1], mqtt_id="d2", short_address=2, uid="uid-2")
        ctrl = _make_controller(dali_devices=[d1, d2])
        await ctrl._refresh_group_virtual_devices()
        source = ctrl._group_devices_by_number[1].state_source
        source.record_poll("uid-1", "actual_level", success=True, value="10")
        assert source.pinned_source("actual_level") == "uid-1"

        # d2 leaves the group; d1 (the pin) remains.
        d2.groups = set()

        await ctrl._refresh_group_virtual_devices()

        assert source.pinned_source("actual_level") == "uid-1"
        assert source.candidates_for("actual_level") == ("uid-1",)
        assert "uid-2" not in source._state["actual_level"].candidate_statuses

    @pytest.mark.asyncio
    async def test_capabilities_change_still_triggers_full_republish(self):
        """Setup-control extension (curve type) changes capabilities — full rebuild."""
        dev = _make_device(groups=[1], mqtt_id="dev", dimming_curve_type=DimmingCurveType.LOGARITHMIC)
        ctrl = _make_controller(dali_devices=[dev])
        await ctrl._refresh_group_virtual_devices()
        cast(MagicMock, ctrl._device_publisher).reset_mock()

        dev.dimming_curve_type = DimmingCurveType.LINEAR

        await ctrl._refresh_group_virtual_devices()

        cast(AsyncMock, ctrl._device_publisher.remove_device).assert_awaited_once()
        cast(AsyncMock, ctrl._device_publisher.add_device).assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rename_keeps_pinned_source(self):
        """Changing only mqtt_id (SetDevice rename) must not drop the pinned source.

        Regression: identity for state-source candidates is ``device.uid``,
        not mqtt_id, so a rename leaves state_config unchanged and the group
        is not touched at all.
        """
        d1 = _make_device(groups=[1], mqtt_id="d1", short_address=1, uid="uid-1")
        d2 = _make_device(groups=[1], mqtt_id="d2", short_address=2, uid="uid-2")
        ctrl = _make_controller(dali_devices=[d1, d2])
        await ctrl._refresh_group_virtual_devices()
        device_before = ctrl._group_devices_by_number[1]
        source = device_before.state_source
        # Pin d1 as the source via a successful poll.
        source.record_poll("uid-1", "actual_level", success=True, value="42")
        assert source.pinned_source("actual_level") == "uid-1"
        cast(MagicMock, ctrl._device_publisher).reset_mock()

        # Rename d1: mqtt_id changes but uid (and groups, capabilities) stay.
        d1.mqtt_id = "d1_renamed"

        await ctrl._refresh_group_virtual_devices()

        device_after = ctrl._group_devices_by_number[1]
        # Group device is the same instance; no publish/remove happened.
        assert device_after is device_before
        cast(AsyncMock, ctrl._device_publisher.remove_device).assert_not_awaited()
        cast(AsyncMock, ctrl._device_publisher.add_device).assert_not_awaited()
        # Pinned source — same uid — survives the rename.
        assert source.pinned_source("actual_level") == "uid-1"
        assert set(source.candidates_for("actual_level")) == {"uid-1", "uid-2"}

    @pytest.mark.asyncio
    async def test_short_address_change_keeps_pinned_source(self):
        """Changing short_address (SetDevice) must not drop the pinned source.

        Regression: identity for state-source candidates is ``device.uid``,
        which is stable across short-address changes, so state_config is
        unchanged and the group is not touched.
        """
        d1 = _make_device(groups=[1], mqtt_id="d1", short_address=1, uid="uid-1")
        d2 = _make_device(groups=[1], mqtt_id="d2", short_address=2, uid="uid-2")
        ctrl = _make_controller(dali_devices=[d1, d2])
        await ctrl._refresh_group_virtual_devices()
        device_before = ctrl._group_devices_by_number[1]
        source = device_before.state_source
        # Pin d1 as the source via a successful poll.
        source.record_poll("uid-1", "actual_level", success=True, value="42")
        assert source.pinned_source("actual_level") == "uid-1"
        cast(MagicMock, ctrl._device_publisher).reset_mock()

        # SetDevice changes d1's short address; uid stays the same.
        d1.address.short = 5

        await ctrl._refresh_group_virtual_devices()

        device_after = ctrl._group_devices_by_number[1]
        assert device_after is device_before
        cast(AsyncMock, ctrl._device_publisher.remove_device).assert_not_awaited()
        cast(AsyncMock, ctrl._device_publisher.add_device).assert_not_awaited()
        assert source.pinned_source("actual_level") == "uid-1"
        assert set(source.candidates_for("actual_level")) == {"uid-1", "uid-2"}


# ---------------------------------------------------------------------------
# collect_group_state_controls helper
# ---------------------------------------------------------------------------


class TestCollectGroupStateControls:
    def test_uninitialized_devices_yield_no_state_controls(self):
        dev = _make_device(is_initialized=False, mqtt_id="dev_1", short_address=1, uid="uid-1")
        templates, candidates = collect_group_state_controls([dev])
        assert not templates
        assert not candidates

    def test_initialized_plain_device_yields_actual_level(self):
        dev = _make_device(mqtt_id="dev_1", short_address=1, uid="uid-1")
        templates, candidates = collect_group_state_controls([dev])
        assert "actual_level" in templates
        assert candidates == {"actual_level": ["uid-1"]}

    def test_two_initialized_devices_yield_combined_candidates(self):
        d1 = _make_device(mqtt_id="d1", short_address=1, uid="uid-1")
        d2 = _make_device(mqtt_id="d2", short_address=2, uid="uid-2", colour_type=ColourType.RGBWAF)
        templates, candidates = collect_group_state_controls([d1, d2])
        assert set(templates) == {"actual_level", "current_rgb", "current_white"}
        assert candidates["actual_level"] == ["uid-1", "uid-2"]
        assert candidates["current_rgb"] == ["uid-2"]
        assert candidates["current_white"] == ["uid-2"]


# ---------------------------------------------------------------------------
# Group composition built from a real DaliDevice (no synthetic helper)
# ---------------------------------------------------------------------------


def _make_real_dali_device(mqtt_id, controls, short_address=1, uid="real-uid"):
    # __new__ bypasses the constructor (no driver, no init); the mqtt_id
    # property setter compares against default_mqtt_id, so set _mqtt_id directly.
    # pylint: disable=protected-access
    dev = DaliDevice.__new__(DaliDevice)
    dev.is_initialized = True
    dev._mqtt_id = mqtt_id
    dev._controls = {c.control_info.id: c for c in controls}
    dev.address = MagicMock()
    dev.address.short = short_address
    dev.uid = uid
    return dev


# Uses a real DaliDevice (not the _make_device mock) so a missing
# is_group_state_control flag in production code surfaces here.
class TestGroupCompositionFromRealDevice:  # pylint: disable=too-few-public-methods
    def test_real_device_only_yields_marked_controls(self):
        eligible = ActualLevelControl(DimmingCurveState())
        non_eligible = ErrorStatusControl()
        rgbwaf_state_controls = [
            c for c in rgbwaf_mqtt_controls(only_setup_controls=False) if c.is_group_state_control
        ]
        assert rgbwaf_state_controls, "Expected at least one RGBWAF group-eligible control"

        dev = _make_real_dali_device(
            mqtt_id="real",
            controls=[eligible, non_eligible, *rgbwaf_state_controls],
            short_address=7,
            uid="real-uid",
        )

        templates, candidates = collect_group_state_controls([dev])

        expected_ids = {eligible.control_info.id} | {c.control_info.id for c in rgbwaf_state_controls}
        assert set(templates) == expected_ids
        assert "error_status" not in templates
        for control_id in expected_ids:
            assert candidates[control_id] == ["real-uid"]


# ---------------------------------------------------------------------------
# _build_group_state_tasks
# ---------------------------------------------------------------------------


class TestBuildGroupStateTasks:
    # pylint: disable=protected-access

    async def _setup_group_with_two_members(self, mqtt_ids=("d1", "d2")):
        devices = [_make_device(groups=[1], mqtt_id=mid) for mid in mqtt_ids]
        ctrl = _make_controller(dali_devices=list(devices))
        await ctrl._refresh_group_virtual_devices()
        group_mqtt_id = ctrl._group_devices_by_number[1].mqtt_id
        return ctrl, devices, group_mqtt_id

    @pytest.mark.asyncio
    async def test_no_groups_yields_no_tasks(self):
        dev = _make_device(groups=[], mqtt_id="lonely")
        ctrl = _make_controller(dali_devices=[dev])

        tasks = ctrl._build_group_state_tasks(
            dev,
            [ControlPollResult(control_id="actual_level", value="42")],
        )

        assert not tasks

    @pytest.mark.asyncio
    async def test_successful_poll_yields_set_control_value_on_group(self):
        ctrl, (d1, _d2), group_mqtt_id = await self._setup_group_with_two_members()

        tasks = ctrl._build_group_state_tasks(
            d1,
            [ControlPollResult(control_id="actual_level", value="42")],
        )

        assert len(tasks) == 1
        cast(AsyncMock, ctrl._device_publisher.set_control_value).assert_called_once_with(
            group_mqtt_id, "actual_level", "42"
        )
        # Returned coroutines are not awaited; close them to avoid warnings.
        for task in tasks:
            task.close()

    @pytest.mark.asyncio
    async def test_error_when_all_candidates_errored_yields_set_control_error(self):
        ctrl, (d1, d2), group_mqtt_id = await self._setup_group_with_two_members()

        tasks_first = ctrl._build_group_state_tasks(
            d1, [ControlPollResult(control_id="actual_level", value=None, error="r")]
        )
        assert not tasks_first

        tasks_second = ctrl._build_group_state_tasks(
            d2, [ControlPollResult(control_id="actual_level", value=None, error="r")]
        )

        assert len(tasks_second) == 1
        cast(AsyncMock, ctrl._device_publisher.set_control_error).assert_called_once_with(
            group_mqtt_id, "actual_level", "r"
        )
        cast(AsyncMock, ctrl._device_publisher.set_control_value).assert_not_called()
        for task in tasks_second:
            task.close()

    @pytest.mark.asyncio
    async def test_response_for_unknown_control_id_is_skipped(self):
        ctrl, (d1, _d2), _ = await self._setup_group_with_two_members()

        tasks = ctrl._build_group_state_tasks(
            d1,
            [ControlPollResult(control_id="error_status", value="0")],
        )

        assert not tasks
        cast(AsyncMock, ctrl._device_publisher.set_control_value).assert_not_called()
        cast(AsyncMock, ctrl._device_publisher.set_control_error).assert_not_called()

    @pytest.mark.asyncio
    async def test_non_source_successful_poll_yields_no_tasks(self):
        ctrl, (d1, d2), _ = await self._setup_group_with_two_members()

        first_tasks = ctrl._build_group_state_tasks(
            d1, [ControlPollResult(control_id="actual_level", value="10")]
        )
        for task in first_tasks:
            task.close()
        cast(MagicMock, ctrl._device_publisher).reset_mock()

        tasks = ctrl._build_group_state_tasks(d2, [ControlPollResult(control_id="actual_level", value="99")])

        assert not tasks
        cast(AsyncMock, ctrl._device_publisher.set_control_value).assert_not_called()


# ---------------------------------------------------------------------------
# GroupStateUpdate (sanity)
# ---------------------------------------------------------------------------


class TestGroupStateUpdate:  # pylint: disable=too-few-public-methods
    def test_group_state_update_is_immutable(self):
        action = GroupStateUpdate(kind=GroupStateUpdateKind.VALUE, control_id="actual_level", payload="42")
        with pytest.raises(Exception):
            action.payload = "99"  # type: ignore[misc]
