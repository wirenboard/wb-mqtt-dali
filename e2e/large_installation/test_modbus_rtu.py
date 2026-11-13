import asyncio
import os
import time
import tty
import unittest

from e2e.large_installation.fast_modbus import (
    DeviceEvents,
    EventKey,
    EventPriority,
    EventType,
    ModbusFunction,
)
from e2e.large_installation.installation_profile import Parity, PortProfile
from e2e.large_installation.modbus_rtu import (
    RESPONSE_GAP_BYTES,
    RESPONSE_GAP_FIXED_S,
    STALE_INPUT_S,
    RequestFramer,
    RtuLine,
    crc16,
    crc_ok,
    with_crc,
)
from e2e.large_installation.template_device import (
    RegisterKind,
    TemplateDevice,
    template_from_dict,
)

TEMPLATE = template_from_dict(
    {
        "device_type": "WB-TEST",
        "device": {
            "channels": [{"reg_type": "input", "address": 100}, {"reg_type": "input", "address": 101}]
        },
    }
)
EVENTS_REQUEST_HEAD = bytes([0xFD, ModbusFunction.FAST_MODBUS, 0x10])
PORT_115200 = PortProfile(115200, Parity.NONE, 2)


def read_request(slave_id: int, address: int, count: int) -> bytes:
    return with_crc(
        bytes([slave_id, ModbusFunction.READ_INPUT_REGISTERS])
        + address.to_bytes(2, "big")
        + count.to_bytes(2, "big")
    )


def events_request(start: int, max_bytes: int, confirm_slave: int = 0, confirm_flag: int = 0) -> bytes:
    return with_crc(EVENTS_REQUEST_HEAD + bytes([start, max_bytes, confirm_slave, confirm_flag]))


def enable_input_events(device: TemplateDevice, address: int, priority: EventPriority) -> None:
    device.handle_pdu(
        bytes([ModbusFunction.FAST_MODBUS, 0x18, 5, EventType.INPUT])
        + address.to_bytes(2, "big")
        + bytes([1, priority])
    )


class CrcTest(unittest.TestCase):
    def test_known_vector(self):
        frame = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A])
        self.assertEqual(with_crc(frame)[-2:], bytes([0xC5, 0xCD]))
        self.assertEqual(crc16(frame), 0xCDC5)
        self.assertTrue(crc_ok(with_crc(frame)))


class FramerTest(unittest.TestCase):
    def frames(self, chunks: list[tuple[bytes, float]]) -> list[bytes]:
        framer = RequestFramer()
        return [f.frame for data, t in chunks for f in framer.feed(data, t)]

    def test_splits_requests_and_skips_garbage(self):
        """A leading stray byte is skipped and a request split over two reads is put together."""
        request = read_request(20, 10, 1)
        self.assertEqual(
            self.frames([(b"\x00" + request[:3], 0.0), (request[3:] + request, 0.001)]), [request] * 2
        )

    def test_write_multiple_registers_is_sized_by_its_byte_count(self):
        request = with_crc(bytes([20, ModbusFunction.WRITE_MULTIPLE_REGISTERS, 0, 10, 0, 2, 4, 1, 2, 3, 4]))
        self.assertEqual(self.frames([(request[:7], 0.0), (request[7:], 0.001)]), [request])

    def test_fast_modbus_enable_and_events_request(self):
        """An enable-events request and an events request arriving in one read are framed apart."""
        enable = with_crc(bytes([20, ModbusFunction.FAST_MODBUS, 0x18, 5, EventType.INPUT, 0, 100, 1, 1]))
        self.assertEqual(
            self.frames([(enable + events_request(0, 100), 0.0)]), [enable, events_request(0, 100)]
        )

    def test_stale_partial_input_is_dropped(self):
        """Half a request, then a pause longer than the stale limit, then a whole request.
        Only the whole one is framed."""
        request = read_request(20, 10, 1)
        self.assertEqual(self.frames([(request[:4], 0.0), (request, 2 * STALE_INPUT_S)]), [request])


class DeviceEventsTest(unittest.TestCase):
    def setUp(self):
        self.values = {100: 1, 101: 2, 102: 3}
        self.events = DeviceEvents(
            lambda key: True, lambda key: self.values[key.address].to_bytes(2, "little")
        )
        for address in self.values:
            self.events.enabled[EventKey(EventType.INPUT, address)] = EventPriority.LOW

    def change(self, address: int, value: int) -> None:
        self.values[address] = value
        self.events.notify_change(EventKey(EventType.INPUT, address))

    def test_change_after_send_survives_the_confirmation(self):
        """The register changes again after its event was sent.
        Confirming the old batch keeps the new change pending."""
        self.change(100, 5)
        flag = self.events.build_response(100)[2]
        self.change(100, 6)
        self.events.confirm(flag)
        self.assertTrue(self.events.has_pending())

    def test_wrong_flag_does_not_confirm(self):
        """A confirmation with another flag than the batch was sent under leaves the event pending."""
        self.change(100, 5)
        flag = self.events.build_response(100)[2]
        self.events.confirm((flag + 1) & 0xFF)
        self.assertTrue(self.events.has_pending())

    def test_batch_is_split_by_the_size_limit(self):
        """Three 6-byte events with room for two: the third comes in the next batch."""
        for address in self.values:
            self.change(address, 9)
        first = self.events.build_response(12)
        self.assertEqual(first[3], 2)
        self.events.confirm(first[2])
        second = self.events.build_response(12)
        self.assertEqual((second[3], second[7:9]), (1, (102).to_bytes(2, "big")))

    def test_flag_wraps_after_255(self):
        """Flags of consecutive confirmed batches run 1..255 and wrap to 0."""
        flags = []
        for value in range(257):
            self.change(100, value)
            flags.append(self.events.build_response(100)[2])
            self.events.confirm(flags[-1])
        self.assertEqual((flags[254], flags[255], flags[256]), (255, 0, 1))


