"""Behaviour of the configurable ``on_off`` control (devices and groups).

Covers control publication, the on/off action command sequences with fade-time
set/skip/restore, group addressing and representative fade restore, state
derivation from the level ``actual_level`` sees, the no-own-queries contract,
same-value write suppression, optimistic publish on write, the group state
mirroring inheritance, the group-edit state recovery, invalid MQTT payloads
and the on_off carry-over through ResetDeviceSettings.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from dali.address import GearGroup, GearShort
from dali.gear.general import (
    DAPC,
    DTR0,
    GoToLastActiveLevel,
    GoToScene,
    Off,
    SetFadeTime,
)

from wb.mqtt_dali.common_dali_device import (
    ControlPollResult,
    DaliDeviceAddress,
    DaliDeviceBase,
)
from wb.mqtt_dali.control_ids import ACTUAL_LEVEL, ON_OFF
from wb.mqtt_dali.dali_common_parameters import FadeTimeFadeRateParam
from wb.mqtt_dali.dali_controls import ActualLevelControl
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveState, DimmingCurveType
from wb.mqtt_dali.device_registry import DeviceRegistry
from wb.mqtt_dali.event_sync_coordinator import EventSyncCoordinator
from wb.mqtt_dali.on_off_control import (
    DeviceOnOffControl,
    OffAction,
    OffActionMode,
    OnAction,
    OnActionMode,
    OnOffConfig,
    OnOffControl,
)
from wb.mqtt_dali.virtual_devices import (
    AggregatedCapabilities,
    BroadcastVirtualDevice,
    GroupSpec,
    GroupVirtualDevice,
    aggregate_capabilities,
)

from ._app_controller_helpers import make_loop_controller, stop_loop
from ._on_off_helpers import ScriptedDriver

DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access

ADDR = GearShort(5)
GROUP_ADDR = GearGroup(3)

SCENE_CONFIG = OnOffConfig(
    on_action=OnAction(OnActionMode.SCENE, scene=7),
    off_action=OffAction(OffActionMode.OFF),
)


def _dapc_config(fade_time=None, off_fade_time=None):
    return OnOffConfig(
        on_action=OnAction(OnActionMode.DAPC, value=200, fade_time=fade_time),
        off_action=OffAction(OffActionMode.DAPC, fade_time=off_fade_time),
    )


def _level_config(percent=60, fade_time=None):
    return OnOffConfig(
        on_action=OnAction(OnActionMode.LEVEL, percent=percent, fade_time=fade_time),
        off_action=OffAction(OffActionMode.OFF),
    )


def _last_active_config(fade_time=None):
    return OnOffConfig(
        on_action=OnAction(OnActionMode.LAST_ACTIVE_LEVEL, fade_time=fade_time),
        off_action=OffAction(OffActionMode.OFF),
    )


def _linear_curve() -> DimmingCurveState:
    curve = DimmingCurveState()
    curve.curve_type = DimmingCurveType.LINEAR
    return curve


def _control(config: OnOffConfig, fade_code=None, curve=None) -> OnOffControl:
    fade_param = FadeTimeFadeRateParam()
    if fade_code is not None:
        fade_param.set_fade_time(fade_code)
    return DeviceOnOffControl(config, curve or _linear_curve(), fade_param)


def _frames(commands) -> list:
    return [(type(command).__name__, command.frame.as_integer) for command in commands]


def _make_dali_device(on_off=None) -> DaliDevice:
    device = DaliDevice(DaliDeviceAddress(5, 0x123456), "gw_bus_1", MagicMock(), on_off=on_off)
    device.rebuild_mqtt_controls()
    return device


class _MemberStub:  # pylint: disable=too-many-instance-attributes
    """Public-surface fake of one initialized DALI gear device."""

    def __init__(self, short=5, groups=(3,), on_off=None, fade_code=None):
        curve = _linear_curve()
        self.uid, self.mqtt_id, self.name = f"uid-{short}", f"dev-{short}", f"member {short}"
        self.address = SimpleNamespace(short=short)
        self.groups = set(groups)
        self.is_initialized = True
        self.dt8_colour_type = None
        self.dt8_tc_limits = None
        self.dt8_handler = None
        self.dimming_curve_type = DimmingCurveType.LINEAR
        self.fade_param = FadeTimeFadeRateParam()
        if fade_code is not None:
            self.fade_param.set_fade_time(fade_code)
        self.controls = {ACTUAL_LEVEL: ActualLevelControl(curve)}
        if on_off is not None:
            self.controls[ON_OFF] = DeviceOnOffControl(on_off, curve, self.fade_param)

    def get_mqtt_control(self, control_id):
        return self.controls.get(control_id)

    def get_group_state_controls(self):
        return [c for c in self.controls.values() if c.is_group_state_control]


def _group_device(member: _MemberStub, on_off=None) -> GroupVirtualDevice:
    registry = DeviceRegistry()
    registry.set_gear_devices([member])
    return GroupVirtualDevice.for_group(
        3,
        GroupSpec.from_devices([member]),
        "gw_bus_1",
        "Bus 1",
        device_registry=registry,
        on_off_config=on_off,
    )


class OnOffControlPublicationTest(unittest.IsolatedAsyncioTestCase):

    def test_on_off_control_absent_without_config(self):
        device = _make_dali_device(on_off=None)
        self.assertIsNone(device.get_mqtt_control(ON_OFF))

    def test_on_off_control_published_when_configured(self):
        """A device built with an on_off config block and a group virtual device built with
        the group's on_off entry both expose a writable, non-readable ``switch`` control;
        a group without the entry stays without it."""
        device = _make_dali_device(on_off=SCENE_CONFIG)
        control = device.get_mqtt_control(ON_OFF)
        self.assertIsInstance(control, OnOffControl)
        self.assertEqual(control.control_info.meta.control_type, "switch")
        self.assertFalse(control.control_info.meta.read_only)
        self.assertTrue(control.is_writable())

        member = _MemberStub()
        group = _group_device(member, on_off=SCENE_CONFIG)
        self.assertIsInstance(group.get_mqtt_control(ON_OFF), OnOffControl)
        self.assertIsNone(_group_device(member).get_mqtt_control(ON_OFF))


class OnOffActionCommandsTest(unittest.IsolatedAsyncioTestCase):

    def test_on_action_scene_sends_go_to_scene_only(self):
        """``scene`` mode turns on with a single GoToScene and no fade-time write, even
        though the device has a known fade code — scene recall keeps the device's fade."""
        control = _control(SCENE_CONFIG, fade_code=4)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(_frames(commands), _frames([GoToScene(ADDR, 7)]))

    def test_on_action_last_active_level_sets_fade_time_then_recalls(self):
        """``last_active_level`` mode writes the configured fade time (DTR0+SetFadeTime)
        before GoToLastActiveLevel."""
        control = _control(_last_active_config(fade_time=2), fade_code=5)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(
            _frames(commands[:3]),
            _frames([DTR0(2), SetFadeTime(ADDR), GoToLastActiveLevel(ADDR)]),
        )

    def test_on_action_level_applies_dimming_curve_and_fade_time(self):
        """``level`` mode converts the configured percent through the dimming curve state
        the control was built with and prefixes the DAPC with the fade-time write."""
        curve = _linear_curve()
        control = _control(_level_config(percent=60, fade_time=3), fade_code=None, curve=curve)
        commands = control.get_setup_commands(ADDR, "1")
        expected_raw = curve.get_raw_value(60)
        self.assertEqual(
            _frames(commands),
            _frames([DTR0(3), SetFadeTime(ADDR), DAPC(ADDR, expected_raw)]),
        )

    def test_on_action_dapc_uses_raw_value_and_fade_time(self):
        """``dapc`` mode writes the configured fade time then DAPCs the raw value straight
        through, with no dimming-curve conversion."""
        control = _control(_dapc_config(fade_time=1))
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(
            _frames(commands),
            _frames([DTR0(1), SetFadeTime(ADDR), DAPC(ADDR, 200)]),
        )

    def test_off_action_dapc_fades_to_zero(self):
        """With the control primed to "on", an ``off_action`` in ``dapc`` mode sets the
        off fade time, DAPCs to level 0, then restores the device's prior fade code."""
        control = _control(_dapc_config(off_fade_time=6), fade_code=1)
        control.update_from_percent("50.0")  # the control is "on", so "0" executes
        commands = control.get_setup_commands(ADDR, "0")
        self.assertEqual(
            _frames(commands),
            _frames([DTR0(6), SetFadeTime(ADDR), DAPC(ADDR, 0), DTR0(1), SetFadeTime(ADDR)]),
        )

    def test_off_action_off_sends_off_command_only(self):
        """With the control primed to "on", an ``off_action`` in ``off`` mode sends a bare
        Off command and no fade-time write — Off is instantaneous."""
        config = OnOffConfig(
            on_action=OnAction(OnActionMode.DAPC, value=200),
            off_action=OffAction(OffActionMode.OFF),
        )
        control = _control(config, fade_code=4)
        control.update_from_percent("50.0")
        commands = control.get_setup_commands(ADDR, "0")
        self.assertEqual(_frames(commands), _frames([Off(ADDR)]))


