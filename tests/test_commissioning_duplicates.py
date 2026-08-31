"""Commissioning tests for buses with conflicting device addresses.

The per-device fake (unlike the {short: random} one in test_commissioning.py)
can hold a device without a short address or several sharing one. Simultaneous
answers merge like on a real bus: identical frames read as one valid frame,
differing ones as a framing error. It also carries gear that does not implement
WITHDRAW and so stays in the search after one.
"""

import unittest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import Mock

from dali.gear.general import (
    Compare,
    Initialise,
    ProgramShortAddress,
    QueryControlGearPresent,
    QueryRandomAddressH,
    QueryRandomAddressL,
    QueryRandomAddressM,
    QueryShortAddress,
    Randomise,
    SetSearchAddrH,
    SetSearchAddrL,
    SetSearchAddrM,
    Terminate,
    VerifyShortAddress,
    Withdraw,
)

from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.commissioning import Commissioning
from wb.mqtt_dali.dali_device import DaliDeviceAddress

NEW_RANDOM_START = 0x200000
BLOCKER_RANDOM = 0x111111
CONFORMING_RANDOM = 0x444444


class MockResponse:  # pylint: disable=R0903
    """Stand-in for a dali Response: ``error=None`` means no backframe at
    all, otherwise ``raw_value`` carries the framing-error flag."""

    def __init__(self, value=None, error=None):
        self.value = value
        self.raw_value = None if error is None else Mock(error=error)


@dataclass
class FakeDevice:
    short: Optional[int]
    random: int
    initialised: bool = False
    withdrawn: bool = False


class PerDeviceFakeDALIBus:
    def __init__(self, devices: list[FakeDevice]) -> None:
        self.devices = devices
        self.sent_commands: list = []
        self.search_addr: list[Optional[int]] = [None, None, None]
        self.next_random = NEW_RANDOM_START

    async def send(
        self, cmd, source=BusTrafficSource.WB, priority=None
    ):  # pylint: disable=R0911 disable=R0912 disable=unused-argument
        self.sent_commands.append(cmd)

        if isinstance(cmd, QueryControlGearPresent):
            return self._respond([True for dev in self.devices if dev.short == cmd.destination.address])

        if isinstance(cmd, (QueryRandomAddressH, QueryRandomAddressM, QueryRandomAddressL)):
            shift = {QueryRandomAddressH: 16, QueryRandomAddressM: 8, QueryRandomAddressL: 0}[type(cmd)]
            answers = [
                (dev.random >> shift) & 0xFF for dev in self.devices if dev.short == cmd.destination.address
            ]
            return self._respond(answers, wrap=lambda v: Mock(as_integer=v))

        if isinstance(cmd, SetSearchAddrH):
            self.search_addr[0] = cmd.param
            return MockResponse()
        if isinstance(cmd, SetSearchAddrM):
            self.search_addr[1] = cmd.param
            return MockResponse()
        if isinstance(cmd, SetSearchAddrL):
            self.search_addr[2] = cmd.param
            return MockResponse()

        if isinstance(cmd, Compare):
            search = self._search()
            if search is None:
                return MockResponse()
            return self._respond(
                [
                    True
                    for dev in self.devices
                    if dev.initialised and not dev.withdrawn and dev.random <= search
                ]
            )

        if isinstance(cmd, QueryShortAddress):
            search = self._search()
            if search is None:
                return MockResponse()
            # WITHDRAW mutes COMPARE only: withdrawn devices still answer here
            # (smart_extend resets short addresses after the WITHDRAW).
            answers = [
                "MASK" if dev.short is None else (dev.short << 1) | 1
                for dev in self.devices
                if dev.initialised and dev.random == search
            ]
            return self._respond(answers)

        if isinstance(cmd, Withdraw):
            search = self._search()
            for dev in self.devices:
                if dev.initialised and dev.random == search:
                    dev.withdrawn = True
            return MockResponse()

        if isinstance(cmd, Terminate):
            for dev in self.devices:
                dev.initialised = False
                dev.withdrawn = False
            return MockResponse()

        if isinstance(cmd, Initialise):
            for dev in self.devices:
                if cmd.broadcast or dev.short == cmd.address:
                    dev.initialised = True
            return MockResponse()

        if isinstance(cmd, Randomise):
            # Accepted by every device in initialisation state, withdrawn or not.
            for dev in self.devices:
                if dev.initialised:
                    dev.random = self.next_random
                    self.next_random += 1
            return MockResponse()

        if isinstance(cmd, ProgramShortAddress):
            search = self._search()
            for dev in self.devices:
                if dev.initialised and dev.random == search:
                    dev.short = None if cmd.address == "MASK" else cmd.address
            return MockResponse()

        if isinstance(cmd, VerifyShortAddress):
            return self._respond(
                [True for dev in self.devices if dev.initialised and dev.short == cmd.address]
            )

        return MockResponse()

    async def send_commands(
        self, cmds, source=BusTrafficSource.WB, priority=None
    ):  # pylint: disable=unused-argument
        return [await self.send(cmd, source, priority) for cmd in cmds]

    # --- Private ---

    def _search(self) -> Optional[int]:
        if None in self.search_addr:
            return None
        return (self.search_addr[0] << 16) | (self.search_addr[1] << 8) | self.search_addr[2]

    def _respond(self, answers, wrap=None):
        if not answers:
            return MockResponse()
        if all(answer == answers[0] for answer in answers):
            return MockResponse(value=wrap(answers[0]) if wrap else answers[0], error=False)
        return MockResponse(value=None, error=True)


