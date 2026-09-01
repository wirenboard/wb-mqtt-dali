"""The addressing tool for gear that joins the search only after a blanket RANDOMISE.

The fake bus here models that gear: silent on COMPARE until a RANDOMISE reaches it, keeping a
random address the command does not re-roll, with a short address that survives and can be
programmed.
"""

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from typing import Callable, Optional
from unittest.mock import Mock

from dali.gear.general import (
    Compare,
    Initialise,
    ProgramShortAddress,
    QueryControlGearPresent,
    QueryShortAddress,
    Randomise,
    SetSearchAddrH,
    SetSearchAddrL,
    SetSearchAddrM,
    Terminate,
    VerifyShortAddress,
    Withdraw,
)

from wb.mqtt_dali.address_with_randomise import (
    AddressingOptions,
    ExitCode,
    address_with_randomise,
)
from wb.mqtt_dali.bus_traffic import BusTrafficSource

GATEWAY = "wb-dali_68"
BUS = 1
LOGGER = "address-with-randomise"


class FakeResponse:  # pylint: disable=too-few-public-methods
    """Stand-in for a dali Response: `error=None` means no backframe at all.

    `error_acceptable` mirrors the flag the yes/no responses carry: silence is their negative
    answer, so the query helpers must take it as an answer rather than retry it.
    """

    def __init__(self, value=None, error=None, error_acceptable: bool = False) -> None:
        self.value = value
        self.raw_value = None if error is None else Mock(error=error)
        self._error_acceptable = error_acceptable


@dataclass
class FakeGear:
    """One control gear of the non-conforming kind.

    `joined_search` is the latch RANDOMISE sets: before it the gear answers neither COMPARE nor
    QUERY SHORT ADDRESS, and the command leaves `random` alone, as the sample on the stand does.
    `goes_silent_after_program` is gear whose EEPROM write neither sticks nor is answered.
    """

    random: int
    short: Optional[int] = None
    withdraws: bool = True
    goes_silent_after_program: bool = False
    joined_search: bool = False
    initialised: bool = False
    withdrawn: bool = False