class OnOffFadeTimeTest(unittest.IsolatedAsyncioTestCase):

    def test_fade_time_restored_after_action(self):
        """An action with a configured fade_time ends with the prior fade time written
        back, so subsequent wanted_level/dapc writes do not inherit the action's fade."""
        control = _control(_dapc_config(fade_time=2), fade_code=4)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(
            _frames(commands),
            _frames([DTR0(2), SetFadeTime(ADDR), DAPC(ADDR, 200), DTR0(4), SetFadeTime(ADDR)]),
        )

    def test_fade_time_omitted_skips_set_and_restore(self):
        """A mode without a configured fade_time sends only the action command — no
        SetFadeTime set nor restore — so it runs with the device's current fade time."""
        control = _control(_dapc_config(fade_time=None), fade_code=4)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(_frames(commands), _frames([DAPC(ADDR, 200)]))

    def test_fade_time_write_skipped_when_matches_known(self):
        """When the tracked fade-time code already equals the action's target, neither the
        set nor the restore DTR0+SetFadeTime pair is sent (NVM wear)."""
        control = _control(_dapc_config(fade_time=4), fade_code=4)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(_frames(commands), _frames([DAPC(ADDR, 200)]))

    def test_fade_time_not_restored_when_prior_unknown(self):
        """When the prior fade code is unknown, the action's fade time is set but not
        restored afterwards — the action's fade stays in effect."""
        control = _control(_dapc_config(fade_time=2), fade_code=None)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(
            _frames(commands),
            _frames([DTR0(2), SetFadeTime(ADDR), DAPC(ADDR, 200)]),
        )


