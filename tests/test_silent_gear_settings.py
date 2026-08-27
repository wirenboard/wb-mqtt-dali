"""Tests for control gear that does not answer some of the queries we make.

Gear that does not implement a part of the standard stays silent instead of reporting an error,
so for the params that opt in silence means "no such feature": initialisation and the settings
read go on without them, and the setting is not shown at all. Silence on any other param, an
unreadable answer and a gateway failure still fail everything, as before.
"""

import logging
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

from dali.address import GearShort
from dali.command import Command, Response
from dali.frame import BackwardFrame, BackwardFrameError
from dali.gear.general import (
    DAPC,
    QueryDeviceType,
    QueryFadeTimeFadeRate,
    QueryGroupsEightToFifteen,
    QueryGroupsZeroToSeven,
    QueryMaxLevel,
    QueryMinLevel,
    QueryNextDeviceType,
    QueryPowerOnLevel,
    QuerySceneLevel,
    QuerySystemFailureLevel,
    SetMinLevel,
)
from dali.gear.led import QueryDimmingCurve, QueryFastFadeTime, SelectDimmingCurve

from wb.mqtt_dali import common_dali_device
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.control_ids import ACTUAL_LEVEL
from wb.mqtt_dali.control_ids import DAPC as DAPC_CONTROL
from wb.mqtt_dali.control_ids import WANTED_LEVEL
from wb.mqtt_dali.dali_device import DaliDevice, DaliDeviceType
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveType
from wb.mqtt_dali.gear.dimming_curve import QueryDimmingCurve as QueryType17DimmingCurve
from wb.mqtt_dali.settings import SettingsParamBase, SettingsParamName
from wb.mqtt_dali.wbdali_error_response import NoTransmission
from wb.mqtt_dali.wbdali_utils import NoAnswerError

_LOGGER_NAME = "test.silent_gear"

# Prevent file system access in DaliDeviceBase.__init__
DaliDeviceBase._common_schema = {"title": "test-schema", "properties": {}}  # pylint: disable=protected-access

# What a fully conforming DT6 device answers. Group 1 and the linear curve are picked so a
# silent gear (no groups, standard curve) cannot be mistaken for an answering one.
GEAR_ANSWERS = {
    QueryDeviceType: DaliDeviceType.LED_MODULES.value,
    QueryDimmingCurve: DimmingCurveType.LINEAR.value,
    QueryGroupsZeroToSeven: 0b0000_0010,
    QueryGroupsEightToFifteen: 0x00,
    QueryFadeTimeFadeRate: 0x27,
    QueryMaxLevel: 254,
    QueryMinLevel: 10,
    QueryPowerOnLevel: 254,
    QuerySystemFailureLevel: 254,
    QuerySceneLevel: 100,
    QueryFastFadeTime: 5,
}

GROUP_QUERIES = (QueryGroupsZeroToSeven, QueryGroupsEightToFifteen)

# 50% on the standard (logarithmic) curve; the linear one would put it at 127.
LOGARITHMIC_HALF_LEVEL = 229

# IEC 62386-102 answers to QUERY DEVICE TYPE / QUERY NEXT DEVICE TYPE.
MANY_TYPES = 255
NO_MORE_TYPES = 254


class _StubMemoryParams(SettingsParamBase):
    """Stands in for `GeneralMemoryParams`.

    Memory-bank silence is already reported as "bank not implemented" and tolerated, and it has
    its own tests; faking bank contents here would only add noise. The base class reads nothing
    and contributes no schema.
    """

    def __init__(self, *_args) -> None:
        super().__init__(SettingsParamName("General memory parameters"))


class _SilentMemoryParams(_StubMemoryParams):
    """Stands in for a param outside the closed list that the gear says nothing about.

    It does not mix in `OptionalSetting`, so its silence must still fail the settings read.
    """

    async def read(self, driver, short_address, logger=None) -> dict:
        del driver, short_address, logger
        raise NoAnswerError("no response")


