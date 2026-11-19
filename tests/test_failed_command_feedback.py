import asyncio
from typing import Callable, List, Optional, Sequence
from unittest.mock import MagicMock

import aiomqtt
from dali.address import GearShort
from dali.command import Command
from dali.gear.colour import Activate, QueryColourValue
from dali.gear.general import DAPC, Off, QueryActualLevel

from wb.mqtt_dali.application_controller import (
    ApplicationController,
    ApplicationControllerConfig,
)
from wb.mqtt_dali.common_dali_device import (
    EVENT_RESYNC_BASE_INTERVAL,
    DaliDeviceAddress,
)
from wb.mqtt_dali.control_ids import ACTUAL_LEVEL, ON_OFF, SET_RGB, WANTED_LEVEL
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.on_off_control import (
    OffAction,
    OffActionMode,
    OnAction,
    OnActionMode,
    OnOffConfig,
)

from ._broker_link_helpers import Inbox
from ._common_schema import load_common_schema
from ._on_off_helpers import BusScript, ReplyStatus, ScriptedBus, ScriptedGatewayClient
from ._virtual_time import VirtualTimeTestCase

ADDR = GearShort(5)
OTHER_ADDR = GearShort(6)
GROUP = 3

NO_REPLY = None
NO_POWER_ON_BUS = ReplyStatus.NO_POWER_ON_BUS << 8
DT8 = 8
# Every colour query answers this: the RGBWAF colour type, and then each component at that value.
RGBWAF_COLOUR_VALUE = 0x80

ON_OFF_CONFIG = OnOffConfig(
    on_action=OnAction(OnActionMode.DAPC, value=200), off_action=OffAction(OffActionMode.DAPC)
)

# Longer than the settle of a no-fade command plus the polling loop's idle ceiling.
NO_READ_WINDOW_S = 2.0
# Longer than the longest jittered interval of the regular level poll.
REGULAR_POLL_WINDOW_S = 2 * EVENT_RESYNC_BASE_INTERVAL


class _GatewayInbox(Inbox):
    """Drops the replies the scripted gateway decided not to send."""

    def deliver(self, message: aiomqtt.Message) -> None:
        if message.payload != str(NO_REPLY):
            super().deliver(message)


class _FlakyGatewayClient(ScriptedGatewayClient):
    """The scripted gateway, with one scripted reply for each of the next frames a test picks."""

    def __init__(self, script: BusScript, device_name: str, bus: int) -> None:
        super().__init__(script, device_name, bus)
        self.messages = _GatewayInbox()
        self._matches: Callable[[Command], bool] = lambda _command: False
        self._replies: List[Optional[int]] = []

    def fail_frames(self, matches: Callable[[Command], bool], replies: Sequence[Optional[int]]) -> None:
        self._matches = matches
        self._replies = list(replies)

    # --- Hooks for subclasses ---

    def _reply_for(self, register: int) -> Optional[int]:
        seen = len(self.sent)
        reply = super()._reply_for(register)
        if len(self.sent) > seen and self._replies and self._matches(self.sent[-1]):
            return self._replies.pop(0)
        return reply


