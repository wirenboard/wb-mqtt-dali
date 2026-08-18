import logging
import unittest
from dataclasses import dataclass
from typing import Optional

from dali.command import Response
from dali.device import general as control_device
from dali.frame import BackwardFrame, BackwardFrameError
from dali.gear import general as control_gear
from dali.sequences import sleep as seq_sleep

from wb.mqtt_dali.dali2_compat import Dali2CommandsCompatibilityLayer
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.short_address import set_short_address_sequence
from wb.mqtt_dali.wbdali_error_response import NoResponseFromGateway
from wb.mqtt_dali.wbdali_utils import FLASH_WRITE_TIME_S, MASK

RANDOM_ADDRESS = 0x2F095F
OTHER_RANDOM_ADDRESS = 0x111111


def short_address_answer(general, short: int) -> int:
    """The backward-frame byte QUERY SHORT ADDRESS would carry for `short`: control gear
    answers `(addr<<1)|1`, a control device the plain value, MASK is 0xFF on both."""
    if short == MASK:
        return MASK
    return (short << 1) | 1 if general is control_gear else short


def programmed_value(cmd) -> Optional[int]:
    """The short address a PROGRAM SHORT ADDRESS command carries: the gear command keeps it
    in `address` (an int or the string "MASK"), the control-device one in `param`."""
    if hasattr(cmd, "address"):
        return MASK if cmd.address == "MASK" else cmd.address
    return getattr(cmd, "param", None)


def search_address_bytes(bus, cmds) -> list[int]:
    return [
        cmd.frame[7:0]
        for cmd in bus.commands_of(cmds.SetSearchAddrH, cmds.SetSearchAddrM, cmds.SetSearchAddrL)
    ]


@dataclass
class Faults:
    """What the fake bus does wrong, from the given command class onwards. `garbled` answers carry
    a framing error, as two devices answering at once do; the counts limit it to that many frames."""

    rejects_from: Optional[type] = None
    rejects_count: Optional[int] = None
    garbled: bool = False
    garbled_count: Optional[int] = None


class Bus:
    """Answers the sequence's queries and records what it yielded.

    The read lists hold one answer per read of that query (`None` — nothing answered), the last
    entry repeating once the list runs out. All three bytes of one random-address read come from
    a single entry."""

    def __init__(
        self,
        general,
        short_address_reads: list[Optional[int]],
        random_address_reads=(RANDOM_ADDRESS,),
        faults=None,
    ):
        self.general = general
        self.short_address_reads = list(short_address_reads)
        self.random_address_reads = list(random_address_reads)
        self.faults = faults or Faults()
        self.gateway_down = False
        self.sent: list = []
        self.delays: list[float] = []

    def run(self, seq):
        """Drive `seq` the way WBDALIDriver.run_sequence does, and return what it
        returned."""
        to_send = None
        while True:
            try:
                item = seq.send(to_send)
            except StopIteration as stop:
                return stop.value
            if isinstance(item, seq_sleep):
                self.delays.append(item.delay)
                to_send = None
            elif isinstance(item, list):
                self.sent.extend(item)
                to_send = [self._answer_to(cmd) for cmd in item]
            else:
                self.sent.append(item)
                to_send = self._answer_to(item)

    def commands_of(self, *command_classes) -> list:
        return [cmd for cmd in self.sent if isinstance(cmd, command_classes)]

    def _answer_to(self, cmd):
        if self.faults.rejects_from is not None and isinstance(cmd, self.faults.rejects_from):
            self.gateway_down = True
        if self.gateway_down:
            if self.faults.rejects_count is None:
                return NoResponseFromGateway()
            if self.faults.rejects_count > 0:
                self.faults.rejects_count -= 1
                return NoResponseFromGateway()
            self.gateway_down = False
        return self._frame(cmd, self._byte_for(cmd))

    def _byte_for(self, cmd) -> Optional[int]:
        general = self.general
        random_address_shifts = {
            general.QueryRandomAddressH: 16,
            general.QueryRandomAddressM: 8,
            general.QueryRandomAddressL: 0,
        }
        for command_class, shift in random_address_shifts.items():
            if isinstance(cmd, command_class):
                address = self._answer_of_read(self.random_address_reads, general.QueryRandomAddressH)
                return None if address is None else address >> shift
        if isinstance(cmd, general.QueryShortAddress):
            return self._answer_of_read(self.short_address_reads, general.QueryShortAddress)
        return None

    def _answer_of_read(self, reads: list[Optional[int]], counted_class) -> Optional[int]:
        """The entry for the read in progress, counted by the queries of `counted_class` already
        sent. Refused frames count too; no test mixes a refusal with a multi-entry list."""
        index = min(len(self.commands_of(counted_class)) - 1, len(reads) - 1)
        return reads[index]

    def _frame(self, cmd, byte: Optional[int]):
        response_class = getattr(type(cmd), "response", None) or Response
        if byte is None:
            return response_class(None)
        frame_class = BackwardFrame
        if self.faults.garbled:
            if self.faults.garbled_count is None:
                frame_class = BackwardFrameError
            elif self.faults.garbled_count > 0:
                self.faults.garbled_count -= 1
                frame_class = BackwardFrameError
        return response_class(frame_class(byte & 0xFF))