class _FakeGear:
    """Driver-shaped fake gear: answers the queries in `answers`, stays silent about the rest.

    Silence is `cmd.response(None)` — what the gateway reports as "transmission without
    response". Command classes listed in `framing_error` answer unreadably instead, and those
    in `gateway_error` fail before reaching the bus, as the gateway reports it.
    """

    def __init__(  # pylint: disable=too-many-arguments, R0917
        self, answers=None, silent=(), framing_error=(), gateway_error=(), types=None
    ) -> None:
        self.answers = dict(GEAR_ANSWERS if answers is None else answers)
        self.silent = tuple(silent)
        self.framing_error = tuple(framing_error)
        self.gateway_error = tuple(gateway_error)
        # More than one device type: answered as MANY_TYPES plus a QUERY NEXT DEVICE TYPE each.
        self.types = None if types is None else list(types)
        self.sent: list[Command] = []
        self._types_left: list[int] = []

    async def send(self, cmd, source=None, priority=None) -> Response:
        del source, priority
        self.sent.append(cmd)
        if isinstance(cmd, self.gateway_error):
            return NoTransmission()
        if cmd.response is None:
            return Response(None)
        if isinstance(cmd, self.framing_error):
            return cmd.response(BackwardFrameError(0xFF))
        return cmd.response(self._answer_frame(cmd))

    def _answer_frame(self, cmd) -> Optional[BackwardFrame]:
        if self.types is not None and isinstance(cmd, (QueryDeviceType, QueryNextDeviceType)):
            if isinstance(cmd, QueryDeviceType):
                self._types_left = list(self.types)
                return BackwardFrame(MANY_TYPES)
            return BackwardFrame(self._types_left.pop(0) if self._types_left else NO_MORE_TYPES)
        value = None if isinstance(cmd, self.silent) else self.answers.get(type(cmd))
        return None if value is None else BackwardFrame(value)

    async def send_commands(self, cmds, source=None, priority=None) -> list[Response]:
        return [await self.send(cmd, source, priority) for cmd in cmds]

    async def run_sequence(self, seq, priority=None, progress=None):
        del priority, progress
        response = None
        try:
            while True:
                try:
                    cmd = seq.send(response)
                except StopIteration as stop:
                    return stop.value
                if isinstance(cmd, list):
                    response = await self.send_commands(cmd)
                else:
                    response = await self.send(cmd)
        finally:
            seq.close()

    def sent_of(self, command_class) -> list[Command]:
        return [cmd for cmd in self.sent if isinstance(cmd, command_class)]


class _GearTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.object(common_dali_device, "GeneralMemoryParams", _StubMemoryParams)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_device(self) -> DaliDevice:
        device = DaliDevice(DaliDeviceAddress(short=1, random=0x1235CF), bus_id="bus", gtin_db=MagicMock())
        device.set_logger(logging.getLogger(_LOGGER_NAME))
        return device

    async def initialized_device(self, gear: _FakeGear) -> DaliDevice:
        device = self.make_device()
        await device.initialize(gear)
        return device

    async def device_with_info(self, gear: _FakeGear) -> DaliDevice:
        device = await self.initialized_device(gear)
        await device.load_info(gear)
        return device

    @staticmethod
    def control_ids(device: DaliDevice) -> set:
        return {control.id for control in device.get_mqtt_controls()}


class TestInitializationWithSilentGear(_GearTestCase):
    async def test_silent_dimming_curve_does_not_fail_initialization(self):
        """DT6 gear answers the device type but not the dimming curve query.

        Initialisation used to fail on it and repeat forever, leaving the device out of MQTT.
        Now it completes and the controls are built.
        """
        gear = _FakeGear(silent=(QueryDimmingCurve,))
        device = await self.initialized_device(gear)

        self.assertTrue(device.is_initialized)
        self.assertTrue(device.get_mqtt_controls())

    async def test_silent_groups_do_not_fail_initialization(self):
        """The group queries stay silent: initialisation completes and the gear is in no group."""
        gear = _FakeGear(silent=GROUP_QUERIES)
        device = await self.initialized_device(gear)

        self.assertTrue(device.is_initialized)
        self.assertEqual(device.groups, set())

    async def test_device_with_silent_optional_params_appears_in_mqtt(self):
        """Gear silent about the curve, the groups and the fade settings still gets its controls.

        These are all the queries initialisation makes besides the device type, so this is the
        bench device: nothing but the type is answered during init.
        """
        gear = _FakeGear(silent=(QueryDimmingCurve, QueryFadeTimeFadeRate, *GROUP_QUERIES))
        device = await self.initialized_device(gear)

        self.assertLessEqual({ACTUAL_LEVEL, WANTED_LEVEL, DAPC_CONTROL}, self.control_ids(device))

    async def test_silent_dimming_curve_assumes_logarithmic(self):
        """An unread curve is the standard one, and percent are converted by it.

        The level control has to convert percent into a raw level on every command, so the
        assumed curve is what the gear is actually driven with.
        """
        gear = _FakeGear(silent=(QueryDimmingCurve,))
        device = await self.initialized_device(gear)

        self.assertEqual(device.dimming_curve_type, DimmingCurveType.LOGARITHMIC)
        commands = device.get_mqtt_control(WANTED_LEVEL).get_setup_commands(GearShort(1), "50")
        self.assertEqual([command.power for command in commands], [LOGARITHMIC_HALF_LEVEL])

    async def test_silent_groups_mean_no_group_membership(self):
        """Group membership drives the group virtual devices, and unread groups mean none.

        Gear that does answer the group queries reports the membership it has, so an empty set
        is the silence and not the default.
        """
        silent_gear = _FakeGear(silent=GROUP_QUERIES)
        answering_gear = _FakeGear()

        self.assertEqual((await self.initialized_device(silent_gear)).groups, set())
        self.assertEqual((await self.initialized_device(answering_gear)).groups, {1})

    async def test_silent_device_type_still_fails_initialization(self):
        """Without the device type there is no telling which features the gear has at all, so
        silence on that query keeps failing initialisation."""
        gear = _FakeGear(silent=(QueryDeviceType,))
        device = self.make_device()

        with self.assertRaises(RuntimeError):
            await device.initialize(gear)
        self.assertFalse(device.is_initialized)