class RandomiseOnlyFakeBus:
    """A bus of `FakeGear` behind the driver interface the tool uses.

    The gear hold distinct random and short addresses, so at most one of them answers any frame
    and answers never collide.
    """

    def __init__(self, gear: list[FakeGear], lose_first_compare_answer: bool = False) -> None:
        self.gear = gear
        self.sent_commands: list = []
        self.lose_first_compare_answer = lose_first_compare_answer
        self._search_addr: list[Optional[int]] = [None, None, None]
        self._handlers: dict[type, Callable] = {
            SetSearchAddrH: lambda cmd: self._set_search_part(0, cmd.param),
            SetSearchAddrM: lambda cmd: self._set_search_part(1, cmd.param),
            SetSearchAddrL: lambda cmd: self._set_search_part(2, cmd.param),
            Terminate: self._terminate,
            Initialise: self._initialise,
            Randomise: self._randomise,
            Withdraw: self._withdraw,
            Compare: self._compare,
            ProgramShortAddress: self._program_short,
            VerifyShortAddress: self._verify_short,
            QueryShortAddress: self._query_short,
            QueryControlGearPresent: self._query_present,
        }

    async def send(self, cmd, source=BusTrafficSource.WB, priority=None):
        del source, priority
        self.sent_commands.append(cmd)
        handler = self._handlers.get(type(cmd))
        return handler(cmd) if handler is not None else FakeResponse()

    async def send_commands(self, cmds, source=BusTrafficSource.WB, priority=None):
        return [await self.send(cmd, source, priority) for cmd in cmds]

    def programmed_shorts(self) -> list[int]:
        """Every short address PROGRAM SHORT ADDRESS put on the bus."""
        return [cmd.address for cmd in self.sent_commands if isinstance(cmd, ProgramShortAddress)]

    # --- Private ---

    def _set_search_part(self, index: int, value: int) -> FakeResponse:
        self._search_addr[index] = value
        return FakeResponse()

    def _terminate(self, _cmd) -> FakeResponse:
        for gear in self.gear:
            gear.initialised = False
            gear.withdrawn = False
        return FakeResponse()

    def _initialise(self, cmd) -> FakeResponse:
        for gear in self.gear:
            if cmd.broadcast or gear.short == cmd.address:
                gear.initialised = True
        return FakeResponse()

    def _randomise(self, _cmd) -> FakeResponse:
        for gear in self.gear:
            if gear.initialised:
                gear.joined_search = True
        return FakeResponse()

    def _withdraw(self, _cmd) -> FakeResponse:
        gear = self._selected()
        if gear is not None and gear.withdraws:
            gear.withdrawn = True
        return FakeResponse()

    def _compare(self, _cmd) -> FakeResponse:
        search = self._search()
        answers = search is not None and any(
            gear.joined_search and gear.initialised and not gear.withdrawn and gear.random <= search
            for gear in self.gear
        )
        if answers and self.lose_first_compare_answer:
            self.lose_first_compare_answer = False
            return FakeResponse(error_acceptable=True)
        return FakeResponse(value=answers, error=False if answers else None, error_acceptable=True)

    def _program_short(self, cmd) -> FakeResponse:
        gear = self._selected()
        if gear is None:
            return FakeResponse()
        if gear.goes_silent_after_program:
            # The write neither sticks nor is answered: the gear falls out of the addressing
            # state machine, so VERIFY and QUERY SHORT ADDRESS both stay silent.
            gear.joined_search = False
        else:
            gear.short = None if cmd.address == "MASK" else cmd.address
        return FakeResponse()

    def _verify_short(self, cmd) -> FakeResponse:
        gear = self._selected()
        if gear is None or not gear.joined_search or gear.short != cmd.address:
            return FakeResponse(error_acceptable=True)
        return FakeResponse(value=True, error=False, error_acceptable=True)

    def _query_short(self, _cmd) -> FakeResponse:
        gear = self._selected()
        if gear is None or not gear.joined_search:
            return FakeResponse()
        return FakeResponse(value="MASK" if gear.short is None else (gear.short << 1) | 1, error=False)

    def _query_present(self, cmd) -> FakeResponse:
        short = cmd.destination.address
        if any(gear.short == short for gear in self.gear):
            return FakeResponse(value=True, error=False, error_acceptable=True)
        return FakeResponse(error_acceptable=True)

    def _selected(self) -> Optional[FakeGear]:
        """The gear the current search address picks out of the initialisation state."""
        search = self._search()
        if search is None:
            return None
        return next((gear for gear in self.gear if gear.initialised and gear.random == search), None)

    def _search(self) -> Optional[int]:
        if None in self._search_addr:
            return None
        return (self._search_addr[0] << 16) | (self._search_addr[1] << 8) | self._search_addr[2]


async def run_tool(bus: RandomiseOnlyFakeBus, config_path: str, search_only: bool = False) -> ExitCode:
    """Run one pass against the configuration at `config_path`."""
    return await address_with_randomise(bus, AddressingOptions(GATEWAY, BUS, config_path, search_only))


def write_config(config_path: str, devices: list[dict]) -> None:
    config = {"gateways": [{"device_id": GATEWAY, "buses": [{"devices": devices}, {"devices": []}]}]}
    with open(config_path, "w", encoding="utf-8") as config_file:
        json.dump(config, config_file)


def read_devices(config_path: str) -> list[dict]:
    with open(config_path, "r", encoding="utf-8") as config_file:
        return json.load(config_file)["gateways"][0]["buses"][0]["devices"]