class WithdrawIgnoringBus(PerDeviceFakeDALIBus):
    """The per-device fake, with `ignoring` devices staying in the search after WITHDRAW."""

    def __init__(self, devices: list[FakeDevice], ignoring: list[FakeDevice]) -> None:
        super().__init__(devices)
        self.ignoring = ignoring

    async def send(self, cmd, source=BusTrafficSource.WB, priority=None):
        response = await super().send(cmd, source, priority)
        if isinstance(cmd, Withdraw):
            for device in self.ignoring:
                device.withdrawn = False
        return response


def programmed_addresses(bus: PerDeviceFakeDALIBus) -> list:
    return [cmd.address for cmd in bus.sent_commands if isinstance(cmd, ProgramShortAddress)]


class TestCommissioningAddressConflicts(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_random_invisible_at_poll_keeps_config_entry(self):
        """Duplicate invisible at poll: B shares X but has no short address,
        so the poll reads X cleanly at short 5.

        Config     | Bus
        ------------------------------------------
        (5, X)     | A: short 5, random X
                   | B: no short, random X

        Confirming X collides (framing error); the pair (5, X) is
        poll-confirmed, so A is kept as unchanged and untouched on the bus,
        B alone is randomised and found as a new device.
        """
        random_x = 0x111111
        device_a = FakeDevice(short=5, random=random_x)
        device_b = FakeDevice(short=None, random=random_x)
        bus = PerDeviceFakeDALIBus([device_a, device_b])

        commissioning = Commissioning(bus, [DaliDeviceAddress(short=5, random=random_x)])
        result = await commissioning.smart_extend()

        self.assertEqual(result.unchanged, [DaliDeviceAddress(short=5, random=random_x)])
        self.assertEqual(result.new, [DaliDeviceAddress(short=0, random=NEW_RANDOM_START)])
        self.assertEqual(result.missing, [])
        self.assertEqual(result.changed, [])
        self.assertEqual(device_a.short, 5)
        self.assertEqual(device_a.random, random_x)
        self.assertEqual(device_b.short, 0)

    async def test_hidden_short_duplicate_stays_deferred(self):
        """The duplicate C holds X and a short address hidden by the poll
        collision at short 7, so the poll cannot see that C holds X.

        Config     | Bus
        ------------------------------------------
        (5, X)     | A: short 5, random X
                   | C: short 7, random X
                   | D: short 7, random Z

        A is spared; randomising addressless devices does not touch C, so it
        stays undiscovered, sharing X — the deliberate trade-off of the trust
        rule. D is found and keeps short 7.
        """
        random_x = 0x111111
        random_z = 0x333333
        device_a = FakeDevice(short=5, random=random_x)
        device_c = FakeDevice(short=7, random=random_x)
        device_d = FakeDevice(short=7, random=random_z)
        bus = PerDeviceFakeDALIBus([device_a, device_c, device_d])

        commissioning = Commissioning(bus, [DaliDeviceAddress(short=5, random=random_x)])
        result = await commissioning.smart_extend()

        self.assertEqual(result.unchanged, [DaliDeviceAddress(short=5, random=random_x)])
        self.assertEqual(result.new, [DaliDeviceAddress(short=7, random=random_z)])
        self.assertEqual(result.missing, [])
        self.assertEqual(result.changed, [])
        self.assertEqual(device_a.short, 5)
        self.assertEqual(device_a.random, random_x)
        self.assertEqual((device_c.short, device_c.random), (7, random_x))
        self.assertEqual((device_d.short, device_d.random), (7, random_z))

    async def test_config_only_random_conflict_is_not_trusted(self):
        """Duplicated X without a poll-confirmed config pair: X reads from
        short 6 while the config records it at short 5, so no trust is
        granted. Both holders are reset, randomised and found as new;
        (5, X) is reported missing.

        Config     | Bus
        ------------------------------------------
        (5, X)     | A: no short, random X
                   | C: short 6, random X
        """
        random_x = 0x111111
        device_a = FakeDevice(short=None, random=random_x)
        device_c = FakeDevice(short=6, random=random_x)
        bus = PerDeviceFakeDALIBus([device_a, device_c])

        commissioning = Commissioning(bus, [DaliDeviceAddress(short=5, random=random_x)])
        result = await commissioning.smart_extend()

        self.assertEqual(result.missing, [DaliDeviceAddress(short=5, random=random_x)])
        self.assertEqual(result.unchanged, [])
        self.assertEqual(result.changed, [])
        self.assertEqual(len(result.new), 2)
        self.assertEqual({device_a.short, device_c.short}, {0, 1})

    async def test_duplicate_short_address_config_keeps_its_device(self):
        """Duplicated short address 5, distinct randoms: the random read at
        short 5 collides, the poll learns nothing and sends no RANDOMISE.
        The config entry finds A at X and it keeps short 5; B is found by
        binary search and moved to a free short address.

        Config     | Bus
        ------------------------------------------
        (5, X)     | A: short 5, random X
                   | B: short 5, random Y
        """
        random_x = 0x111111
        random_y = 0x222222
        device_a = FakeDevice(short=5, random=random_x)
        device_b = FakeDevice(short=5, random=random_y)
        bus = PerDeviceFakeDALIBus([device_a, device_b])

        commissioning = Commissioning(bus, [DaliDeviceAddress(short=5, random=random_x)])
        result = await commissioning.smart_extend()

        self.assertEqual(result.unchanged, [DaliDeviceAddress(short=5, random=random_x)])
        self.assertEqual(result.new, [DaliDeviceAddress(short=0, random=random_y)])
        self.assertEqual(result.missing, [])
        self.assertEqual(result.changed, [])
        self.assertEqual(device_a.short, 5)
        self.assertEqual(device_b.short, 0)
        self.assertFalse(any(isinstance(cmd, Randomise) for cmd in bus.sent_commands))

    async def test_duplicate_short_address_without_config_first_found_keeps_it(self):
        """Same duplicated short address 5 with an empty config: binary
        search walks randoms upward, so the device with the lower random is
        found first and keeps short 5; the other is moved to a free one.
        Both are reported as new.

        Config     | Bus
        ------------------------------------------
        (empty)    | A: short 5, random X
                   | B: short 5, random Y   (X < Y)
        """
        random_x = 0x111111
        random_y = 0x222222
        device_a = FakeDevice(short=5, random=random_x)
        device_b = FakeDevice(short=5, random=random_y)
        bus = PerDeviceFakeDALIBus([device_a, device_b])

        commissioning = Commissioning(bus, [])
        result = await commissioning.smart_extend()

        self.assertEqual(
            result.new,
            [
                DaliDeviceAddress(short=5, random=random_x),
                DaliDeviceAddress(short=0, random=random_y),
            ],
        )
        self.assertEqual(result.unchanged, [])
        self.assertEqual(result.missing, [])
        self.assertEqual(result.changed, [])
        self.assertEqual(device_a.short, 5)
        self.assertEqual(device_b.short, 0)
        self.assertFalse(any(isinstance(cmd, Randomise) for cmd in bus.sent_commands))


class TestWithdrawIgnoringGear(unittest.IsolatedAsyncioTestCase):
    """Control gear that does not implement WITHDRAW keeps answering COMPARE after it, so the
    search finds it over and over.
    A find reporting a short address this run already recorded for that random address is the
    same device, and is left alone; the search then makes no progress and gives up with its
    stuck error, which is as far as such a bus can be enumerated.
    """

    async def test_gear_found_again_gets_no_second_short_address(self):
        """The blocker holds short address 1 and is found again after WITHDRAW: no address is
        programmed onto the bus, the journal names it, and the search gives up."""
        blocker = FakeDevice(short=1, random=BLOCKER_RANDOM)
        bus = WithdrawIgnoringBus([blocker], ignoring=[blocker])

        commissioning = Commissioning(bus, [DaliDeviceAddress(short=1, random=BLOCKER_RANDOM)])
        with self.assertLogs("commissioning", "WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "Binary search stuck"):
                await commissioning.smart_extend()

        self.assertEqual(blocker.short, 1)
        self.assertEqual(programmed_addresses(bus), [])
        self.assertTrue(
            any("did not leave the device search after WITHDRAW" in line for line in logs.output),
            logs.output,
        )

    async def test_gear_without_short_address_is_addressed_only_once(self):
        """The blocker has no short address, so the first find is not a repeat and programs
        one. Every find after it is, so exactly one address is programmed onto the bus."""
        blocker = FakeDevice(short=None, random=BLOCKER_RANDOM)
        bus = WithdrawIgnoringBus([blocker], ignoring=[blocker])

        with self.assertRaisesRegex(RuntimeError, "Binary search stuck"):
            await Commissioning(bus, []).smart_extend()

        self.assertEqual(blocker.short, 0)
        self.assertEqual(programmed_addresses(bus), [0])

    async def test_random_address_conflict_is_still_randomised(self):
        """Two devices really do share a random address and both respect WITHDRAW. The framing
        error on QUERY SHORT ADDRESS is not a repeat find: the addressless one is randomised
        between passes and picks up its own short address, the configured one keeps its."""
        configured = FakeDevice(short=7, random=CONFORMING_RANDOM)
        duplicate = FakeDevice(short=None, random=CONFORMING_RANDOM)
        bus = WithdrawIgnoringBus([configured, duplicate], ignoring=[])

        config = [DaliDeviceAddress(short=7, random=CONFORMING_RANDOM)]
        result = await Commissioning(bus, config).smart_extend()

        self.assertEqual(result.unchanged, config)
        self.assertEqual(result.changed, [])
        self.assertEqual(result.missing, [])
        self.assertEqual((configured.short, configured.random), (7, CONFORMING_RANDOM))
        self.assertNotEqual(duplicate.random, CONFORMING_RANDOM)
        self.assertEqual(result.new, [DaliDeviceAddress(short=duplicate.short, random=duplicate.random)])
        self.assertTrue(any(isinstance(cmd, Randomise) for cmd in bus.sent_commands))