class TestSettingsReadWithSilentGear(_GearTestCase):
    async def test_silent_param_does_not_fail_settings_read(self):
        """One silent parameter used to abort the whole read with `Error reading "..."`.

        Now the params are independent: the silent one has no value, the rest are read.
        """
        gear = _FakeGear(silent=(QueryMinLevel,))
        device = await self.device_with_info(gear)

        self.assertEqual(device.params["max_level"], 254)
        self.assertNotIn("min_level", device.params)

    async def test_unread_param_absent_from_schema(self):
        """A parameter without a value gets no field in the settings schema either: the field
        would show a made-up value and accept a write nothing can confirm."""
        gear = _FakeGear(silent=(QueryMinLevel, QuerySceneLevel, *GROUP_QUERIES))
        device = await self.device_with_info(gear)

        properties = device.schema["properties"]
        for unread in ("min_level", "scenes", "groups"):
            self.assertNotIn(unread, properties)
        self.assertIn("max_level", properties)

    async def test_silent_scenes_leave_other_settings_readable(self):
        """All 16 scene queries stay silent: the other settings are still read and shown."""
        gear = _FakeGear(silent=(QuerySceneLevel,))
        device = await self.device_with_info(gear)

        self.assertNotIn("scenes", device.params)
        self.assertEqual(device.params["max_level"], 254)
        self.assertEqual(device.params["min_level"], 10)
        self.assertIn("fade_time", device.params)

    async def test_unread_limits_allow_full_range(self):
        """Unread MAX and MIN level put no limit on the level control: 100% still goes out as
        the raw maximum instead of being clamped to a guessed value."""
        gear = _FakeGear(silent=(QueryMaxLevel, QueryMinLevel))
        device = await self.device_with_info(gear)

        await device.execute_control(gear, WANTED_LEVEL, "100")

        self.assertNotIn("max_level", device.params)
        self.assertNotIn("min_level", device.params)
        self.assertEqual([command.power for command in gear.sent_of(DAPC)], [254])

    async def test_gear_silent_about_everything_still_loads(self):
        """The bench device: it answers the device type and nothing else.

        The settings page opens with the common fields and the assumed curve, and none of the
        gear settings; before, the read failed on the first of them and the page stayed shut.
        """
        gear = _FakeGear(answers={QueryDeviceType: DaliDeviceType.LED_MODULES.value})
        device = await self.device_with_info(gear)

        properties = device.schema["properties"]
        self.assertEqual(device.params["short_address"], 1)
        self.assertIn("dimming_curve", properties)
        for unread in (
            "groups",
            "scenes",
            "max_level",
            "min_level",
            "power_on_level",
            "system_failure_level",
            "fade_time",
            "type_6_fast_fade_time",
        ):
            self.assertNotIn(unread, properties)
            self.assertNotIn(unread, device.params)

    async def test_unread_param_is_not_written(self):
        """A field the schema does not offer is not written either.

        Nothing validates its value any more — the schema field it was checked against is gone —
        and there is no read value to write over, so such a key is ignored instead of reaching
        the gear's memory.
        """
        gear = _FakeGear(silent=(QueryMinLevel,))
        device = await self.device_with_info(gear)

        await device.apply_parameters(gear, {**device.params, "min_level": 5})

        self.assertEqual(gear.sent_of(SetMinLevel), [])

    async def test_out_of_range_min_level_is_accepted(self):
        """A MIN level of 0 is outside what the standard allows, and is taken as reported: the
        gear must keep working, and there is nothing to check the value against."""
        gear = _FakeGear(answers={**GEAR_ANSWERS, QueryMinLevel: 0})
        device = await self.device_with_info(gear)

        self.assertEqual(device.params["min_level"], 0)
        self.assertIn("min_level", device.schema["properties"])

    async def test_answering_device_unchanged(self):
        """Regression: gear that answers everything reads real values and keeps every field.

        The curve is the linear one here, so a fallback to the standard curve would show up.
        """
        gear = _FakeGear()
        device = await self.device_with_info(gear)

        self.assertEqual(device.params["dimming_curve"], DimmingCurveType.LINEAR)
        self.assertEqual(device.dimming_curve_type, DimmingCurveType.LINEAR)
        self.assertEqual(device.params["min_level"], 10)
        self.assertEqual(device.params["groups"][1], True)
        self.assertEqual(len(device.params["scenes"]), 16)
        properties = device.schema["properties"]
        for expected in ("dimming_curve", "min_level", "max_level", "groups", "scenes"):
            self.assertIn(expected, properties)
        self.assertNotIn("wb", properties["dimming_curve"]["options"])