class ArbitrationTest(unittest.TestCase):
    def setUp(self):
        self.low = TemplateDevice(10, TEMPLATE, 0.0)
        self.high = TemplateDevice(20, TEMPLATE, 0.0)
        self.line = RtuLine([self.low, self.high], PORT_115200)

    def respond(self, frame: bytes) -> bytes:
        response = self.line.respond(frame)
        self.assertIsNotNone(response)
        return response[0]

    def test_no_events_answer_comes_from_the_broadcast_id(self):
        self.assertEqual(
            self.respond(events_request(0, 100))[:3], bytes([0xFD, ModbusFunction.FAST_MODBUS, 0x12])
        )

    def test_higher_priority_wins_then_lower_slave_id(self):
        """Both slaves have a change; the one with the high-priority register answers first,
        and with equal priorities the lower slave id does."""
        enable_input_events(self.low, 100, EventPriority.LOW)
        enable_input_events(self.high, 100, EventPriority.HIGH)
        self.low.set(RegisterKind.INPUT, 100, 1)
        self.high.set(RegisterKind.INPUT, 100, 1)
        self.assertEqual(self.respond(events_request(0, 100))[0], 20)

    def test_starting_slave_id_excludes_lower_slaves(self):
        """A slave below the starting slave id stays out of the arbitration."""
        enable_input_events(self.low, 100, EventPriority.HIGH)
        self.low.set(RegisterKind.INPUT, 100, 1)
        self.assertEqual(self.respond(events_request(11, 100))[2], 0x12)
        self.assertEqual(self.respond(events_request(10, 100))[0], 10)

    def test_confirmation_inside_the_events_request(self):
        """The next events request confirms the batch it names; the slave then has nothing left."""
        enable_input_events(self.low, 100, EventPriority.LOW)
        self.low.set(RegisterKind.INPUT, 100, 1)
        flag = self.respond(events_request(0, 100))[3]
        self.assertEqual(self.respond(events_request(0, 100, 10, flag))[2], 0x12)


class LinePacingTest(unittest.IsolatedAsyncioTestCase):
    async def test_response_arrives_no_earlier_than_on_the_wire(self):
        """A read at 9600 baud with a 5 ms response time, sent into the slave end of a real pty.
        The whole response takes at least request + delay + response on the wire."""
        port = PortProfile(9600, Parity.NONE, 2)
        device = TemplateDevice(20, TEMPLATE, 0.005)
        line = RtuLine([device], port)
        master, slave = os.openpty()
        tty.setraw(slave)
        os.set_blocking(slave, False)
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        serving = asyncio.create_task(line.serve(master))
        self.addCleanup(serving.cancel)
        await asyncio.sleep(0.05)
        request = read_request(20, 100, 1)
        started = time.monotonic()
        os.write(slave, request)
        received = b""
        while len(received) < 7 and time.monotonic() - started < 1.0:
            await asyncio.sleep(0.001)
            try:
                received += os.read(slave, 64)
            except BlockingIOError:
                pass
        elapsed = time.monotonic() - started
        self.assertTrue(crc_ok(received))
        self.assertGreaterEqual(elapsed, (len(request) + len(received)) * port.byte_time_s + 0.005)

    async def test_response_gap_follows_the_baud_rate(self):
        """A device without its own delay at 9600 baud: the response still waits 0.23 ms + 8 bytes."""
        port = PortProfile(9600, Parity.NONE, 2)
        line = RtuLine([TemplateDevice(20, TEMPLATE, 0.0)], port)
        master, slave = os.openpty()
        tty.setraw(slave)
        os.set_blocking(slave, False)
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        serving = asyncio.create_task(line.serve(master))
        self.addCleanup(serving.cancel)
        await asyncio.sleep(0.05)
        request = read_request(20, 100, 1)
        started = time.monotonic()
        os.write(slave, request)
        received = b""
        while len(received) < 7 and time.monotonic() - started < 1.0:
            await asyncio.sleep(0.001)
            try:
                received += os.read(slave, 64)
            except BlockingIOError:
                pass
        elapsed = time.monotonic() - started
        self.assertTrue(crc_ok(received))
        wire_s = (len(request) + len(received)) * port.byte_time_s
        self.assertGreaterEqual(
            elapsed, wire_s + RESPONSE_GAP_FIXED_S + RESPONSE_GAP_BYTES * port.byte_time_s
        )


class PortReopenTest(unittest.IsolatedAsyncioTestCase):
    async def test_serial_side_can_close_and_reopen(self):
        """The slave fd is held as in `emulator.run`; a second one opens, reads and closes three times.
        The line answers every time."""
        line = RtuLine([TemplateDevice(20, TEMPLATE, 0.0)], PORT_115200)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        serving = asyncio.create_task(line.serve(master))
        self.addCleanup(serving.cancel)
        for _ in range(3):
            port = os.open(os.ttyname(slave), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            tty.setraw(port)
            os.write(port, read_request(20, 100, 1))
            received = b""
            for _ in range(200):
                await asyncio.sleep(0.002)
                try:
                    received += os.read(port, 64)
                except BlockingIOError:
                    pass
                if len(received) >= 7:
                    break
            os.close(port)
            self.assertTrue(crc_ok(received))
        self.assertFalse(serving.done())
