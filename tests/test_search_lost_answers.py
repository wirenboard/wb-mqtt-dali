"""Binary search on a bus that loses answers.

Silence is the negative answer to COMPARE, so a lost answer reads as "no device here".
`CompareFaults` injects the losses.
"""

import unittest
from dataclasses import dataclass, field
from typing import Optional, Sequence

from dali.gear.general import Compare, ProgramShortAddress, QueryShortAddress, Terminate

from tests.test_commissioning_duplicates import (
    FakeDevice,
    MockResponse,
    PerDeviceFakeDALIBus,
)
from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.commissioning import (
    SILENT_COMPARE_ATTEMPTS,
    BinarySearchAddressFinder,
    Commissioning,
)

TOP = 0xFFFFFF
DEVICE_ADDR = 0x400000

# A search over the whole range asks this address first. The device is below it, so a loss
# here sends the bisection into the empty half, where it converges on FALSE_ADDR.
FIRST_MIDPOINT = 0x7FFFFF
FALSE_ADDR = 0x800000


@dataclass(frozen=True)
class Ask:
    """One COMPARE at `addr`: the `nth` request the bus sees there, counted from 1."""

    addr: int
    nth: int = 1


@dataclass
class CompareFaults:
    """What the fake bus does to COMPARE answers: `lost` swallows the answer to the requests
    it names, `lose_first_ask` swallows the first at every address and lets the repeats
    through."""

    lost: list[Ask] = field(default_factory=list)
    lose_first_ask: bool = False
    asks: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    injected: list[Ask] = field(default_factory=list, init=False, repr=False)

    def answer(self, addr: int, would_answer: bool) -> bool:
        nth = self.asks.get(addr, 0) + 1
        self.asks[addr] = nth
        ask = Ask(addr, nth)
        if would_answer and (ask in self.lost or (self.lose_first_ask and nth == 1)):
            self.injected.append(ask)
            return False
        return would_answer

    def all_injected(self) -> bool:
        """A test proves nothing about a loss the run never reached."""
        return all(ask in self.injected for ask in self.lost)


class LossySearchProbe:
    """What the finder selects through; the search address is not tracked here."""

    def __init__(self, devices: Sequence[int], faults: Optional[CompareFaults] = None) -> None:
        self.devices = list(devices)
        self.faults = faults or CompareFaults()
        self.calls: list[int] = []

    async def compare(self, addr: int) -> bool:
        self.calls.append(addr)
        return self.faults.answer(addr, any(dev <= addr for dev in self.devices))

    async def set_search_addr(self, addr: int) -> None:
        pass


class LossyPerDeviceBus(PerDeviceFakeDALIBus):
    """The shared per-device fake with COMPARE answers lost, everything else unchanged."""

    def __init__(self, devices: list[FakeDevice], faults: Optional[CompareFaults] = None) -> None:
        super().__init__(devices)
        self.faults = faults or CompareFaults()

    async def send(self, cmd, source=BusTrafficSource.WB, priority=None):
        response = await super().send(cmd, source, priority)
        if not isinstance(cmd, Compare):
            return response
        if not self.faults.answer(self._search(), response.value is True):
            return MockResponse()
        return response


class TestSearchSurvivesLostCompareAnswers(unittest.IsolatedAsyncioTestCase):
    async def test_lost_terminating_compare_does_not_end_the_search(self):
        """The answer to the first COMPARE is lost — the one whose silence means "no devices
        left". The repeat gets through, so the pass goes on and finds the device."""
        probe = LossySearchProbe([DEVICE_ADDR], CompareFaults(lost=[Ask(TOP)]))
        finder = BinarySearchAddressFinder(probe)

        with self.assertLogs("commissioning", "WARNING") as logs:
            found = await finder.find_next_device(0)

        self.assertEqual(found, DEVICE_ADDR)

        self.assertTrue(probe.faults.all_injected())
        self.assertTrue(
            any(
                f"COMPARE at 0x{TOP:06x} answered only on attempt 2 of " f"{SILENT_COMPARE_ATTEMPTS}" in line
                for line in logs.output
            ),
            logs.output,
        )

    async def test_lost_compare_in_the_middle_is_caught(self):
        """A lost answer at the first midpoint makes the bisection converge on an address
        nobody holds. The device the search walked past answers below it, so the run reports
        it could not confirm the find."""
        probe = LossySearchProbe([DEVICE_ADDR], CompareFaults(lost=[Ask(FIRST_MIDPOINT)]))
        finder = BinarySearchAddressFinder(probe)

        found = await finder.find_next_device(0)

        self.assertEqual(found, BinarySearchAddressFinder.UNCONFIRMED_ADDRESS)
        self.assertIn(FALSE_ADDR, probe.calls)

    async def test_lost_answer_at_the_lower_check_is_confirmed(self):
        """The bisection converged above the device, and the answer to the check below the
        find is lost too. Without a repeat that silence accepts the false address."""
        probe = LossySearchProbe(
            [DEVICE_ADDR], CompareFaults(lost=[Ask(FIRST_MIDPOINT), Ask(FALSE_ADDR - 1, 2)])
        )
        finder = BinarySearchAddressFinder(probe)

        found = await finder.find_next_device(0)

        self.assertEqual(found, BinarySearchAddressFinder.UNCONFIRMED_ADDRESS)
        self.assertTrue(probe.faults.all_injected())

    async def test_search_converged_at_zero_needs_no_lower_check(self):
        """A device at random address 0 leaves nothing to ask below it: no COMPARE at -1."""
        probe = LossySearchProbe([0])
        finder = BinarySearchAddressFinder(probe)

        found = await finder.find_next_device(0)

        self.assertEqual(found, 0)
        self.assertNotIn(-1, probe.calls)


class TestCommissioningSurvivesLostCompareAnswers(unittest.IsolatedAsyncioTestCase):
    async def test_no_program_short_address_when_the_check_fails(self):
        """A search that converged on an address nobody holds programs nothing there; the
        gear gets its short address from the next run."""
        gear = FakeDevice(short=None, random=DEVICE_ADDR)
        bus = LossyPerDeviceBus([gear], CompareFaults(lost=[Ask(FIRST_MIDPOINT)]))

        commissioning = Commissioning(bus, [])
        await commissioning.smart_extend()

        self.assertEqual(commissioning.found_devices, {0: DEVICE_ADDR})
        self.assertEqual(gear.short, 0)
        self.assertEqual(sum(isinstance(cmd, ProgramShortAddress) for cmd in bus.sent_commands), 1)
        # Without the skip the pass would query and withdraw at the false find
        self.assertEqual(sum(isinstance(cmd, QueryShortAddress) for cmd in bus.sent_commands), 1)

    async def test_unconfirmed_result_aborts_and_keeps_config(self):
        """A bus that loses the first answer at every address: no run confirms its find, so
        commissioning fails and leaves the caller no result to write into the config."""
        gear = FakeDevice(short=None, random=DEVICE_ADDR)
        bus = LossyPerDeviceBus([gear], CompareFaults(lose_first_ask=True))

        commissioning = Commissioning(bus, [])
        with self.assertRaisesRegex(RuntimeError, "Binary search stuck"):
            await commissioning.smart_extend()

        self.assertEqual(commissioning.found_devices, {})
        self.assertIsNone(gear.short)
        # Devices left in initialisation state block the next pass for 15 minutes
        self.assertIsInstance(bus.sent_commands[-1], Terminate)
