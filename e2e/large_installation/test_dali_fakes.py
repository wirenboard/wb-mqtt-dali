import logging
import unittest

from dali.address import DeviceShort, GearShort, InstanceNumber
from dali.command import Command, Response
from dali.device import general as device_general
from dali.device import light, pushbutton
from dali.frame import BackwardFrame, BackwardFrameError
from dali.gear import general as gear_general
from dali.gear import led
from dali.memory import diagnostics, energy, maintenance, oem

from e2e.large_installation.dali_fakes import (
    ENERGY_WH,
    ControlDevice,
    DaliBus,
    EmulatedGear,
)
from e2e.large_installation.installation_profile import (
    KNOWN_GEAR_PRODUCTS,
    ControlDeviceSpec,
    GearModel,
    GearSpec,
)
from wb.mqtt_dali.common_dali_device import read_memory_bank
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.dali_device import query_device_types_sequence

OTI = KNOWN_GEAR_PRODUCTS["OTI DALI 15/220-240/1A0 NFC"]


def respond(bus: DaliBus, cmd: Command) -> Response:
    if cmd.devicetype:
        bus.transact(gear_general.EnableDeviceType(cmd.devicetype).frame)
    reply = bus.transact(cmd.frame)
    if reply.backward is None:
        backward = None
    else:
        backward = BackwardFrameError(reply.backward) if reply.collision else BackwardFrame(reply.backward)
    return cmd.response(backward) if cmd.response else Response(backward)


def run_sequence(bus: DaliBus, sequence):
    """Runs a wb-mqtt-dali command sequence on the fake bus the way the driver does."""
    response = None
    command = next(sequence)
    while True:
        if isinstance(command, list):
            response = [respond(bus, c) for c in command]
        elif isinstance(command, Command):
            response = respond(bus, command)
        try:
            command = sequence.send(response)
        except StopIteration as result:
            return result.value


class FakesTest(unittest.TestCase):
    def setUp(self):
        self.bus = DaliBus(
            [
                EmulatedGear(GearSpec(3, 0x0A0B0C, GearModel.ENERGY, frozenset({1, 9}), OTI.gtin)),
                EmulatedGear(GearSpec(4, 0x0A0B0D, GearModel.PLAIN, frozenset())),
                ControlDevice(
                    ControlDeviceSpec(3, 0x112233, (pushbutton.instance_type, light.instance_type))
                ),
            ]
        )

    def answer(self, cmd):
        return self.bus.transact(cmd.frame).backward

    def read_bank(self, short: int, bank: int, start: int, count: int) -> list:
        self.answer(gear_general.DTR1(bank))
        self.answer(gear_general.DTR0(start))
        return [self.answer(gear_general.ReadMemoryLocation(GearShort(short))) for _ in range(count)]

    def test_gear_answers_the_init_and_poll_queries(self):
        """Energy and plain gear answer device type, groups, status and fade.
        An absent address stays silent."""
        self.assertEqual(self.answer(gear_general.QueryDeviceType(GearShort(3))), 0xFF)
        self.assertEqual(self.answer(gear_general.QueryDeviceType(GearShort(4))), 254)
        self.assertEqual(self.answer(gear_general.QueryGroupsEightToFifteen(GearShort(3))), 0b10)
        self.assertEqual(self.answer(gear_general.QueryStatus(GearShort(3))), 0)
        self.assertIsNotNone(self.answer(gear_general.QueryFadeTimeFadeRate(GearShort(3))))
        self.assertIsNone(self.answer(gear_general.QueryActualLevel(GearShort(5))))

    def test_gtin_and_energy_bank(self):
        """OTI gear carries its GTIN in bank 0 and an energy bank 202; plain gear has neither."""
        self.assertEqual(bytes(self.read_bank(3, 0, 3, 6)), OTI.gtin.to_bytes(6, "big"))
        self.assertEqual(self.read_bank(4, 0, 3, 6), [0xFF] * 6)
        self.assertEqual(self.read_bank(3, 202, 4, 1), [0])
        self.assertEqual(self.read_bank(4, 202, 4, 1), [None])

    def test_control_device_init_queries(self):
        """Instances, their types and features, device groups and a pushbutton timer, as read at init."""
        device = DeviceShort(3)
        self.assertEqual(self.answer(device_general.QueryNumberOfInstances(device)), 2)
        self.assertEqual(self.answer(device_general.QueryInstanceType(device, InstanceNumber(1))), 4)
        self.assertEqual(self.answer(device_general.QueryFeatureType(device, InstanceNumber(0))), 254)
        self.assertEqual(self.answer(device_general.QueryDeviceGroupsZeroToSeven(device)), 0)
        self.assertEqual(self.answer(pushbutton.QueryShortTimer(device, InstanceNumber(0))), 20)
        self.assertIsNone(self.answer(device_general.QueryInstanceType(device, InstanceNumber(2))))

    def test_control_device_is_found_by_its_random_address(self):
        """Initialise, then search addresses below, at and above the device's random address.
        Compare answers at and above it; QueryShortAddress answers only at it."""
        self.answer(device_general.Initialise(0xFF))
        for name, value in zip(("SearchAddrH", "SearchAddrM", "SearchAddrL"), (0x11, 0x22, 0x32)):
            self.answer(getattr(device_general, name)(value))
        self.assertIsNone(self.answer(device_general.Compare()))
        self.answer(device_general.SearchAddrL(0x33))
        self.assertEqual(self.answer(device_general.Compare()), 0xFF)
        self.assertEqual(self.answer(device_general.QueryShortAddress()), 3)
        self.answer(device_general.Withdraw())
        self.assertIsNone(self.answer(device_general.Compare()))