class _FlakyBus(ScriptedBus):
    def __init__(self) -> None:
        self.script = BusScript(answers={"QueryActualLevel": 0}, groups={5: {GROUP}, 6: {GROUP}})
        super().__init__(self.script)
        self.client = _FlakyGatewayClient(self.script, "gw1", 1)
        self.dispatcher = MQTTDispatcher(self.client)

    def fail_frames(self, matches: Callable[[Command], bool], replies: Sequence[Optional[int]]) -> None:
        self.client.fail_frames(matches, replies)

    def reads_of(self, address: GearShort) -> int:
        return sum(
            1
            for command in self.sent
            if isinstance(command, QueryActualLevel) and command.destination == address
        )

    def colour_reads(self) -> int:
        return sum(1 for command in self.sent if isinstance(command, QueryColourValue))

    async def start_device(self) -> tuple[ApplicationController, str]:
        """Start a bus with the light at short address 5 and forget its startup traffic."""
        bus = await self._start_lights([_light(5, None)])
        return bus, bus.dali_devices[0].mqtt_id

    async def start_switch(self) -> str:
        """Start a bus with the light at 5 carrying an ``on_off`` and forget its startup traffic."""
        bus = await self._start_lights([_light(5, ON_OFF_CONFIG)])
        return bus.dali_devices[0].mqtt_id

    async def start_group(self) -> tuple[ApplicationController, str]:
        """Start a bus with the lights at 5 and 6 in one group and forget its startup traffic."""
        bus = await self._start_lights([_light(5, None), _light(6, None)])
        return bus, bus.group_devices[GROUP].mqtt_id

    async def start_group_switch(self) -> str:
        """Start a bus with the lights at 5 and 6 in one group carrying an ``on_off``."""
        bus = await self._start_lights([_light(5, None), _light(6, None)], {GROUP: ON_OFF_CONFIG})
        return bus.group_devices[GROUP].mqtt_id

    async def write_failing(self, device_id: str, control_id: str, payload: str) -> None:
        await self.write(device_id, control_id, payload)
        await self.wait_for_error(device_id, control_id, "w")

    async def wait_for_error(self, device_id: str, control_id: str, error: str) -> None:
        await self.wait_until(lambda: self.control_error(device_id, control_id) == error)

    def error_publishes(self, device_id: str, control_id: str) -> List[Optional[str]]:
        topic = f"/devices/{device_id}/controls/{control_id}/meta/error"
        return [publish.payload for publish in self.client.publishes if publish.topic == topic]

    async def wait_for_error_cleared(self, device_id: str, control_id: str) -> None:
        await self.wait_until(lambda: self.error_publishes(device_id, control_id)[-1:] == [None])
        await self.settle()

    # --- Private ---

    async def _start_lights(
        self, lights: List[DaliDevice], group_on_off: Optional[dict[int, OnOffConfig]] = None
    ) -> ApplicationController:
        bus = ApplicationController(
            ApplicationControllerConfig("gw1", 1, lights, [], group_on_off=group_on_off or {}),
            self.dispatcher,
            MagicMock(),
            load_common_schema(),
            MagicMock(),
        )
        await self.start(bus)
        await self.wait_until(lambda: all(light.is_initialized for light in lights))
        await self.settle()
        self.drain()
        return bus


def _light(short: int, on_off: Optional[OnOffConfig]) -> DaliDevice:
    return DaliDevice(
        DaliDeviceAddress(short, 0x123456 + short),
        "gw1_bus_1",
        MagicMock(),
        load_common_schema(),
        on_off=on_off,
    )


def _is_dapc(command: Command) -> bool:
    return isinstance(command, DAPC)


def _is_off(command: Command) -> bool:
    return isinstance(command, Off)


