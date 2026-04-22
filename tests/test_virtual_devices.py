"""Tests for capability-aware virtual device logic.

Covers:
- aggregate_capabilities
- build_virtual_device_controls
- DaliDevice.dt8_tc_limits
- _refresh_broadcast_device
- _refresh_group_virtual_devices (rebuild on capability change)
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.address import GearBroadcast, GearGroup
from dali.gear.colour import tc_kelvin_mirek
from dali.gear.general import DAPC

from wb.mqtt_dali.application_controller import (
    AggregatedCapabilities,
    AggregatedVirtualDevice,
    ApplicationController,
    aggregate_capabilities,
    build_virtual_device_controls,
)
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveType
from wb.mqtt_dali.dali_type8_parameters import ColourType
from wb.mqtt_dali.dali_type8_tc import MAX_TC_MIREK, MIN_TC_MIREK, Type8TcLimits

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_device(
    *,
    is_initialized=True,
    groups=None,
    colour_type=None,
    tc_limits=None,
    dimming_curve_type=DimmingCurveType.LOGARITHMIC,
):
    """Return a minimal mock DaliDevice-like object."""
    d = MagicMock()
    d.is_initialized = is_initialized
    d.groups = set(groups or [])
    d.dt8_colour_type = colour_type
    d.dt8_tc_limits = tc_limits
    d.dimming_curve_type = dimming_curve_type
    return d


def _make_publisher():
    pub = MagicMock()
    pub.add_device = AsyncMock()
    pub.remove_device = AsyncMock()
    pub.register_control_handler = AsyncMock()
    pub.set_control_error = AsyncMock()
    pub.has_device = MagicMock(return_value=False)
    return pub


def _make_controller(dali_devices=None):
    """Create a bare ApplicationController instance with minimal state."""
    ctrl = ApplicationController.__new__(ApplicationController)
    ctrl.logger = logging.getLogger("test")
    ctrl.uid = "gw_bus_1"
    ctrl.bus_name = "Bus 1"
    ctrl.dali_devices = list(dali_devices or [])
    ctrl._device_publisher = _make_publisher()  # pylint: disable=protected-access
    ctrl._devices_by_mqtt_id = {}  # pylint: disable=protected-access
    ctrl._group_devices_by_number = {}  # pylint: disable=protected-access
    ctrl._broadcast_device = AggregatedVirtualDevice(  # pylint: disable=protected-access
        mqtt_id="bus_1_broadcast",
        name="Bus 1 Broadcast",
        capabilities=AggregatedCapabilities(),
        address=GearBroadcast(),
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


class TestRefreshAggregatedVirtualDevices:
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
        old_device = AggregatedVirtualDevice(
            mqtt_id="bus_1_group_02",
            name="Bus 1 Group 2",
            capabilities=AggregatedCapabilities(),
            address=GearGroup(2),
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