class TestSilentDimmingCurveField(_GearTestCase):
    async def test_unread_dimming_curve_shown_as_standard_read_only(self):
        """The curve field stays in the schema when the query is silent, read-only and standard.

        Percent are converted by the standard curve anyway, so the shown value is the real one —
        exactly what gear whose type has no curve support gets. A write attempt reaches no bus.
        """
        gear = _FakeGear(silent=(QueryDimmingCurve,))
        device = await self.device_with_info(gear)

        curve_property = device.schema["properties"]["dimming_curve"]
        self.assertEqual(device.params["dimming_curve"], DimmingCurveType.LOGARITHMIC)
        self.assertEqual(curve_property["options"]["wb"], {"read_only": True})

        await device.apply_parameters(gear, {**device.params, "dimming_curve": DimmingCurveType.LINEAR})

        self.assertEqual(gear.sent_of(SelectDimmingCurve), [])
        self.assertEqual(device.dimming_curve_type, DimmingCurveType.LOGARITHMIC)

    async def test_answer_adds_the_field_back(self):
        """The gear answers the curve query on a later read: the value is taken, and the field
        becomes an ordinary writable one."""
        gear = _FakeGear(silent=(QueryDimmingCurve,))
        device = await self.device_with_info(gear)
        gear.silent = ()

        await device.load_info(gear, force_reload=True)

        self.assertEqual(device.params["dimming_curve"], DimmingCurveType.LINEAR)
        self.assertEqual(device.dimming_curve_type, DimmingCurveType.LINEAR)
        self.assertNotIn("wb", device.schema["properties"]["dimming_curve"]["options"])

    async def test_unread_setting_logged_once(self):
        """Each setting the gear does not report says so itself, once, not on every read.

        Groups and the curve are asked at init and again at every settings read, scenes only at
        the settings read, so every one of them is met more than once here.
        """
        gear = _FakeGear(silent=(QueryDimmingCurve, QuerySceneLevel, *GROUP_QUERIES))
        device = self.make_device()

        with self.assertLogs(_LOGGER_NAME, level=logging.INFO) as captured:
            await device.initialize(gear)
            await device.load_info(gear)
            await device.load_info(gear, force_reload=True)

        self.assertEqual(len([line for line in captured.output if '"Groups"' in line]), 1)
        self.assertEqual(len([line for line in captured.output if '"Scenes"' in line]), 1)
        self.assertEqual(
            len([line for line in captured.output if "does not report its dimming curve" in line]), 1
        )

    async def test_silent_curve_of_any_type_assumes_standard(self):
        """The curve query of a DT17 device is a different command with the same meaning.

        Its silence gets the same treatment as type 6: the standard curve and a read-only field,
        instead of a device that never appears in MQTT at all.
        """
        gear = _FakeGear(
            answers={**GEAR_ANSWERS, QueryDeviceType: DaliDeviceType.DIMMING_CURVE_SELECTION.value},
            silent=(QueryType17DimmingCurve,),
        )
        device = await self.device_with_info(gear)

        self.assertEqual(device.params["dimming_curve"], DimmingCurveType.LOGARITHMIC)
        self.assertEqual(device.schema["properties"]["dimming_curve"]["options"]["wb"], {"read_only": True})

    async def test_lost_curve_answer_is_an_error(self):
        """The gear reported a curve and later stops answering the query.

        Silence means the gear does not implement the feature, and gear that answered once
        demonstrably does: the lost answer is a bus problem, so the read fails instead of
        quietly swapping the curve every percent-to-level conversion goes through.
        """
        gear = _FakeGear()
        device = await self.device_with_info(gear)
        gear.silent = (QueryDimmingCurve,)

        with self.assertRaises(RuntimeError) as caught:
            await device.load_info(gear, force_reload=True)

        self.assertIn("Dimming curve", str(caught.exception))
        self.assertEqual(device.dimming_curve_type, DimmingCurveType.LINEAR)


