import asyncio
import unittest
from typing import Optional

from dali.address import DeviceShort, GearShort
from dali.frame import ForwardFrame
from dali.gear import general as gear_general
from dali.memory import info

from e2e.emulator.dali_fakes import (
    HELVAR_311_E2,
    ControlDevice,
    DaliBus,
    EmulatedGear,
    Helvar311Gear,
    Helvar312Gear,
    Helvar321Gear,
    make_gear,
)
from e2e.emulator.installation_profile import (
    HELVAR_312_GTIN,
    HELVAR_321_GTIN,
    GearModel,
    GearSpec,
    HelvarFirmware,
    profile_from_configs,
)
from e2e.emulator.template_device import RegisterKind
from e2e.emulator.test_dali_fakes import answer, run_sequence
from e2e.emulator.test_installation_profile import SERIAL_CONFIG
from e2e.emulator.test_wbdali_gateway import (
    EMPTY_TEMPLATE,
    ZERO_TIMING,
    write_slots_request,
)
from e2e.emulator.wbdali_gateway import (
    DRIVER,
    REPLIES_REGISTER,
    EmulatedWbDali,
    decode_slot,
    reply_value,
)
from wb.mqtt_dali.common_dali_device import read_memory_bank
from wb.mqtt_dali.dali2_compat import Dali2CommandsCompatibilityLayer
from wb.mqtt_dali.wbdali import FramePriority, GatewayReplyStatus

HELVAR_SHORT = 6
HELVAR_RANDOM = 0x15DBB4
PLAIN_SHORT = 7
HELVAR_ADDRESS_BYTE = (HELVAR_SHORT << 1) | 1
ENABLE_WRITE_E2 = 0x84
READ_E2 = 0x9A
QUERY_MAJOR_VERSION = 0xE5
QUERY_MINOR_VERSION = 0xE6
WRITE_E2 = 0xC3
EXTENDED_DTR_MSB = 0xC5
FF17_SIZE_CODE = 3
GATEWAY_REPLY_WAIT_S = 1.0
GATEWAY_REPLY_POLL_S = 0.001


def ff17(bus: DaliBus, address_byte: int, data: int) -> Optional[int]:
    """Backward frame of a 17-bit frame whose 17th bit is 0."""
    return bus.transact(ForwardFrame(17, ((address_byte << 8) | data) << 1)).backward


def helvar_spec(model: GearModel, firmware: Optional[HelvarFirmware] = None) -> GearSpec:
    return GearSpec(HELVAR_SHORT, HELVAR_RANDOM, model, frozenset(), helvar_firmware=firmware)


class HelvarE2Test(unittest.TestCase):
    def setUp(self):
        self.sensor = Helvar311Gear(helvar_spec(GearModel.HELVAR_311))
        self.bus = DaliBus([self.sensor])

    def read_e2(self, start: int, count: int) -> list[Optional[int]]:
        answer(self.bus, gear_general.DTR0(start))
        ff17(self.bus, EXTENDED_DTR_MSB, 0)
        return [ff17(self.bus, HELVAR_ADDRESS_BYTE, READ_E2) for _ in range(count)]

    def test_version_is_6_08_by_default(self):
        self.assertEqual(ff17(self.bus, HELVAR_ADDRESS_BYTE, QUERY_MAJOR_VERSION), 6)
        self.assertEqual(ff17(self.bus, HELVAR_ADDRESS_BYTE, QUERY_MINOR_VERSION), 8)

    def test_read_e2_from_dtr_advances_the_address(self):
        """DTR = 0x20, then four Read E2: bytes 0x20-0x23 of the live 311 image."""
        self.assertEqual(self.read_e2(0x20, 4), list(HELVAR_311_E2[0x20:0x24]))

    def test_gtin_and_serial_read_from_e2(self):
        """DTR = 0 and ten Read E2: the 311 GTIN as BCD, then the random address as the serial."""
        self.assertEqual(self.read_e2(0, 10), [0x50, 0x30, 0x79, 0x70, 0x03, 0x11, 0x80, 0x15, 0xDB, 0xB4])

    def test_enable_write_e2_allows_one_write(self):
        """DTR = 0x21, Enable Write E2 and two Write E2: only the first lands, at 0x21."""
        answer(self.bus, gear_general.DTR0(0x21))
        ff17(self.bus, HELVAR_ADDRESS_BYTE, ENABLE_WRITE_E2)
        ff17(self.bus, WRITE_E2, 0x3C)
        ff17(self.bus, WRITE_E2, 0x55)
        expected = bytearray(HELVAR_311_E2)
        expected[0x21] = 0x3C
        self.assertEqual(self.sensor.e2, bytes(expected))

    def test_consecutive_writes_advance_the_address(self):
        """Two Enable Write E2 + Write E2 pairs after one DTR write two neighbouring bytes."""
        answer(self.bus, gear_general.DTR0(0x21))
        for value in (0x3C, 0x06):
            ff17(self.bus, HELVAR_ADDRESS_BYTE, ENABLE_WRITE_E2)
            ff17(self.bus, WRITE_E2, value)
        self.assertEqual(self.sensor.e2[0x21:0x23], bytes([0x3C, 0x06]))

    def test_write_without_enable_is_ignored(self):
        """Write E2 with no Enable Write E2 and one after a DTR0 in between: E2 unchanged."""
        before = self.sensor.e2
        answer(self.bus, gear_general.DTR0(0x21))
        ff17(self.bus, WRITE_E2, 0x3C)
        ff17(self.bus, HELVAR_ADDRESS_BYTE, ENABLE_WRITE_E2)
        answer(self.bus, gear_general.DTR0(0x21))
        ff17(self.bus, WRITE_E2, 0x3C)
        self.assertEqual(self.sensor.e2, before)

    def test_other_short_address_is_ignored(self):
        """Read E2 and the version queries on another short address go unanswered."""
        other = ((HELVAR_SHORT + 1) << 1) | 1
        for opcode in (READ_E2, QUERY_MAJOR_VERSION, QUERY_MINOR_VERSION):
            with self.subTest(opcode=hex(opcode)):
                self.assertIsNone(ff17(self.bus, other, opcode))