class ToolTestCase(unittest.IsolatedAsyncioTestCase):
    """Every pass writes the configuration, so each test gets one of its own."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.config_path = os.path.join(directory, "wb-mqtt-dali.conf")
        write_config(self.config_path, [])


class TestAddressingOnTheBus(ToolTestCase):
    async def test_gear_answering_only_after_randomise_is_addressed(self):
        """The gear stays silent on COMPARE until the pass sends RANDOMISE to all devices.
        The pass finds it anyway and leaves it with a short address."""
        gear = FakeGear(random=0x1235CF)
        bus = RandomiseOnlyFakeBus([gear])

        self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)

        self.assertTrue(gear.joined_search)
        self.assertEqual(gear.short, 0)

    async def test_unaddressed_gear_gets_lowest_free_short(self):
        """Short addresses 0 and 2 answer the opening scan, so the gear without one gets 1."""
        unaddressed = FakeGear(random=0x300000)
        bus = RandomiseOnlyFakeBus(
            [FakeGear(random=0x100000, short=0), FakeGear(random=0x200000, short=2), unaddressed]
        )

        self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)

        self.assertEqual(unaddressed.short, 1)
        self.assertEqual(bus.programmed_shorts(), [1])

    async def test_addressed_gear_keeps_its_short(self):
        """Gear that reports a valid short address keeps it and is never written to."""
        gear = FakeGear(random=0x1235CF, short=5)
        bus = RandomiseOnlyFakeBus([gear])

        self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)

        self.assertEqual(gear.short, 5)
        self.assertEqual(bus.programmed_shorts(), [])

    async def test_all_devices_addressed_in_one_pass(self):
        """Three gear with distinct random addresses withdraw as they are found, so one pass
        enumerates all of them; each gets its own short address, lowest random address first."""
        first = FakeGear(random=0x300000)
        second = FakeGear(random=0x100000)
        third = FakeGear(random=0x200000)
        bus = RandomiseOnlyFakeBus([first, second, third])

        self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)

        self.assertEqual((second.short, third.short, first.short), (0, 1, 2))

    async def test_short_of_later_found_device_is_not_handed_out(self):
        """Short address 0 belongs to gear the search reaches second. The pool comes from the
        opening scan, so the first gear found gets 1 and the two do not collide."""
        unaddressed = FakeGear(random=0x100000)
        addressed = FakeGear(random=0x200000, short=0)
        bus = RandomiseOnlyFakeBus([unaddressed, addressed])

        self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)

        self.assertEqual(unaddressed.short, 1)
        self.assertEqual(addressed.short, 0)

    async def test_unconfirmed_program_aborts(self):
        """The first gear answers neither VERIFY SHORT ADDRESS nor QUERY SHORT ADDRESS after the
        write. The pass stops there: the second gear is never programmed and the configuration
        keeps what it had."""
        silent = FakeGear(random=0x100000, goes_silent_after_program=True)
        never_reached = FakeGear(random=0x200000)
        bus = RandomiseOnlyFakeBus([silent, never_reached])

        with self.assertLogs(LOGGER, "ERROR") as logs:
            self.assertEqual(await run_tool(bus, self.config_path), ExitCode.ADDRESSING_ABORTED)

        self.assertEqual(read_devices(self.config_path), [])
        self.assertEqual(bus.programmed_shorts(), [0])
        self.assertIsNone(never_reached.short)
        self.assertTrue(any("Short address 0" in line for line in logs.output), logs.output)

    async def test_search_not_progressing_aborts(self):
        """Gear that ignores WITHDRAW keeps answering COMPARE, so the search finds it over and
        over. The pass gives up instead of looping."""
        bus = RandomiseOnlyFakeBus([FakeGear(random=0x100000, withdraws=False)])

        with self.assertLogs(LOGGER, "ERROR") as logs:
            self.assertEqual(await run_tool(bus, self.config_path), ExitCode.ADDRESSING_ABORTED)

        self.assertTrue(any("0x100000" in line for line in logs.output), logs.output)

    async def test_lost_compare_answer_does_not_break_search(self):
        """The first COMPARE answer is lost. The repeat brings it back, so the pass finds the
        gear that is there instead of concluding the bus is empty."""
        gear = FakeGear(random=0x1235CF)
        bus = RandomiseOnlyFakeBus([gear], lose_first_compare_answer=True)

        self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)

        self.assertFalse(bus.lose_first_compare_answer)
        self.assertEqual(gear.short, 0)


class TestConfigurationAfterThePass(ToolTestCase):
    async def test_config_entries_added_and_names_preserved(self):
        """A gear already in the configuration and a new one. The pass adds an entry for the
        new short address and leaves the name and mqtt_id of the existing one alone."""
        known = FakeGear(random=0x111111, short=5)
        bus = RandomiseOnlyFakeBus([known, FakeGear(random=0x222222)])

        write_config(self.config_path, [{"short": 5, "random": 0x999999, "name": "Amp", "mqtt_id": "amp"}])

        self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)

        self.assertEqual(
            read_devices(self.config_path),
            [
                {"short": 5, "random": 0x111111, "name": "Amp", "mqtt_id": "amp"},
                {"short": 0, "random": 0x222222},
            ],
        )

    async def test_config_entries_not_found_on_bus_are_removed(self):
        """The configuration holds a short address nothing answers at. Its entry goes, its
        address is named in the log, and the pass still succeeds."""
        bus = RandomiseOnlyFakeBus([FakeGear(random=0x111111, short=5)])

        write_config(self.config_path, [{"short": 5, "random": 0x111111}, {"short": 7, "random": 0x777777}])

        with self.assertLogs(LOGGER, "WARNING") as logs:
            self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)

        self.assertEqual(read_devices(self.config_path), [{"short": 5, "random": 0x111111}])
        self.assertTrue(any("not found on the bus" in line and "7" in line for line in logs.output))

    async def test_empty_bus_leaves_the_configuration_alone(self):
        """Nothing on the bus answers the search. The entries stay where they are and the pass
        says so in the log."""
        write_config(self.config_path, [{"short": 5, "random": 0x111111}])

        with self.assertLogs(LOGGER, "WARNING") as logs:
            self.assertEqual(await run_tool(RandomiseOnlyFakeBus([]), self.config_path), ExitCode.SUCCESS)

        self.assertEqual(read_devices(self.config_path), [{"short": 5, "random": 0x111111}])
        self.assertTrue(any("found no gear" in line for line in logs.output))

    async def test_repeated_run_changes_nothing(self):
        """A second pass over an unchanged bus: every gear reports its short address, so nothing
        is written to the bus and the configuration comes out identical."""
        bus = RandomiseOnlyFakeBus([FakeGear(random=0x111111), FakeGear(random=0x222222)])

        self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)
        after_first_run = read_devices(self.config_path)
        bus.sent_commands.clear()

        self.assertEqual(await run_tool(bus, self.config_path), ExitCode.SUCCESS)

        self.assertEqual(read_devices(self.config_path), after_first_run)
        self.assertEqual(bus.programmed_shorts(), [])


class TestSearchOnly(ToolTestCase):
    async def test_gear_is_reported_and_nothing_is_programmed(self):
        """Gear without a short address is found and reported as having none; the pass programs
        nothing and leaves the configuration alone."""
        gear = FakeGear(random=0x1235CF)
        bus = RandomiseOnlyFakeBus([gear])
        write_config(self.config_path, [{"short": 7, "random": 0x300000}])

        with self.assertLogs(LOGGER, "INFO") as logs:
            self.assertEqual(await run_tool(bus, self.config_path, search_only=True), ExitCode.SUCCESS)

        self.assertIsNone(gear.short)
        self.assertEqual(bus.programmed_shorts(), [])
        self.assertEqual(read_devices(self.config_path), [{"short": 7, "random": 0x300000}])
        self.assertTrue(any("0x1235cf, short address none" in line for line in logs.output), logs.output)

    async def test_every_device_is_reported_with_the_short_it_has(self):
        """The search walks past each device as usual, so all of them are reported, and the ones
        holding a short address are reported with it."""
        addressed = FakeGear(random=0x300000, short=5)
        unaddressed = FakeGear(random=0x400000)
        bus = RandomiseOnlyFakeBus([addressed, unaddressed])

        with self.assertLogs(LOGGER, "INFO") as logs:
            self.assertEqual(await run_tool(bus, self.config_path, search_only=True), ExitCode.SUCCESS)

        self.assertEqual((addressed.short, unaddressed.short), (5, None))
        self.assertEqual(bus.programmed_shorts(), [])
        self.assertTrue(any("0x300000, short address 5" in line for line in logs.output), logs.output)
        self.assertTrue(any("0x400000, short address none" in line for line in logs.output), logs.output)