class TestReread(_GearTestCase):
    async def test_reread_repeats_the_initialization_reads(self):
        """A settings read asks again everything initialisation asked.

        Otherwise a feature the gear said nothing about at startup would stay absent forever:
        those queries are never repeated, and the background read covers only some params.
        """
        gear = _FakeGear()
        device = await self.device_with_info(gear)
        gear.sent.clear()

        await device.load_info(gear, force_reload=True)

        for repeated in (QueryDimmingCurve, QueryGroupsZeroToSeven, QueryFadeTimeFadeRate):
            self.assertTrue(gear.sent_of(repeated), f"{repeated.__name__} was not asked again")

    async def test_groups_come_back_after_an_answer(self):
        """The gear answers the group queries on a later read: membership is back.

        This is the whole point of repeating the initialisation reads — group membership drives
        the group virtual devices, and it is read nowhere else.
        """
        gear = _FakeGear(silent=GROUP_QUERIES)
        device = await self.device_with_info(gear)
        self.assertEqual(device.groups, set())
        gear.silent = ()

        await device.load_info(gear, force_reload=True)

        self.assertEqual(device.groups, {1})
        self.assertIn("groups", device.schema["properties"])


class TestUnreadableAnswerStillFails(_GearTestCase):
    async def test_framing_error_still_fails(self):
        """An unreadable answer means something on the bus answers wrongly, which is not
        smoothed over: it keeps failing both initialisation and the settings read."""
        device = self.make_device()
        with self.assertRaises(RuntimeError):
            await device.initialize(_FakeGear(framing_error=(QueryDimmingCurve,)))

        gear = _FakeGear(framing_error=(QueryMinLevel,))
        device = await self.initialized_device(gear)
        with self.assertRaises(RuntimeError):
            await device.load_info(gear)

    async def test_framing_error_in_a_batch_still_fails(self):
        """Both group queries go out as one batch: one silent and one unreadable answer in it is
        not silence, so initialisation fails instead of reporting no groups."""
        gear = _FakeGear(silent=(QueryGroupsZeroToSeven,), framing_error=(QueryGroupsEightToFifteen,))
        device = self.make_device()

        with self.assertRaises(RuntimeError):
            await device.initialize(gear)
        self.assertFalse(device.is_initialized)

    async def test_gateway_failure_is_not_silence(self):
        """A query the gateway could not carry out is an error, not an absent feature.

        No power on the bus, an overheated gateway or a reply timeout must not quietly hide
        every setting of every device instead of being reported.
        """
        gear = _FakeGear(gateway_error=(QueryMinLevel,))
        device = await self.initialized_device(gear)

        with self.assertRaises(RuntimeError) as raised:
            await device.load_info(gear)
        self.assertIn('Error reading "Min level"', str(raised.exception))

    async def test_lost_answer_after_a_read_is_an_error(self):
        """The gear reported a setting and later stops answering its query.

        Silence means the gear does not implement the feature, and gear that answered once
        demonstrably does, so the read fails instead of quietly dropping the setting.
        """
        gear = _FakeGear()
        device = await self.device_with_info(gear)
        self.assertIn("max_level", device.params)
        gear.silent = (QueryMaxLevel,)

        with self.assertRaises(RuntimeError) as caught:
            await device.load_info(gear, force_reload=True)

        self.assertIn("Max level", str(caught.exception))

    async def test_silent_param_outside_the_closed_list_still_fails(self):
        """Silence is tolerated only for the params that allow it, not for any param at all."""
        with patch.object(common_dali_device, "GeneralMemoryParams", _SilentMemoryParams):
            gear = _FakeGear()
            device = await self.initialized_device(gear)

            with self.assertRaises(RuntimeError) as raised:
                await device.load_info(gear)
        self.assertIn('Error reading "General memory parameters"', str(raised.exception))