class OnOffGroupTest(unittest.IsolatedAsyncioTestCase):

    async def test_group_on_off_addresses_group(self):
        """Writing the group's on_off sends every command to the group DALI address —
        no member short address appears."""
        member = _MemberStub()
        group = _group_device(member, on_off=_dapc_config(fade_time=2))
        driver = AsyncMock()
        driver.send_commands = AsyncMock(return_value=[])

        await group.execute_control(driver, ON_OFF, "1")

        commands = driver.send_commands.call_args.args[0]
        self.assertTrue(commands)
        for command in commands:
            if isinstance(command, DTR0):  # DTR0 carries no destination
                continue
            self.assertIsInstance(command.destination, GearGroup)
            self.assertEqual(command.destination.group, 3)

    def test_group_on_off_level_uses_shared_or_fallback_curve(self):
        """``level`` mode on a group converts the percent through the members' shared
        dimming curve; with mixed member curves the logarithmic default is used."""

        def _stub(curve_type):
            return SimpleNamespace(
                is_initialized=True,
                dt8_colour_type=None,
                dt8_tc_limits=None,
                dimming_curve_type=curve_type,
            )

        def _dapc_raw(devices):
            capabilities = aggregate_capabilities(devices)
            group = GroupVirtualDevice(
                mqtt_id="grp",
                name="grp",
                capabilities=capabilities,
                group_number=3,
                state_control_templates={},
                state_candidates={},
                device_registry=DeviceRegistry(),
                on_off_config=_level_config(percent=60),
            )
            commands = group.get_mqtt_control(ON_OFF).get_setup_commands(GROUP_ADDR, "1")
            self.assertEqual(len(commands), 1)
            return commands[0].power

        linear = DimmingCurveState()
        linear.curve_type = DimmingCurveType.LINEAR
        logarithmic = DimmingCurveState()

        shared_raw = _dapc_raw([_stub(DimmingCurveType.LINEAR), _stub(DimmingCurveType.LINEAR)])
        self.assertEqual(shared_raw, linear.get_raw_value(60))

        mixed_raw = _dapc_raw([_stub(DimmingCurveType.LINEAR), _stub(DimmingCurveType.LOGARITHMIC)])
        self.assertEqual(mixed_raw, logarithmic.get_raw_value(60))

    def test_group_fade_time_restored_from_representative(self):
        """The group restore uses the fade-time code of the pinned member the group state
        mirrors; with no pinned representative (or unknown code) there is no restore."""
        member = _MemberStub(fade_code=7)
        group = _group_device(member, on_off=_dapc_config(fade_time=2))
        group.state_source.record_poll(member.uid, ACTUAL_LEVEL, success=True, value="50.000")
        commands = group.get_mqtt_control(ON_OFF).get_setup_commands(GROUP_ADDR, "1")
        self.assertEqual(
            _frames(commands),
            _frames(
                [
                    DTR0(2),
                    SetFadeTime(GROUP_ADDR),
                    DAPC(GROUP_ADDR, 200),
                    DTR0(7),
                    SetFadeTime(GROUP_ADDR),
                ]
            ),
        )

        unpinned = _group_device(member, on_off=_dapc_config(fade_time=2))
        commands = unpinned.get_mqtt_control(ON_OFF).get_setup_commands(GROUP_ADDR, "1")
        self.assertEqual(
            _frames(commands),
            _frames([DTR0(2), SetFadeTime(GROUP_ADDR), DAPC(GROUP_ADDR, 200)]),
        )