class FailedCommandFeedbackTest(VirtualTimeTestCase):  # pylint: disable=too-many-public-methods

    async def test_failed_write_sets_write_error_and_republishes_the_level(self):
        """A level write the gateway fails, unanswered or with no power on the bus: ``w``, the
        unchanged ``wanted_level = 0`` goes out again, and nothing is read."""
        for replies in ([NO_REPLY] * 3, [NO_POWER_ON_BUS] * 3):
            with self.subTest(replies=replies):
                async with _FlakyBus() as harness:
                    _bus, device_id = await harness.start_device()
                    harness.fail_frames(_is_dapc, replies)

                    await harness.write_failing(device_id, WANTED_LEVEL, "100")
                    await asyncio.sleep(NO_READ_WINDOW_S)

                    self.assertEqual(harness.value_publishes(device_id, WANTED_LEVEL), ["0"])
                    self.assertEqual(harness.reads_of(ADDR), 0)

    async def test_poll_finds_the_command_did_arrive(self):
        """The failed write did reach the light: the poll publishes the new levels and clears ``w``."""
        async with _FlakyBus() as harness:
            _bus, device_id = await harness.start_device()
            harness.fail_frames(_is_dapc, [NO_REPLY] * 3)
            harness.script.answers["QueryActualLevel"] = 254

            await harness.write_failing(device_id, WANTED_LEVEL, "100")
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            await harness.wait_for_error_cleared(device_id, WANTED_LEVEL)

            self.assertEqual(harness.value_publishes(device_id, WANTED_LEVEL), ["0", "100"])
            self.assertEqual(harness.value_publishes(device_id, ACTUAL_LEVEL), ["100.000"])

    async def test_failed_poll_keeps_the_write_error(self):
        """The poll after a failed write fails as well, on a light and a group: ``w`` holds until a
        poll answers, which clears it without publishing the value again."""
        for name, start in (("light", _FlakyBus.start_device), ("group", _FlakyBus.start_group)):
            with self.subTest(name):
                async with _FlakyBus() as harness:
                    _bus, device_id = await start(harness)
                    harness.fail_frames(_is_dapc, [NO_REPLY] * 3)
                    harness.script.unanswered = frozenset({"QueryActualLevel"})

                    await harness.write_failing(device_id, WANTED_LEVEL, "100")
                    await asyncio.sleep(REGULAR_POLL_WINDOW_S)
                    await harness.wait_for_error(device_id, ACTUAL_LEVEL, "r")
                    await harness.settle()
                    self.assertEqual(harness.control_error(device_id, WANTED_LEVEL), "w")

                    harness.script.unanswered = frozenset()
                    await asyncio.sleep(REGULAR_POLL_WINDOW_S)
                    await harness.wait_for_error_cleared(device_id, WANTED_LEVEL)

                    self.assertEqual(harness.value_publishes(device_id, WANTED_LEVEL), ["0"])

    async def test_new_command_after_a_failed_one_clears_the_write_error(self):
        """A successful write of 50 after a failed one: the prediction is published and ``w`` is
        cleared at once; its confirming read publishes nothing new."""
        async with _FlakyBus() as harness:
            bus, device_id = await harness.start_device()
            harness.fail_frames(_is_dapc, [NO_REPLY] * 3)
            level_50 = bus.dali_devices[0].get_mqtt_control(WANTED_LEVEL).get_setup_commands(ADDR, "50")[0]
            harness.script.answers["QueryActualLevel"] = level_50.power

            await harness.write_failing(device_id, WANTED_LEVEL, "100")
            await harness.write(device_id, WANTED_LEVEL, "50")

            # The level-to-percent curve turns the 50 into the nearest percent the gear can show.
            published = harness.value_publishes(device_id, WANTED_LEVEL)
            self.assertEqual(len(published), 2)
            self.assertEqual(harness.error_publishes(device_id, WANTED_LEVEL)[-1:], [None])
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(harness.value_publishes(device_id, WANTED_LEVEL), published)

    async def test_failed_command_does_not_become_the_control_value(self):
        """Two failed writes, then a poll: the control keeps 0 and 100 is never published."""
        async with _FlakyBus() as harness:
            bus, device_id = await harness.start_device()
            harness.fail_frames(_is_dapc, [NO_POWER_ON_BUS] * 3)
            await harness.write_failing(device_id, WANTED_LEVEL, "100")

            harness.fail_frames(_is_dapc, [NO_REPLY] * 3)
            await harness.write(device_id, WANTED_LEVEL, "80")
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            await harness.wait_for_error_cleared(device_id, WANTED_LEVEL)

            self.assertEqual(harness.value_publishes(device_id, WANTED_LEVEL), ["0", "0"])
            control = bus.dali_devices[0].get_mqtt_control(WANTED_LEVEL)
            self.assertEqual(control.control_info.state.value, "0")

    async def test_group_settles_on_the_poll_of_its_leading_member(self):
        """A failed group write: ``w`` and the old ``wanted_level`` on the group, no member is read,
        and the regular poll the group takes clears ``w``."""
        async with _FlakyBus() as harness:
            _bus, group_id = await harness.start_group()
            harness.fail_frames(_is_dapc, [NO_REPLY] * 3)

            await harness.write_failing(group_id, WANTED_LEVEL, "100")
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(harness.value_publishes(group_id, WANTED_LEVEL), ["0"])
            self.assertEqual(harness.reads_of(ADDR) + harness.reads_of(OTHER_ADDR), 0)

            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            await harness.wait_for_error_cleared(group_id, WANTED_LEVEL)
            self.assertEqual(harness.value_publishes(group_id, WANTED_LEVEL), ["0"])

    async def test_group_write_error_cleared_by_the_next_group_command(self):
        """A failed group write, then one that goes through: ``w`` is cleared at once, and the
        group takes the new level from its leading member."""
        async with _FlakyBus() as harness:
            _bus, group_id = await harness.start_group()
            harness.fail_frames(_is_dapc, [NO_REPLY] * 3)

            await harness.write_failing(group_id, WANTED_LEVEL, "100")
            await harness.write(group_id, WANTED_LEVEL, "50")

            self.assertIsNone(harness.control_error(group_id, WANTED_LEVEL))
            # The old level, then the new one from the leading member, on the curve's nearest percent.
            await harness.wait_until(lambda: len(harness.value_publishes(group_id, WANTED_LEVEL)) == 2)

    async def test_button_write_error_cleared_only_by_the_next_command(self):
        """A failed ``off``: ``w`` on the button with no press and no read; the regular poll keeps
        ``w``, the next successful ``off`` clears it."""
        async with _FlakyBus() as harness:
            _bus, device_id = await harness.start_device()
            harness.fail_frames(_is_off, [NO_REPLY] * 3)

            await harness.write_failing(device_id, "off", "1")
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(harness.value_publishes(device_id, "off"), [])
            self.assertEqual(harness.reads_of(ADDR), 0)

            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            await harness.wait_until(lambda: harness.reads_of(ADDR) > 0)
            await harness.settle()
            self.assertEqual(harness.control_error(device_id, "off"), "w")
            self.assertEqual(harness.value_publishes(device_id, "off"), [])

            await harness.write(device_id, "off", "1")
            self.assertIsNone(harness.control_error(device_id, "off"))

    async def test_successful_command_publishes_the_prediction(self):
        """A write that goes through: the predicted level is published at once with no ``w``,
        and the level is read once the fade is over."""
        async with _FlakyBus() as harness:
            _bus, device_id = await harness.start_device()
            harness.script.answers["QueryActualLevel"] = 254

            await harness.write(device_id, WANTED_LEVEL, "100")

            self.assertEqual(harness.value_publishes(device_id, WANTED_LEVEL), ["100"])
            self.assertIsNone(harness.control_error(device_id, WANTED_LEVEL))
            await harness.wait_until(lambda: harness.reads_of(ADDR) > 0)
            await harness.settle()
            self.assertEqual(harness.value_publishes(device_id, WANTED_LEVEL), ["100"])
            self.assertEqual(harness.error_publishes(device_id, WANTED_LEVEL), [])

    async def test_successful_button_press_is_not_repeated_by_the_poll(self):
        """An ``off`` that goes through, then a regular poll: no ``w``, and the press the write
        published is not published again."""
        async with _FlakyBus() as harness:
            _bus, device_id = await harness.start_device()

            await harness.write(device_id, "off", "1")
            self.assertEqual(harness.value_publishes(device_id, "off"), ["1"])
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            await harness.wait_until(lambda: harness.reads_of(ADDR) > 0)
            await harness.settle()

            self.assertEqual(harness.value_publishes(device_id, "off"), ["1"])
            self.assertEqual(harness.error_publishes(device_id, "off"), [])

    async def test_group_button_write_error_cleared_only_by_the_next_command(self):
        """A failed group ``off``: members' regular polls keep ``w`` on the group button, the next
        successful group ``off`` clears it."""
        async with _FlakyBus() as harness:
            _bus, group_id = await harness.start_group()
            harness.fail_frames(_is_off, [NO_REPLY] * 3)

            await harness.write_failing(group_id, "off", "1")
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            await harness.wait_until(lambda: harness.reads_of(ADDR) > 0)
            await harness.settle()
            self.assertEqual(harness.control_error(group_id, "off"), "w")
            self.assertEqual(harness.value_publishes(group_id, "off"), [])

            await harness.write(group_id, "off", "1")
            self.assertIsNone(harness.control_error(group_id, "off"))

    async def test_failed_colour_is_settled_by_the_colour_poll(self):
        """A failed ``set_rgb`` on DT8 gear: ``w`` and the old ``set_rgb`` go out, the colour is not
        read until the regular poll, which clears ``w``."""
        async with _FlakyBus() as harness:
            harness.script.answers.update(QueryDeviceType=DT8, QueryColourValue=RGBWAF_COLOUR_VALUE)
            _bus, device_id = await harness.start_device()
            harness.fail_frames(lambda command: isinstance(command, Activate), [NO_REPLY] * 3)

            await harness.write_failing(device_id, SET_RGB, "255;0;0")
            await asyncio.sleep(NO_READ_WINDOW_S)
            old_colour = ";".join([str(RGBWAF_COLOUR_VALUE)] * 3)
            self.assertEqual(harness.value_publishes(device_id, SET_RGB), [old_colour])
            self.assertEqual(harness.colour_reads(), 0)

            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            await harness.wait_for_error_cleared(device_id, SET_RGB)
            self.assertEqual(harness.value_publishes(device_id, SET_RGB), [old_colour])

    async def test_failed_broadcast_is_cleared_by_the_next_command(self):
        """A failed broadcast write: ``w`` with no light read, and the next successful broadcast
        write clears ``w``."""
        async with _FlakyBus() as harness:
            bus, _group_id = await harness.start_group()
            broadcast_id = f"{bus.uid}_broadcast"
            harness.fail_frames(_is_dapc, [NO_REPLY] * 3)

            await harness.write_failing(broadcast_id, WANTED_LEVEL, "100")
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(harness.reads_of(ADDR) + harness.reads_of(OTHER_ADDR), 0)
            self.assertEqual(harness.control_error(broadcast_id, WANTED_LEVEL), "w")

            await harness.write(broadcast_id, WANTED_LEVEL, "100")
            self.assertIsNone(harness.control_error(broadcast_id, WANTED_LEVEL))

    async def test_switch_read_error_survives_a_successful_command(self):
        """``on_off`` in ``r`` takes a write that goes through: ``r`` stays with no prediction,
        and the read that answers clears ``r``."""
        async with _FlakyBus() as harness:
            harness.script.unanswered = frozenset({"QueryActualLevel"})
            device_id = await harness.start_switch()

            await harness.write(device_id, ON_OFF, "1")

            self.assertEqual(harness.value_publishes(device_id, ON_OFF), [])
            self.assertEqual(harness.error_publishes(device_id, ON_OFF), [])

            harness.script.unanswered = frozenset()
            await harness.wait_for_error_cleared(device_id, ON_OFF)

    async def test_group_switch_failed_write_settles_on_the_member_poll(self):
        """A failed group ``on_off`` write: ``w`` and the old ``on_off`` go out, no member is read
        until the regular poll, which clears ``w``."""
        async with _FlakyBus() as harness:
            group_id = await harness.start_group_switch()
            harness.fail_frames(_is_dapc, [NO_REPLY] * 3)

            await harness.write_failing(group_id, ON_OFF, "1")
            await asyncio.sleep(NO_READ_WINDOW_S)
            self.assertEqual(harness.value_publishes(group_id, ON_OFF), ["0"])
            self.assertEqual(harness.reads_of(ADDR) + harness.reads_of(OTHER_ADDR), 0)

            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            await harness.wait_for_error_cleared(group_id, ON_OFF)
            self.assertEqual(harness.value_publishes(group_id, ON_OFF), ["0"])

    async def test_switch_read_error_gains_the_write_error(self):
        """``on_off`` in ``r`` takes a write that fails: ``rw``, and the last known ``on_off`` goes
        out again; a poll that answers clears both."""
        async with _FlakyBus() as harness:
            harness.script.unanswered = frozenset({"QueryActualLevel"})
            device_id = await harness.start_switch()
            harness.fail_frames(_is_dapc, [NO_POWER_ON_BUS] * 3)

            await harness.write(device_id, ON_OFF, "1")

            self.assertEqual(harness.control_error(device_id, ON_OFF), "rw")
            self.assertEqual(harness.value_publishes(device_id, ON_OFF), ["0"])

            harness.script.unanswered = frozenset()
            await asyncio.sleep(REGULAR_POLL_WINDOW_S)
            await harness.wait_for_error_cleared(device_id, ON_OFF)
