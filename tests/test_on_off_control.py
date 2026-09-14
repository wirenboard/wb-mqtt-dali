"""Behaviour of the configurable ``on_off`` control (devices and groups).

Covers control publication, the on/off action command sequences with fade-time
set/skip/restore, group addressing and the group fade time left in force, state
derivation from the level ``actual_level`` sees, the no-own-queries contract,
same-value write suppression, publishing on the observed level, the group state
mirroring inheritance, the group-edit state recovery, invalid MQTT payloads
and the on_off carry-over through ResetDeviceSettings.
"""

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
    Up,
)

from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase, Pollable
from wb.mqtt_dali.control_ids import ACTUAL_LEVEL, ON_OFF
from wb.mqtt_dali.dali_common_parameters import FadeTimeFadeRateParam
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveState, DimmingCurveType
from wb.mqtt_dali.device_registry import DeviceRegistry
from wb.mqtt_dali.on_off_control import (
    OffAction,
    OffActionMode,
    OnAction,
    OnActionMode,
    OnOffConfig,
    OnOffControl,
    OnOffSettingsParam,
    on_off_config_to_editor_json,
    on_off_config_to_json,
)
from wb.mqtt_dali.virtual_devices import GroupVirtualDevice
from wb.mqtt_dali.wbmqtt import ControlError

from ._on_off_helpers import BusScript, ScriptedBus, frames, group_settings

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
    return OnOffControl(OnOffSettingsParam(config), curve or _linear_curve(), fade_param)


def _names(commands) -> list:
    return [type(command).__name__ for command in commands]


def _make_dali_device(on_off=None) -> DaliDevice:
    device = DaliDevice(DaliDeviceAddress(5, 0x123456), "gw_bus_1", MagicMock(), on_off=on_off)
    device.rebuild_mqtt_controls()
    return device


class _MemberDevice(DaliDevice):
    """Gear member of a group: real gear, so dispatch and the group mirror work as they ship."""

    def __init__(self, short=5, groups=(3,), on_off=None, fade_code=None):
        super().__init__(DaliDeviceAddress(short=short, random=0), "gw_bus_1", MagicMock(), on_off=on_off)
        self.mqtt_id = f"dev-{short}"
        self.name = f"member {short}"
        self._test_groups = set(groups)
        self.is_initialized = True
        self.rebuild_mqtt_controls()
        if fade_code is not None:
            self.fade_param.set_fade_time(fade_code)

    @property
    def groups(self) -> set:
        return self._test_groups


def _registry_of(members) -> DeviceRegistry:
    registry = DeviceRegistry()
    registry.set_gear_devices(list(members))
    return registry


def _group_device(member: _MemberDevice, on_off=None) -> GroupVirtualDevice:
    return GroupVirtualDevice(3, _registry_of([member]), "gw_bus_1", "Bus 1", on_off_config=on_off)


def _device_entry(on_off=None, short=5) -> dict:
    entry = {"short": short, "random": 0x123456}
    if on_off is not None:
        entry["on_off"] = on_off_config_to_json(on_off)
    return entry


def _group_entry(on_off: OnOffConfig, number=3) -> dict:
    return {"number": number, "on_off": on_off_config_to_json(on_off)}


class OnOffControlPublicationTest(unittest.IsolatedAsyncioTestCase):

    def test_on_off_control_absent_without_config(self):
        device = _make_dali_device(on_off=None)
        self.assertIsNone(device.get_mqtt_control(ON_OFF))

    def test_on_off_control_published_when_configured(self):
        """A device and a group built with an on_off block both expose a writable,
        non-readable ``switch`` control; a group without the block stays without it."""
        device = _make_dali_device(on_off=SCENE_CONFIG)
        control = device.get_mqtt_control(ON_OFF)
        self.assertIsInstance(control, OnOffControl)
        self.assertEqual(control.control_info.state.meta.control_type, "switch")
        self.assertFalse(control.control_info.state.meta.read_only)
        self.assertTrue(control.is_writable())

        member = _MemberDevice()
        group = _group_device(member, on_off=SCENE_CONFIG)
        self.assertIsInstance(group.get_mqtt_control(ON_OFF), OnOffControl)
        self.assertIsNone(_group_device(member).get_mqtt_control(ON_OFF))