class OnOffStateTest(unittest.IsolatedAsyncioTestCase):

    async def test_state_reflects_zero_level(self):
        """After the predicted/polled raw level becomes 0 the control publishes "0";
        a non-zero level publishes "1". Driven through the event-sync coordinator so
        the same path that updates actual_level updates on_off."""
        member = _MemberStub(groups=(), on_off=_dapc_config())
        registry = DeviceRegistry()
        registry.set_gear_devices([member])
        publisher = AsyncMock()
        coordinator = EventSyncCoordinator(
            publisher=publisher,
            device_registry=registry,
            group_devices_by_number={},
            logger=MagicMock(),
        )

        await coordinator.apply_commands([DAPC(ADDR, 0)])
        published = {(c.args[0], c.args[1]): c.args[2] for c in publisher.set_control_value.await_args_list}
        self.assertEqual(published[(member.mqtt_id, ON_OFF)], "0")

        await coordinator.apply_commands([DAPC(ADDR, 150)])
        published = {(c.args[0], c.args[1]): c.args[2] for c in publisher.set_control_value.await_args_list}
        self.assertEqual(published[(member.mqtt_id, ON_OFF)], "1")

    def test_on_off_adds_no_bus_queries(self):
        """The control is not readable and builds no query command: only the existing
        actual_level read feeds it, so it never adds its own frames to the bus poll."""
        device = _make_dali_device(on_off=_dapc_config())
        control = device.get_mqtt_control(ON_OFF)
        self.assertFalse(control.is_readable())
        self.assertIsNone(control.get_query(ADDR))

    def test_same_value_write_is_ignored(self):
        """Once the state is known, writing the value the control already shows produces
        no bus commands, both for the "0" state and after the state moved to "1"."""
        control = _control(_dapc_config(fade_time=2), fade_code=4)
        control.update_from_percent("0.000")  # establish a known "0" state first
        self.assertEqual(control.get_setup_commands(ADDR, "0"), [])
        control.update_from_percent("39.370")
        self.assertEqual(control.get_setup_commands(ADDR, "1"), [])

    def test_unknown_state_write_is_not_suppressed(self):
        """Before any level readback the state is unknown, so a "0" write onto a device
        that is physically on still emits the off action instead of being dropped as a
        same-value no-op against the placeholder "0"."""
        control = _control(SCENE_CONFIG, fade_code=4)
        self.assertEqual(_frames(control.get_setup_commands(ADDR, "0")), _frames([Off(ADDR)]))

    async def test_write_publishes_state_optimistically(self):
        # pylint: disable=protected-access
        """A write publishes the new on_off state immediately after the action runs,
        without waiting for a level prediction/poll; a later poll readback with a
        diverging level corrects the value through the actual_level mirror."""
        controller = make_loop_controller()
        curve = _linear_curve()
        on_off = DeviceOnOffControl(_dapc_config(), curve, FadeTimeFadeRateParam())
        controls = {ON_OFF: on_off, ACTUAL_LEVEL: ActualLevelControl(curve)}
        device = MagicMock(spec=DaliDevice)
        device.mqtt_id = "dev-5"
        device.name = "dev5"
        device.groups = []
        device.execute_control = AsyncMock(return_value=None)
        device.get_mqtt_control.side_effect = controls.get
        controller._devices_by_mqtt_id = {"dev-5": device}

        message = MagicMock()
        message.topic.value = f"/devices/dev-5/controls/{ON_OFF}/on"
        message.payload = b"1"
        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            await asyncio.wait_for(controller._handle_on_topic(message), timeout=1.0)
        finally:
            await stop_loop(controller, loop_task)

        controller._device_publisher.set_control_value.assert_any_await("dev-5", ON_OFF, "1")

        await controller._publish_poll_results(
            device, [ControlPollResult(control_id=ACTUAL_LEVEL, value="0.000")]
        )
        on_off_calls = [
            c.args[2]
            for c in controller._device_publisher.set_control_value.await_args_list
            if c.args[:2] == ("dev-5", ON_OFF)
        ]
        self.assertEqual(on_off_calls[-1], "0")

    async def test_group_state_mirrors_member_candidates(self):
        # pylint: disable=protected-access
        """The group's on_off follows the existing group state mirroring: the pinned
        member's polled level drives it through the poll path, and a sniffed level
        command to the member updates it through the event path — even though the
        member itself has no on_off control."""
        controller = make_loop_controller()
        member = _MemberStub()
        group = _group_device(member, on_off=_dapc_config())
        controller._group_devices_by_number[3] = group
        controller._device_registry.set_gear_devices([member])

        await controller._publish_poll_results(
            member, [ControlPollResult(control_id=ACTUAL_LEVEL, value="0.000")]
        )
        published = {
            (c.args[0], c.args[1]): c.args[2]
            for c in controller._device_publisher.set_control_value.await_args_list
        }
        self.assertEqual(published[(group.mqtt_id, ON_OFF)], "0")

        await controller._event_sync.apply_commands([DAPC(ADDR, 150)])
        published = {
            (c.args[0], c.args[1]): c.args[2]
            for c in controller._device_publisher.set_control_value.await_args_list
        }
        self.assertEqual(published[(group.mqtt_id, ON_OFF)], "1")

    async def test_on_off_invalid_mqtt_payload_sends_nothing(self):
        # pylint: disable=protected-access
        """Garbage written to the on_off control topic sends no bus commands and
        surfaces a write error on the control."""
        controller = make_loop_controller()
        device = _make_dali_device(on_off=_dapc_config())
        device.is_initialized = True
        controller._devices_by_mqtt_id = {device.mqtt_id: device}

        message = MagicMock()
        message.topic.value = f"/devices/{device.mqtt_id}/controls/{ON_OFF}/on"
        message.payload = b"banana"
        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            await asyncio.wait_for(controller._handle_on_topic(message), timeout=1.0)
        finally:
            await stop_loop(controller, loop_task)

        controller._dev.send_commands.assert_not_awaited()
        controller._device_publisher.set_control_error.assert_any_await(device.mqtt_id, ON_OFF, "w")
        on_off_publishes = [
            c
            for c in controller._device_publisher.set_control_value.await_args_list
            if c.args[:2] == (device.mqtt_id, ON_OFF)
        ]
        self.assertEqual(on_off_publishes, [])