class ControlDeviceAddressingTest(unittest.TestCase):
    def setUp(self):
        self.device = ControlDevice(ControlDeviceSpec(3, 0x112233, (pushbutton.instance_type,)))
        self.bus = DaliBus([self.device])

    def answer(self, cmd):
        return self.bus.transact(cmd.frame).backward

    def select(self, random_address: int) -> None:
        for name, value in zip(
            ("SearchAddrH", "SearchAddrM", "SearchAddrL"), random_address.to_bytes(3, "big")
        ):
            self.answer(getattr(device_general, name)(value))

    def test_initialise_selects_by_short_address(self):
        """Initialise addressed to another short address leaves the device out of the search."""
        self.answer(device_general.Initialise(7))
        self.select(0x112233)
        self.assertIsNone(self.answer(device_general.Compare()))
        self.answer(device_general.Initialise(3))
        self.assertEqual(self.answer(device_general.Compare()), 0xFF)

    def test_program_and_verify_short_address(self):
        """The device found by its random address gets short address 9 and confirms it."""
        self.answer(device_general.Initialise(0xFF))
        self.select(0x112233)
        self.answer(device_general.ProgramShortAddress(9))
        self.assertEqual(self.device.short_address, 9)
        self.assertEqual(self.answer(device_general.VerifyShortAddress(9)), 0xFF)
        self.assertIsNone(self.answer(device_general.VerifyShortAddress(3)))
        self.answer(device_general.Terminate())
        self.assertEqual(self.answer(device_general.QueryNumberOfInstances(DeviceShort(9))), 1)

    def test_program_needs_the_selected_random_address(self):
        """ProgramShortAddress with a search address one off the device's random leaves its address."""
        self.answer(device_general.Initialise(0xFF))
        self.select(0x112234)
        self.answer(device_general.ProgramShortAddress(9))
        self.assertEqual(self.device.short_address, 3)


class EnergyGearTest(unittest.TestCase):
    def setUp(self):
        self.bus = DaliBus([EmulatedGear(GearSpec(7, 0x123456, GearModel.ENERGY, frozenset(), OTI.gtin))])

    def test_device_types_are_read_as_wb_mqtt_dali_reads_them(self):
        """QueryDeviceType answers MASK, QueryNextDeviceType walks 6, 50, 51, 52 and ends with 254."""
        types = run_sequence(self.bus, query_device_types_sequence(GearShort(7), logging.getLogger()))
        self.assertEqual(types, [6, 50, 51, 52])

    def test_every_bank_the_type_handlers_read_is_readable(self):
        """wb-mqtt-dali's own bank reader gets data from every bank the OTI type handlers read."""
        compat = DaliCommandsCompatibilityLayer()
        for bank in (
            oem.BANK_1,
            energy.BANK_202,
            diagnostics.BANK_205,
            diagnostics.BANK_206,
            maintenance.BANK_207,
        ):
            with self.subTest(bank=bank.address):
                values: dict = run_sequence(self.bus, read_memory_bank(bank, GearShort(7), compat))
                self.assertTrue(values)

    def test_energy_value_decodes_from_bank_202(self):
        """Bank 202 read the way wb-mqtt-dali reads it decodes to the fake's active energy."""
        values = dict(
            run_sequence(
                self.bus, read_memory_bank(energy.BANK_202, GearShort(7), DaliCommandsCompatibilityLayer())
            )
        )
        self.assertEqual(values[energy.ActiveEnergy], ENERGY_WH)

    def test_led_queries_follow_enable_device_type(self):
        """DT6 queries answer after EnableDeviceType(6); an absent address stays silent."""
        self.assertEqual(respond(self.bus, led.QueryDimmingCurve(GearShort(7))).raw_value.as_integer, 0)
        self.assertEqual(respond(self.bus, led.QueryFastFadeTime(GearShort(7))).raw_value.as_integer, 0)
        self.assertIsNone(respond(self.bus, gear_general.QueryContentDTR0(GearShort(8))).raw_value)