class OnOffActionCommandsTest(unittest.IsolatedAsyncioTestCase):

    def test_on_action_scene_sends_go_to_scene_only(self):
        """``scene`` mode turns on with a single GoToScene and no fade-time write, even
        though the device has a known fade code — scene recall keeps the device's fade."""
        control = _control(SCENE_CONFIG, fade_code=4)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(frames(commands), frames([GoToScene(ADDR, 7)]))

    def test_on_action_last_active_level_sets_fade_time_then_recalls(self):
        """``last_active_level`` mode writes the configured fade time (DTR0+SetFadeTime)
        before GoToLastActiveLevel."""
        control = _control(_last_active_config(fade_time=2), fade_code=5)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(
            frames(commands[:3]),
            frames([DTR0(2), SetFadeTime(ADDR), GoToLastActiveLevel(ADDR)]),
        )

    def test_on_action_level_applies_dimming_curve_and_fade_time(self):
        """``level`` mode converts the configured percent through the dimming curve state
        the control was built with and prefixes the DAPC with the fade-time write."""
        curve = _linear_curve()
        control = _control(_level_config(percent=60, fade_time=3), fade_code=None, curve=curve)
        commands = control.get_setup_commands(ADDR, "1")
        expected_raw = curve.get_raw_value(60)
        self.assertEqual(
            frames(commands),
            frames([DTR0(3), SetFadeTime(ADDR), DAPC(ADDR, expected_raw)]),
        )

    def test_on_action_dapc_uses_raw_value_and_fade_time(self):
        """``dapc`` mode writes the configured fade time then DAPCs the raw value with no
        dimming-curve conversion; the prior code is unknown, so nothing is restored after."""
        control = _control(_dapc_config(fade_time=1))
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(
            frames(commands),
            frames([DTR0(1), SetFadeTime(ADDR), DAPC(ADDR, 200)]),
        )

    def test_off_action_dapc_fades_to_zero(self):
        """With the control primed to "on", an ``off_action`` in ``dapc`` mode sets the
        off fade time, DAPCs to level 0, then restores the device's prior fade code."""
        control = _control(_dapc_config(off_fade_time=6), fade_code=1)
        control.update_from_percent("50.0")  # the control is "on", so "0" executes
        commands = control.get_setup_commands(ADDR, "0")
        self.assertEqual(
            frames(commands),
            frames([DTR0(6), SetFadeTime(ADDR), DAPC(ADDR, 0), DTR0(1), SetFadeTime(ADDR)]),
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
        self.assertEqual(frames(commands), frames([Off(ADDR)]))


class OnOffFadeTimeTest(unittest.IsolatedAsyncioTestCase):

    def test_fade_time_restored_after_action(self):
        """An action with a configured fade_time ends with the prior fade time written
        back, so subsequent wanted_level/dapc writes do not inherit the action's fade."""
        control = _control(_dapc_config(fade_time=2), fade_code=4)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(
            frames(commands),
            frames([DTR0(2), SetFadeTime(ADDR), DAPC(ADDR, 200), DTR0(4), SetFadeTime(ADDR)]),
        )

    def test_fade_time_omitted_skips_set_and_restore(self):
        """A mode without a configured fade_time sends only the action command — no
        SetFadeTime set nor restore — so it runs with the device's current fade time."""
        control = _control(_dapc_config(fade_time=None), fade_code=4)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(frames(commands), frames([DAPC(ADDR, 200)]))

    def test_fade_time_write_skipped_when_matches_known(self):
        """When the tracked fade-time code already equals the action's target, neither the
        set nor the restore DTR0+SetFadeTime pair is sent (NVM wear)."""
        control = _control(_dapc_config(fade_time=4), fade_code=4)
        commands = control.get_setup_commands(ADDR, "1")
        self.assertEqual(frames(commands), frames([DAPC(ADDR, 200)]))


class OnOffGroupTest(unittest.IsolatedAsyncioTestCase):

    async def test_group_on_off_addresses_group(self):
        """Writing the group's on_off sends every command to the group DALI address —
        no member short address appears, whether or not the members have been read."""
        for members in ([_MemberDevice()], []):
            with self.subTest(members=len(members)):
                group = GroupVirtualDevice(
                    3, _registry_of(members), "gw_bus_1", "Bus 1", on_off_config=_dapc_config(fade_time=2)
                )
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

    async def test_configured_group_published_without_members(self):
        """A bus loaded with a groups entry publishes that group at start, before any ballast
        has answered: the group device carries the on_off switch and no mirrored level."""
        async with ScriptedBus() as harness:
            bus = await harness.start_bus({"devices": [], "groups": [_group_entry(SCENE_CONFIG)]})

            group = bus.group_devices[3]
            self.assertIsInstance(group.get_mqtt_control(ON_OFF), OnOffControl)
            self.assertIsNone(group.get_mqtt_control(ACTUAL_LEVEL))
            self.assertIn(ON_OFF, harness.published_controls(group.mqtt_id))

    async def test_configured_group_survives_a_start_with_no_gear_known(self):
        """With no gear known at all, a scan that finds none refutes nothing: the configured
        group keeps its settings and its published device."""
        async with ScriptedBus(BusScript(present_shorts=frozenset())) as harness:
            bus = await harness.start_bus({"devices": [], "groups": [_group_entry(SCENE_CONFIG)]})
            group_mqtt_id = bus.group_devices[3].mqtt_id

            await bus.start_commissioning()
            await harness.wait_until(lambda: not bus.commissioning_state.is_running(), timeout=30)

            self.assertEqual(group_settings(bus), {3: SCENE_CONFIG})
            self.assertIn(3, bus.group_devices)
            self.assertIn(group_mqtt_id, harness.published_devices())

    async def test_group_gains_member_controls_after_init(self):
        """The group device published without members is rebuilt with the member's
        controls once it initialises, and keeps the on_off setting."""
        async with ScriptedBus(BusScript(groups={5: {3}})) as harness:
            bus = await harness.start_bus(
                {"devices": [_device_entry()], "groups": [_group_entry(SCENE_CONFIG)]}
            )
            without_members = bus.group_devices[3]
            self.assertIsNone(without_members.get_mqtt_control(ACTUAL_LEVEL))

            await harness.wait_until(lambda: bus.dali_devices[0].is_initialized)
            await harness.settle()

            rebuilt = bus.group_devices[3]
            self.assertIsNot(rebuilt, without_members)
            self.assertIsNotNone(rebuilt.get_mqtt_control(ACTUAL_LEVEL))
            self.assertIsInstance(rebuilt.get_mqtt_control(ON_OFF), OnOffControl)
            self.assertEqual(group_settings(bus), {3: SCENE_CONFIG})

    def test_group_on_off_level_uses_shared_or_fallback_curve(self):
        """``level`` mode on a group converts the percent through the members' shared
        dimming curve; with mixed member curves the logarithmic default is used."""

        def _stub(curve_type):
            return SimpleNamespace(
                uid=f"uid-{curve_type}",
                is_initialized=True,
                dt8_colour_type=None,
                dt8_tc_limits=None,
                dimming_curve_type=curve_type,
                get_group_state_controls=list,
            )

        def _dapc_raw(devices):
            group = GroupVirtualDevice(
                3,
                SimpleNamespace(resolve=lambda _address: devices),
                "grp",
                "Bus 1",
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

    def test_group_fade_time_not_restored(self):
        """A group action sets its fade time and leaves it in force: no restore frames,
        even though the member has a fade-time code of its own."""
        member = _MemberDevice(fade_code=7)
        group = _group_device(member, on_off=_dapc_config(fade_time=2))
        commands = group.get_mqtt_control(ON_OFF).get_setup_commands(GROUP_ADDR, "1")
        self.assertEqual(
            frames(commands),
            frames([DTR0(2), SetFadeTime(GROUP_ADDR), DAPC(GROUP_ADDR, 200)]),
        )


class OnOffStateTest(unittest.IsolatedAsyncioTestCase):

    async def test_state_reflects_zero_level(self):
        """The level the device reports drives the switch: the initial read of 0 publishes "0",
        an observed DAPC to 150 publishes "1"."""
        async with ScriptedBus() as harness:
            bus = await harness.start_bus({"devices": [_device_entry(_dapc_config())]})
            device = bus.dali_devices[0]
            await harness.wait_until(lambda: device.is_initialized)
            await harness.settle()

            self.assertEqual(harness.values(device.mqtt_id)[ON_OFF], "0")

            await bus.send_command_batch([DAPC(ADDR, 150)])
            await harness.settle()

            self.assertEqual(harness.values(device.mqtt_id)[ON_OFF], "1")

    async def test_on_off_shows_the_read_error_when_the_level_is_unknown(self):
        """Gear that leaves QueryActualLevel unanswered puts both actual_level and on_off in
        read error: the switch never claims to be off on an unread level."""
        script = BusScript(unanswered=frozenset({"QueryActualLevel"}))
        async with ScriptedBus(script) as harness:
            bus = await harness.start_bus({"devices": [_device_entry(_dapc_config())]})
            device = bus.dali_devices[0]
            await harness.wait_until(lambda: device.is_initialized)
            await harness.settle()

            self.assertEqual(harness.control_error(device.mqtt_id, ACTUAL_LEVEL), "r")
            self.assertEqual(harness.control_error(device.mqtt_id, ON_OFF), "r")

    def test_on_off_adds_no_bus_queries(self):
        """The control is not pollable, so only the existing actual_level read feeds it and it
        never adds its own frames to the bus poll."""
        device = _make_dali_device(on_off=_dapc_config())
        self.assertNotIsInstance(device.get_mqtt_control(ON_OFF), Pollable)

    def test_same_value_write_is_ignored(self):
        """Once the state is known, writing the value the control already shows produces
        no bus commands, both for the "0" state and after the state moved to "1"."""
        control = _control(_dapc_config(fade_time=2), fade_code=4)
        control.update_from_percent("0.000")  # establish a known "0" state first
        self.assertEqual(control.get_setup_commands(ADDR, "0"), [])
        control.update_from_percent("39.370")
        self.assertEqual(control.get_setup_commands(ADDR, "1"), [])

    def test_unknown_state_write_is_not_suppressed(self):
        """Before any level readback the state is unknown, so a "0" write still emits the off
        action instead of being dropped as a same-value no-op against the placeholder "0"."""
        control = _control(SCENE_CONFIG, fade_code=4)
        self.assertEqual(frames(control.get_setup_commands(ADDR, "0")), frames([Off(ADDR)]))

    async def test_write_leaves_the_publish_to_the_observed_command(self):
        """A switch write puts its action on the bus and publishes no value of its own; the
        level the confirming read finds afterwards is what sets the state."""
        script = BusScript(answers={"QueryActualLevel": 0})
        async with ScriptedBus(script) as harness:
            bus = await harness.start_bus({"devices": [_device_entry(_last_active_config())]})
            device = bus.dali_devices[0]
            await harness.wait_until(lambda: device.is_initialized)
            await harness.settle()
            self.assertEqual(harness.values(device.mqtt_id)[ON_OFF], "0")
            harness.drain()

            await harness.write(device.mqtt_id, ON_OFF, "1")

            self.assertEqual(_names(harness.sent), ["GoToLastActiveLevel"])
            self.assertEqual(harness.value_publishes(device.mqtt_id, ON_OFF), [])

            script.answers["QueryActualLevel"] = 100  # the gear did come on
            await harness.wait_until(lambda: harness.value_publishes(device.mqtt_id, ON_OFF) != [])
            self.assertEqual(harness.value_publishes(device.mqtt_id, ON_OFF), ["1"])

    async def test_group_state_mirrors_member_candidates(self):
        """The group's on_off follows the group state mirroring: the member's read level drives
        it through the poll path and an observed DAPC through the event path."""
        script = BusScript(groups={5: {3}}, answers={"QueryActualLevel": 200})
        async with ScriptedBus(script) as harness:
            bus = await harness.start_bus(
                {"devices": [_device_entry()], "groups": [_group_entry(_dapc_config())]}
            )
            member = bus.dali_devices[0]
            await harness.wait_until(lambda: member.is_initialized)
            await harness.settle()
            group = bus.group_devices[3]
            self.assertIsNone(member.get_mqtt_control(ON_OFF))
            self.assertEqual(harness.values(group.mqtt_id)[ON_OFF], "1")

            # Up is not predictable, so the level comes from the member's confirming read.
            harness.drain()
            script.answers["QueryActualLevel"] = 0
            await bus.send_command_batch([Up(ADDR)])
            await harness.wait_until(lambda: harness.value_publishes(group.mqtt_id, ON_OFF) != [])
            self.assertEqual(harness.value_publishes(group.mqtt_id, ON_OFF), ["0"])

            harness.drain()
            await bus.send_command_batch([DAPC(ADDR, 150)])
            await harness.settle()

            self.assertEqual(harness.value_publishes(group.mqtt_id, ON_OFF), ["1"])

    async def test_on_off_invalid_mqtt_payload_sends_nothing(self):
        """Garbage written to the on_off control topic sends no bus commands and
        surfaces a write error on the control."""
        async with ScriptedBus() as harness:
            bus = await harness.start_bus({"devices": [_device_entry(_dapc_config())]})
            device = bus.dali_devices[0]
            await harness.wait_until(lambda: device.is_initialized)
            await harness.settle()
            harness.drain()

            await harness.write(device.mqtt_id, ON_OFF, "banana")

            self.assertEqual(harness.sent, [])
            self.assertEqual(harness.control_error(device.mqtt_id, ON_OFF), ControlError.WRITE.to_mqtt())
            self.assertEqual(harness.value_publishes(device.mqtt_id, ON_OFF), [])


class OnOffDeviceEditStateTest(unittest.IsolatedAsyncioTestCase):

    async def test_device_on_off_strategy_edit_stays_off_the_bus(self):
        """A strategy-only edit of a published switch: no frames, no republished device and the
        same control object, whose next write already carries the new action."""
        script = BusScript(answers={"QueryActualLevel": 0})
        async with ScriptedBus(script) as harness:
            bus = await harness.start_bus({"devices": [_device_entry(SCENE_CONFIG)]})
            device = bus.dali_devices[0]
            await harness.wait_until(lambda: device.is_initialized)
            await bus.load_device_info(device)
            await harness.settle()
            control = device.get_mqtt_control(ON_OFF)
            level_due_at = device.get_mqtt_control(ACTUAL_LEVEL).next_due_at
            harness.drain()

            # The editor echoes the whole config back, as SetDevice does.
            new_config = {**device.params, "on_off": on_off_config_to_editor_json(_dapc_config())}
            await bus.apply_parameters(device, new_config)
            await harness.settle()

            self.assertEqual(harness.sent, [])
            self.assertEqual(harness.device_publishes(device.mqtt_id), [])
            self.assertIs(device.get_mqtt_control(ON_OFF), control)
            # A rebuilt control set would come back due, and the whole device be re-read.
            self.assertEqual(device.get_mqtt_control(ACTUAL_LEVEL).next_due_at, level_due_at)
            harness.drain()

            await harness.write(device.mqtt_id, ON_OFF, "1")
            self.assertEqual(frames(harness.sent), frames([DAPC(ADDR, 200)]))


class OnOffGroupEditStateTest(unittest.IsolatedAsyncioTestCase):

    async def test_group_on_off_edit_keeps_known_state(self):
        """A strategy-only edit keeps the group device: the mirrored level and the switch value
        derived from it are never lost, and the group is not republished."""
        script = BusScript(groups={5: {3}}, answers={"QueryActualLevel": 100})
        async with ScriptedBus(script) as harness:
            bus = await harness.start_bus(
                {"devices": [_device_entry()], "groups": [_group_entry(_dapc_config())]}
            )
            member = bus.dali_devices[0]
            await harness.wait_until(lambda: member.is_initialized)
            await harness.settle()
            group = bus.group_devices[3]
            known_level = member.get_mqtt_control(ACTUAL_LEVEL).control_info.state.value
            self.assertTrue(known_level)
            self.assertEqual(group.get_mqtt_control(ON_OFF).control_info.state.value, "1")

            new_config = _dapc_config(fade_time=5)
            harness.drain()
            await bus.apply_group_parameters(3, {"on_off": on_off_config_to_editor_json(new_config)})
            await harness.settle()

            self.assertIs(bus.group_devices[3], group)
            self.assertEqual(harness.device_publishes(group.mqtt_id), [])
            self.assertEqual(group_settings(bus), {3: new_config})
            self.assertEqual(group.get_mqtt_control(ACTUAL_LEVEL).control_info.state.value, known_level)
            self.assertEqual(group.get_mqtt_control(ON_OFF).control_info.state.value, "1")

    async def test_group_on_off_enable_rebuilds_group_with_the_switch(self):
        """Enabling on_off on a group that had none rebuilds its device and republishes it,
        now carrying the switch."""
        async with ScriptedBus(BusScript(groups={5: {3}})) as harness:
            bus = await harness.start_bus({"devices": [_device_entry()]})
            await harness.wait_until(lambda: bus.dali_devices[0].is_initialized)
            await harness.settle()
            group = bus.group_devices[3]
            self.assertIsNone(group.get_mqtt_control(ON_OFF))
            harness.drain()

            await bus.apply_group_parameters(3, {"on_off": on_off_config_to_editor_json(_dapc_config())})
            await harness.settle()

            rebuilt = bus.group_devices[3]
            self.assertIsNot(rebuilt, group)
            self.assertIsInstance(rebuilt.get_mqtt_control(ON_OFF), OnOffControl)
            # The old device goes off MQTT (payload None) before the new one is published.
            self.assertEqual(
                [payload is None for payload in harness.device_publishes(group.mqtt_id)],
                [True, False],
            )
            self.assertIn(ON_OFF, harness.published_controls(rebuilt.mqtt_id))

    async def test_group_on_off_disable_deletes_a_memberless_group(self):
        """Removing the settings of a group no gear reports takes the group away: the setting
        was all that kept it, and its virtual device goes off MQTT."""
        async with ScriptedBus(BusScript(present_shorts=frozenset())) as harness:
            bus = await harness.start_bus(
                {"devices": [_device_entry()], "groups": [_group_entry(_dapc_config())]}
            )
            await harness.settle()
            self.assertFalse(bus.dali_devices[0].is_initialized)
            group_mqtt_id = bus.group_devices[3].mqtt_id

            await bus.apply_group_parameters(3, {"on_off": {"enabled": False}})
            await harness.settle()

            self.assertNotIn(3, bus.group_devices)
            self.assertEqual(group_settings(bus), {})
            self.assertNotIn(group_mqtt_id, harness.published_devices())

    async def test_group_on_off_disable_rebuilds_group_without_the_switch(self):
        """Disabling on_off rebuilds the group device without the switch; the group itself stays
        because a member still reports it."""
        async with ScriptedBus(BusScript(groups={5: {3}})) as harness:
            bus = await harness.start_bus(
                {"devices": [_device_entry()], "groups": [_group_entry(_dapc_config())]}
            )
            await harness.wait_until(lambda: bus.dali_devices[0].is_initialized)
            await harness.settle()
            group = bus.group_devices[3]

            await bus.apply_group_parameters(3, {"on_off": {"enabled": False}})
            await harness.settle()

            rebuilt = bus.group_devices[3]
            self.assertIsNot(rebuilt, group)
            self.assertIsNone(rebuilt.get_mqtt_control(ON_OFF))
            self.assertEqual(group_settings(bus), {})


class OnOffResetDeviceSettingsTest(unittest.IsolatedAsyncioTestCase):

    async def test_device_on_off_survives_reset_device_settings(self):
        """ResetDeviceSettings recreates the device object; the service-side on_off
        config is carried over and the recreated device publishes the control again."""
        async with ScriptedBus() as harness:
            bus = await harness.start_bus({"devices": [_device_entry(SCENE_CONFIG)]})
            device = bus.dali_devices[0]
            await harness.wait_until(lambda: device.is_initialized)
            await harness.settle()

            await bus.reset_device_settings(device)
            await harness.settle()

            new_device = bus.dali_devices[0]
            self.assertIsNot(new_device, device)
            self.assertEqual(new_device.uid, device.uid)
            self.assertEqual(new_device.on_off_config, SCENE_CONFIG)
            self.assertIsInstance(new_device.get_mqtt_control(ON_OFF), OnOffControl)
            self.assertIn(ON_OFF, harness.published_controls(new_device.mqtt_id))


if __name__ == "__main__":
    unittest.main()