class OnOffGroupEditStateTest(unittest.IsolatedAsyncioTestCase):

    async def test_group_on_off_edit_keeps_known_state(self):
        # pylint: disable=protected-access
        """Re-strategising a group's on_off mutates the live device in place: no rebuild,
        no re-poll.

        A pinned representative feeds the group's actual_level and derives on_off. A
        strategy-only edit keeps the same device object, the pinned representative and
        the known switch value, and touches no MQTT topic (the switch type/value are
        unchanged, so nothing is added, removed or republished). The next poll still
        flows through the same device, and a same-config write is a no-op returning
        ``False``.
        """
        controller = make_loop_controller()
        controller.bus_name = "Bus 1"
        member = _MemberStub()
        controller.dali_devices = [member]
        controller._device_registry.set_gear_devices([member])
        old_config = _dapc_config()
        group = _group_device(member, on_off=old_config)
        controller._group_devices_by_number[3] = group
        controller._devices_by_mqtt_id = {group.mqtt_id: group}
        controller._group_on_off = {3: old_config}

        await controller._publish_poll_results(
            member, [ControlPollResult(control_id=ACTUAL_LEVEL, value="39.370")]
        )
        self.assertEqual(group.state_source.pinned_source(ACTUAL_LEVEL), member.uid)
        self.assertEqual(group.get_mqtt_control(ON_OFF).control_info.value, "1")

        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            self.assertTrue(
                await asyncio.wait_for(controller.set_group_on_off(3, _dapc_config(fade_time=5)), timeout=1.0)
            )

            # Same live device; the strategy-only edit rebuilds nothing and republishes no
            # control, so the pin and the known switch value are untouched.
            self.assertIs(controller._group_devices_by_number[3], group)
            self.assertIsInstance(group.get_mqtt_control(ON_OFF), OnOffControl)
            self.assertEqual(group.state_source.pinned_source(ACTUAL_LEVEL), member.uid)
            self.assertEqual(group.get_mqtt_control(ON_OFF).control_info.value, "1")
            controller._device_publisher.remove_device.assert_not_awaited()
            controller._device_publisher.add_control.assert_not_awaited()
            controller._device_publisher.remove_control.assert_not_awaited()

            await controller._publish_poll_results(
                member, [ControlPollResult(control_id=ACTUAL_LEVEL, value="39.370")]
            )
            published = {
                (c.args[0], c.args[1]): c.args[2]
                for c in controller._device_publisher.set_control_value.await_args_list
            }
            self.assertEqual(published[(group.mqtt_id, ACTUAL_LEVEL)], "39.370")
            self.assertEqual(published[(group.mqtt_id, ON_OFF)], "1")

            self.assertFalse(
                await asyncio.wait_for(controller.set_group_on_off(3, _dapc_config(fade_time=5)), timeout=1.0)
            )
        finally:
            await stop_loop(controller, loop_task)

    async def test_group_on_off_enable_adds_control_in_place(self):
        # pylint: disable=protected-access
        """Enabling on_off on a group that had none adds the single control through the
        publisher's ``add_control`` — the group device is not rebuilt (``remove_device``
        is never called) and stays the same object."""
        controller = make_loop_controller()
        controller.bus_name = "Bus 1"
        member = _MemberStub()
        controller.dali_devices = [member]
        controller._device_registry.set_gear_devices([member])
        group = _group_device(member, on_off=None)
        controller._group_devices_by_number[3] = group
        controller._devices_by_mqtt_id = {group.mqtt_id: group}
        controller._group_on_off = {}
        self.assertIsNone(group.get_mqtt_control(ON_OFF))

        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            self.assertTrue(
                await asyncio.wait_for(controller.set_group_on_off(3, _dapc_config()), timeout=1.0)
            )

            self.assertIs(controller._group_devices_by_number[3], group)
            control = group.get_mqtt_control(ON_OFF)
            self.assertIsInstance(control, OnOffControl)
            controller._device_publisher.add_control.assert_awaited_once_with(
                group.mqtt_id, control.control_info
            )
            controller._device_publisher.remove_device.assert_not_awaited()
        finally:
            await stop_loop(controller, loop_task)

    async def test_group_on_off_disable_removes_control_in_place(self):
        # pylint: disable=protected-access
        """Disabling on_off drops just the control through the publisher's
        ``remove_control`` — the group device is not rebuilt and stays the same object."""
        controller = make_loop_controller()
        controller.bus_name = "Bus 1"
        member = _MemberStub()
        controller.dali_devices = [member]
        controller._device_registry.set_gear_devices([member])
        group = _group_device(member, on_off=_dapc_config())
        controller._group_devices_by_number[3] = group
        controller._devices_by_mqtt_id = {group.mqtt_id: group}
        controller._group_on_off = {3: _dapc_config()}

        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            self.assertTrue(await asyncio.wait_for(controller.set_group_on_off(3, None), timeout=1.0))

            self.assertIs(controller._group_devices_by_number[3], group)
            self.assertIsNone(group.get_mqtt_control(ON_OFF))
            controller._device_publisher.remove_control.assert_awaited_once_with(group.mqtt_id, ON_OFF)
            controller._device_publisher.remove_device.assert_not_awaited()
        finally:
            await stop_loop(controller, loop_task)


