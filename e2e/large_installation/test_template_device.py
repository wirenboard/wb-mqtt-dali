import json
import shutil
import tempfile
import unittest
from pathlib import Path

from e2e.large_installation.fast_modbus import EventKey, EventType
from e2e.large_installation.template_device import (
    CONTINUOUS_READ_HOLE_VALUE,
    RegisterKind,
    TemplateDevice,
    WbRegister,
    load_installed_template,
    template_from_dict,
)

TEMPLATE = {
    "device_type": "WB-TEST",
    "hw": [{"signature": "WBTEST"}],
    "device": {
        "parameters": {
            "mode": {"address": 10, "reg_type": "holding"},
            "delay": {"address": "0x20"},
        },
        "channels": [
            {"name": "K1", "reg_type": "coil", "address": 0},
            {"name": "Counter", "reg_type": "input", "address": 100, "format": "u32"},
            {"name": "Presses", "reg_type": "press_counter", "address": 464},
            {"name": "Bit", "reg_type": "holding", "address": "40:2:1"},
        ],
        "subdevices": [{"device": {"setup": [{"title": "x", "address": 50, "value": 1}]}}],
    },
}


def read_request(function: int, address: int, count: int) -> bytes:
    return bytes([function]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")


class TemplateRegistersTest(unittest.TestCase):
    def test_every_register_of_the_template_is_built(self):
        """Channels, parameters with and without reg_type, setup and multi-register formats all map."""
        template = template_from_dict(TEMPLATE)
        self.assertEqual(template.signature, "WBTEST")
        self.assertEqual(template.registers[RegisterKind.COIL], frozenset({0}))
        self.assertEqual(template.registers[RegisterKind.INPUT], frozenset({100, 101, 464}))
        self.assertEqual(template.registers[RegisterKind.HOLDING], frozenset({10, 0x20, 40, 50}))


class TemplateDeviceTest(unittest.TestCase):
    def setUp(self):
        self.device = TemplateDevice(20, template_from_dict(TEMPLATE), 0.001, "1.2.3")

    def test_setup_parameter_writes_are_accepted_and_read_back(self):
        """A write to a setup parameter without reg_type is stored and reads back."""
        request = bytes([6]) + (0x20).to_bytes(2, "big") + (7).to_bytes(2, "big")
        self.assertEqual(self.device.handle_pdu(request), request)
        self.assertEqual(self.device.handle_pdu(read_request(3, 0x20, 1)), bytes([3, 2, 0, 7]))

    def test_unknown_register_is_an_illegal_address_without_continuous_read(self):
        self.assertEqual(self.device.handle_pdu(read_request(3, 10, 2)), bytes([0x83, 2]))

    def test_holes_read_as_ffe_with_continuous_read(self):
        """With continuous read on, an unknown register inside a read range comes back as 0xFFFE."""
        self.device.handle_pdu(
            bytes([6]) + WbRegister.CONTINUOUS_READ.to_bytes(2, "big") + (1).to_bytes(2, "big")
        )
        answer = self.device.handle_pdu(read_request(3, 10, 2))
        self.assertEqual(answer[2:], (0).to_bytes(2, "big") + CONTINUOUS_READ_HOLE_VALUE.to_bytes(2, "big"))

    def test_wb_service_registers_carry_version_and_signature(self):
        answer = self.device.handle_pdu(read_request(3, WbRegister.FW_VERSION, 5))
        self.assertEqual(bytes(answer[3::2]), b"1.2.3")
        answer = self.device.handle_pdu(read_request(3, WbRegister.FW_SIGNATURE, 6))
        self.assertEqual(bytes(answer[3::2]), b"WBTEST")

    def test_coil_write_and_read(self):
        """A single coil written on reads back as set."""
        self.device.handle_pdu(bytes([5, 0, 0, 0xFF, 0]))
        self.assertEqual(self.device.handle_pdu(read_request(1, 0, 1)), bytes([1, 1, 1]))


class FastModbusTest(unittest.TestCase):
    def setUp(self):
        self.device = TemplateDevice(20, template_from_dict(TEMPLATE), 0.0)

    def test_enable_answers_one_bit_per_register(self):
        """A record over input 100..102 with a hole at 102 and a disabled priority on 101.
        Only 100 is enabled; the hole and the disabled one answer 0."""
        request = bytes([0x46, 0x18, 7, EventType.INPUT, 0, 100, 3, 2, 0, 1])
        self.assertEqual(self.device.handle_pdu(request), bytes([0x46, 0x18, 1, 0b001]))
        self.assertEqual(set(self.device.events.enabled), {EventKey(EventType.INPUT, 100)})

    def test_change_is_delivered_until_confirmed(self):
        """An enabled register changes, the event is read twice without confirmation, then confirmed.
        The batch repeats under the same flag and disappears after the confirmation."""
        self.device.handle_pdu(bytes([0x46, 0x18, 5, EventType.INPUT, 0, 100, 1, 1]))
        self.device.set(RegisterKind.INPUT, 100, 0x0102)
        first = self.device.events.build_response(100)
        self.assertEqual(first[3:], bytes([1, 6, 2, EventType.INPUT, 0, 100, 0x02, 0x01]))
        self.assertEqual(self.device.events.build_response(100), first)
        self.device.events.confirm(first[2])
        self.assertFalse(self.device.events.has_pending())

    def test_unknown_event_type_is_never_enabled(self):
        """The reboot record (type 15) wb-mqtt-serial appends answers 0 even with a priority."""
        request = bytes([0x46, 0x18, 5, 15, 0, 0, 1, 2])
        self.assertEqual(self.device.handle_pdu(request), bytes([0x46, 0x18, 1, 0]))
        self.assertEqual(self.device.events.enabled, {})


class InstalledTemplateTest(unittest.TestCase):
    """Reduced from the installed ONOKOM-AIR-DK-5-MB-B pair: a deprecated twin without discrete @0."""

    CURRENT = {
        "device_type": "ONOKOM-AIR-DK-5-MB-B",
        "hw": [{"signature": "DK-5-MB-B"}],
        "device": {
            "groups": [{"title": "Errors", "id": "errors", "order": 8}],
            "channels": [
                {"name": "Errors", "reg_type": "discrete", "address": 0, "type": "switch", "group": "errors"},
                {
                    "name": "AC connected",
                    "reg_type": "coil",
                    "address": 20,
                    "type": "switch",
                    "sporadic": True,
                },
            ],
        },
    }
    DEPRECATED = {
        "device_type": "ONOKOM-AIR-DK-5-MB-B",
        "deprecated": True,
        "device": {"channels": [{"name": "AC connected", "reg_type": "coil", "address": 20}]},
    }

    def test_the_template_wb_mqtt_serial_uses_is_loaded(self):
        """With both files installed the non-deprecated one wins, as in wb-mqtt-serial.
        Its discrete @0 answers."""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        (directory / "config-onokom-air-dk-5-mb-b.json").write_text(
            json.dumps(self.CURRENT), encoding="utf-8"
        )
        (directory / "config-onokom-air-dk-5-mb-b-depricated.json").write_text(
            json.dumps(self.DEPRECATED), encoding="utf-8"
        )
        device = TemplateDevice(110, load_installed_template("ONOKOM-AIR-DK-5-MB-B", directory), 0.0)
        self.assertEqual(device.handle_pdu(read_request(2, 0, 1)), bytes([2, 1, 0]))