class HelvarModelsTest(unittest.TestCase):
    def test_profile_firmware_answers_the_version_queries(self):
        bus = DaliBus([make_gear(helvar_spec(GearModel.HELVAR_311, HelvarFirmware(5, 12)))])
        self.assertEqual(ff17(bus, HELVAR_ADDRESS_BYTE, QUERY_MAJOR_VERSION), 5)
        self.assertEqual(ff17(bus, HELVAR_ADDRESS_BYTE, QUERY_MINOR_VERSION), 12)

    def test_321_gear_has_its_gtin_firmware_and_factory_settings(self):
        """Helvar 321 gear: version 10.41, E2 GTIN 5030797003217 as BCD, 32x factory E2 0x20-0x2F."""
        sensor = make_gear(helvar_spec(GearModel.HELVAR_321))
        self.assertIsInstance(sensor, Helvar321Gear)
        bus = DaliBus([sensor])
        self.assertEqual(ff17(bus, HELVAR_ADDRESS_BYTE, QUERY_MAJOR_VERSION), 10)
        self.assertEqual(ff17(bus, HELVAR_ADDRESS_BYTE, QUERY_MINOR_VERSION), 41)
        self.assertEqual(sensor.e2[0:7], bytes.fromhex("50 30 79 70 03 21 70"))
        self.assertEqual(
            sensor.e2[0x20:0x30], bytes.fromhex("09 3c 06 82 01 fb fb 00 08 04 08 00 00 00 ff ff")
        )

    def test_312_gear_answers_with_its_gtin(self):
        """Helvar 312 gear: version 6.47, E2 GTIN 5030797003125 as BCD, the rest as the 311."""
        sensor = make_gear(helvar_spec(GearModel.HELVAR_312))
        self.assertIsInstance(sensor, Helvar312Gear)
        bus = DaliBus([sensor])
        self.assertEqual(ff17(bus, HELVAR_ADDRESS_BYTE, QUERY_MAJOR_VERSION), 6)
        self.assertEqual(ff17(bus, HELVAR_ADDRESS_BYTE, QUERY_MINOR_VERSION), 47)
        self.assertEqual(sensor.e2[0:7], bytes.fromhex(f"{HELVAR_312_GTIN:013d}0"))
        self.assertEqual(sensor.e2[0x0A:], HELVAR_311_E2[0x0A:])

    def test_ordinary_gear_ignores_17_bit_frames(self):
        """17-bit frames that read as DTR0 and DAPC in 16 bits: no answer, the gear's DTR0 and level
        stay as they were."""
        gear = EmulatedGear(GearSpec(PLAIN_SHORT, 0x111111, GearModel.PLAIN, frozenset()))
        bus = DaliBus([gear])
        answer(bus, gear_general.DTR0(0x11))
        before = (gear.dtr0, gear.level)
        self.assertIsNone(ff17(bus, 0xA3, 0x42))
        self.assertIsNone(ff17(bus, PLAIN_SHORT << 1, 0x80))
        self.assertIsNone(ff17(bus, (PLAIN_SHORT << 1) | 1, READ_E2))
        self.assertEqual((gear.dtr0, gear.level), before)
        self.assertEqual(answer(bus, gear_general.QueryContentDTR0(GearShort(PLAIN_SHORT))), 0x11)

    def test_321_control_device_reads_its_gtin_from_bank_0(self):
        """The Helvar 321 control device from a profile: wb-mqtt-dali's bank reader gets GTIN
        5030797003217, the device has instances of types 3 and 4."""
        dali_config = {
            "gateways": [
                {
                    "device_id": "wb-dali_5",
                    "buses": [
                        {
                            "devices": [
                                {"short": HELVAR_SHORT, "random": HELVAR_RANDOM},
                                {"short": 10, "random": HELVAR_RANDOM, "dali2": True},
                            ]
                        }
                    ],
                }
            ]
        }
        state = {
            "1": {
                "gear": {str(HELVAR_SHORT): {"model": "Helvar 321"}},
                "control_devices": {"10": {"model": "Helvar 321"}},
            }
        }
        spec = profile_from_configs(SERIAL_CONFIG, dali_config, state).buses[0].control_devices[0]
        self.assertEqual(spec.instance_types, (3, 4))
        bus = DaliBus([ControlDevice(spec)])
        values = dict(
            run_sequence(
                bus,
                read_memory_bank(
                    info.BANK_0, DeviceShort(10), Dali2CommandsCompatibilityLayer(), [info.GTIN]
                ),
            )
        )
        self.assertEqual(values[info.GTIN], HELVAR_321_GTIN)


class HelvarGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_size_code_3_slot_sends_a_17_bit_frame(self):
        """A slot with frame size 3 written over Modbus: a 17-bit frame reaches the Helvar sensor
        and its version answer lands in the reply register."""
        bus = DaliBus(
            [
                Helvar311Gear(helvar_spec(GearModel.HELVAR_311)),
                EmulatedGear(GearSpec(PLAIN_SHORT, 0x111111, GearModel.PLAIN, frozenset())),
            ]
        )
        gateway = EmulatedWbDali(7, EMPTY_TEMPLATE, ZERO_TIMING, [bus], "1.3.0")
        task = asyncio.create_task(gateway.run())
        self.addCleanup(task.cancel)
        word = (HELVAR_ADDRESS_BYTE << 8) | QUERY_MAJOR_VERSION
        raw = (FramePriority.USER_ACTION.value << 29) | (FF17_SIZE_CODE << 25) | (word << 1)
        slot = decode_slot(raw)
        self.assertEqual((len(slot.frame), slot.frame.as_integer), (17, word << 1))
        gateway.handle_pdu(write_slots_request(DRIVER.queue_start_modbus_address, [raw]))
        deadline = asyncio.get_running_loop().time() + GATEWAY_REPLY_WAIT_S
        while gateway.get(RegisterKind.INPUT, REPLIES_REGISTER) == 0:
            self.assertLess(asyncio.get_running_loop().time(), deadline)
            await asyncio.sleep(GATEWAY_REPLY_POLL_S)
        self.assertEqual(
            gateway.get(RegisterKind.INPUT, REPLIES_REGISTER),
            reply_value(GatewayReplyStatus.BACKWARD_FRAME, 6),
        )


class HelvarProfileTest(unittest.TestCase):
    def profile_gear(self, gear_state: dict) -> GearSpec:
        dali_config = {
            "gateways": [
                {
                    "device_id": "wb-dali_5",
                    "buses": [{"devices": [{"short": HELVAR_SHORT, "random": HELVAR_RANDOM}]}],
                }
            ]
        }
        state = {"1": {"gear": {str(HELVAR_SHORT): gear_state}}}
        return profile_from_configs(SERIAL_CONFIG, dali_config, state).buses[0].gear[0]

    def test_firmware_is_major_dot_two_minor_digits(self):
        spec = self.profile_gear({"model": "Helvar 311", "firmware": "5.07"})
        self.assertEqual(spec.helvar_firmware, HelvarFirmware(5, 7))

    def test_without_firmware_the_model_default_applies(self):
        self.assertIsNone(self.profile_gear({"model": "Helvar 321"}).helvar_firmware)

    def test_bad_firmware_is_rejected(self):
        """Firmware not like "6.08", or on a non-Helvar model: an error at load."""
        for gear_state in (
            {"model": "Helvar 311", "firmware": "6.8"},
            {"model": "Helvar 311", "firmware": "256.00"},
            {"model": "Helvar 311", "firmware": 6.08},
            {"model": "default_gear", "firmware": "6.08"},
        ):
            with self.subTest(gear_state=gear_state), self.assertRaises(ValueError):
                self.profile_gear(gear_state)

    def test_unknown_control_device_model_is_rejected(self):
        dali_config = {
            "gateways": [
                {"device_id": "wb-dali_5", "buses": [{"devices": [{"short": 1, "random": 2, "dali2": True}]}]}
            ]
        }
        state = {"1": {"control_devices": {"1": {"model": "Helvar 320"}}}}
        with self.assertRaises(ValueError):
            profile_from_configs(SERIAL_CONFIG, dali_config, state)