class OnOffResetDeviceSettingsTest(unittest.IsolatedAsyncioTestCase):

    async def test_device_on_off_survives_reset_device_settings(self):
        # pylint: disable=protected-access
        """ResetDeviceSettings recreates the device object; the service-side on_off
        config is carried over and the recreated device publishes the control again."""
        controller = make_loop_controller()
        driver = ScriptedDriver()
        controller._dev = driver
        controller._gtin_db = MagicMock()
        controller._init_scheduler.get_retry_count = MagicMock(return_value=0)
        controller._device_publisher.has_device = MagicMock(return_value=False)
        controller._broadcast_device = BroadcastVirtualDevice(AggregatedCapabilities(), "gw_bus_1", "Bus 1")
        device = DaliDevice(DaliDeviceAddress(5, 0x123456), "gw_bus_1", MagicMock(), on_off=SCENE_CONFIG)
        controller.dali_devices = [device]
        controller._devices_by_mqtt_id = {device.mqtt_id: device}
        controller._device_registry.set_gear_devices([device])

        loop_task = asyncio.create_task(controller._polling_loop())
        try:
            await asyncio.wait_for(controller.reset_device_settings(device), timeout=1.0)
        finally:
            await stop_loop(controller, loop_task)

        new_device = controller.dali_devices[0]
        self.assertIsNot(new_device, device)
        self.assertEqual(new_device.uid, device.uid)
        self.assertEqual(new_device.on_off_config, SCENE_CONFIG)
        self.assertIsInstance(new_device.get_mqtt_control(ON_OFF), OnOffControl)


if __name__ == "__main__":
    unittest.main()