class TestSetShortAddressSequence(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test-short-address")

    def test_programs_via_initialise(self):
        """PROGRAM SHORT ADDRESS inside INITIALISE, search address set to the device's random one;
        SET SHORT ADDRESS is not used and the address the device reports comes back."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [short_address_answer(control_gear, 7)])

        reported = bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertEqual(reported, 7)
        self.assertEqual(len(bus.commands_of(control_gear.Initialise)), 1)
        self.assertEqual(
            [programmed_value(c) for c in bus.commands_of(control_gear.ProgramShortAddress)], [7]
        )
        self.assertEqual(bus.commands_of(control_gear.SetShortAddress), [])
        self.assertEqual(search_address_bytes(bus, cmds), [0x2F, 0x09, 0x5F])
        # The check waits out the EEPROM write, or a device mid-write reads as "did not take".
        self.assertEqual(bus.delays, [FLASH_WRITE_TIME_S])
        self.assertIsInstance(bus.sent[-1], control_gear.Terminate)

    def test_clears_with_mask(self):
        """Clearing is the same path with MASK as the target, confirmed by QUERY SHORT ADDRESS."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [MASK])

        reported = bus.run(set_short_address_sequence(cmds, 10, MASK, self.logger))

        self.assertEqual(reported, MASK)
        self.assertEqual(
            [programmed_value(c) for c in bus.commands_of(control_gear.ProgramShortAddress)], [MASK]
        )

    def test_reports_the_old_address_when_the_write_is_ignored(self):
        """The device keeps reporting its old address: the caller is handed that, not an
        exception, the way every other device parameter reports back what was read."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [short_address_answer(control_gear, 5)])

        reported = bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertEqual(reported, 5)
        self.assertIsInstance(bus.sent[-1], control_gear.Terminate)

    def test_reports_nothing_when_nobody_answers(self):
        """Nobody answers QUERY SHORT ADDRESS: no confirmed address to report, TERMINATE still
        goes out."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [None])

        reported = bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertIsNone(reported)
        self.assertIsInstance(bus.sent[-1], control_gear.Terminate)

    def test_writes_nothing_when_the_random_address_is_unreadable(self):
        """No random address to select the device with, so nothing is written: the unverifiable
        SET SHORT ADDRESS is what such devices ignore, and reporting it as done is the bug."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [None], random_address_reads=[None])

        with self.assertLogs(self.logger, level=logging.WARNING) as logs:
            reported = bus.run(set_short_address_sequence(cmds, 5, MASK, self.logger))

        self.assertIsNone(reported)
        self.assertEqual(bus.commands_of(control_gear.SetShortAddress), [])
        self.assertEqual(bus.commands_of(control_gear.Initialise), [])
        self.assertEqual(bus.commands_of(control_gear.ProgramShortAddress), [])
        self.assertEqual(bus.delays, [])
        self.assertTrue(any("no usable random address" in message for message in logs.output))

    def test_rereads_the_random_address_until_two_answers_agree(self):
        """One read returns a different random address — a corrupted byte decodes into a
        valid-looking one. The value two reads agree on becomes the search address."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(
            control_gear,
            [short_address_answer(control_gear, 7)],
            random_address_reads=[OTHER_RANDOM_ADDRESS, RANDOM_ADDRESS, RANDOM_ADDRESS],
        )

        reported = bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertEqual(reported, 7)
        self.assertEqual(len(bus.commands_of(control_gear.QueryRandomAddressH)), 3)
        self.assertEqual(search_address_bytes(bus, cmds), [0x2F, 0x09, 0x5F])

    def test_writes_nothing_when_the_random_address_never_reads_the_same_twice(self):
        """Three reads, three random addresses: programming on a guess would hit somebody else
        or nobody, so nothing is written."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [None], random_address_reads=[0x111111, 0x222222, 0x333333])

        with self.assertLogs(self.logger, level=logging.WARNING):
            reported = bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertIsNone(reported)
        self.assertEqual(bus.commands_of(control_gear.Initialise), [])
        self.assertEqual(bus.commands_of(control_gear.ProgramShortAddress), [])

    def test_confirms_the_write_when_two_short_address_reads_agree(self):
        """The first confirming answer is a corrupted byte carrying another address: two agreeing
        reads decide, so a successful write is not reported as refused."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(
            control_gear,
            [
                short_address_answer(control_gear, 3),
                short_address_answer(control_gear, 7),
                short_address_answer(control_gear, 7),
            ],
        )

        reported = bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertEqual(reported, 7)
        self.assertEqual(len(bus.commands_of(control_gear.QueryShortAddress)), 3)

    def test_works_for_dali2_layer(self):
        """The same sequence on control devices, where QUERY SHORT ADDRESS is encoded differently."""
        cmds = Dali2CommandsCompatibilityLayer()
        bus = Bus(control_device, [short_address_answer(control_device, MASK)])

        reported = bus.run(set_short_address_sequence(cmds, 10, MASK, self.logger))

        self.assertEqual(reported, MASK)
        self.assertEqual(
            [programmed_value(c) for c in bus.commands_of(control_device.ProgramShortAddress)], [MASK]
        )
        self.assertEqual(bus.commands_of(control_device.SetShortAddress), [])
        self.assertIsInstance(bus.sent[-1], control_device.Terminate)

    def test_raises_when_the_gateway_stops_taking_frames(self):
        """The gateway drops out midway: a transport failure, not a device refusing to be
        reprogrammed."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [None], faults=Faults(rejects_from=control_gear.Initialise))

        with self.assertRaises(RuntimeError) as ctx:
            bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertIn("Gateway did not accept", str(ctx.exception))
        self.assertEqual(bus.commands_of(control_gear.ProgramShortAddress), [])

    def test_does_not_write_when_the_gateway_is_down(self):
        """A gateway answering nothing must not pass for a device with an unreadable random
        address — that would write to a bus we cannot read at all."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [None], faults=Faults(rejects_from=control_gear.QueryRandomAddressH))

        with self.assertRaises(RuntimeError) as ctx:
            bus.run(set_short_address_sequence(cmds, 5, MASK, self.logger))

        self.assertIn("Gateway did not accept", str(ctx.exception))
        self.assertEqual(bus.commands_of(control_gear.Initialise), [])

    def test_terminates_when_the_gateway_fails_inside_initialise(self):
        """A frame rejected inside the session must not leave devices in initialise state: they
        stay open to any master on the bus for 15 minutes."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [None], faults=Faults(rejects_from=cmds.SetSearchAddrH))

        with self.assertRaises(RuntimeError):
            bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertEqual(len(bus.commands_of(control_gear.Initialise)), 1)
        self.assertIsInstance(bus.sent[-1], control_gear.Terminate)

    def test_retries_a_rejected_frame_and_succeeds(self):
        """One rejected frame is a lost frame, not a dead bus: the batch is re-sent and the
        write still completes and confirms."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(
            control_gear,
            [short_address_answer(control_gear, 7)],
            faults=Faults(rejects_from=cmds.SetSearchAddrH, rejects_count=1),
        )

        reported = bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertEqual(reported, 7)
        self.assertEqual(len(bus.commands_of(control_gear.ProgramShortAddress)), 2)
        self.assertIsInstance(bus.sent[-1], control_gear.Terminate)

    def test_retries_an_unanswered_query_and_succeeds(self):
        """A refused QUERY SHORT ADDRESS is not a read of the device: it is re-sent, and the two
        answers that arrive confirm the write."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(
            control_gear,
            [short_address_answer(control_gear, 7)],
            faults=Faults(rejects_from=control_gear.QueryShortAddress, rejects_count=1),
        )

        reported = bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertEqual(reported, 7)
        self.assertEqual(len(bus.commands_of(control_gear.QueryShortAddress)), 3)

    def test_programs_a_device_whose_random_address_is_mask(self):
        """After the Reset that reset_device sends first, a compliant device carries the MASK
        random address — a real value that selects it, not an unreadable one."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [MASK], random_address_reads=[0xFFFFFF])

        with self.assertNoLogs(self.logger, level=logging.WARNING):
            reported = bus.run(set_short_address_sequence(cmds, 5, MASK, self.logger))

        self.assertEqual(reported, MASK)
        self.assertEqual(search_address_bytes(bus, cmds), [MASK, MASK, MASK])
        self.assertEqual(
            [programmed_value(c) for c in bus.commands_of(control_gear.ProgramShortAddress)], [MASK]
        )
        self.assertEqual(bus.commands_of(control_gear.SetShortAddress), [])

    def test_raises_instead_of_writing_on_an_unreadable_answer(self):
        """Framing errors on every read mean a device is there but unreadable — typically two on
        one address. Reported as "nobody answered", it would look like a free address."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(control_gear, [None], faults=Faults(garbled=True))

        with self.assertRaises(RuntimeError) as ctx:
            bus.run(set_short_address_sequence(cmds, 5, MASK, self.logger))

        self.assertIn("Unreadable answer", str(ctx.exception))
        self.assertEqual(bus.commands_of(control_gear.SetShortAddress), [])
        self.assertEqual(bus.commands_of(control_gear.Initialise), [])

    def test_retries_a_garbled_random_address_read_and_succeeds(self):
        """A single framing error is a corrupted frame, not two devices on one address: the reads
        that follow agree and the write goes the normal way."""
        cmds = DaliCommandsCompatibilityLayer()
        bus = Bus(
            control_gear,
            [short_address_answer(control_gear, 7)],
            faults=Faults(garbled=True, garbled_count=1),
        )

        reported = bus.run(set_short_address_sequence(cmds, 5, 7, self.logger))

        self.assertEqual(reported, 7)
        self.assertEqual(len(bus.commands_of(control_gear.QueryRandomAddressH)), 3)
        self.assertEqual(search_address_bytes(bus, cmds), [0x2F, 0x09, 0x5F])
        self.assertEqual(bus.commands_of(control_gear.SetShortAddress), [])
