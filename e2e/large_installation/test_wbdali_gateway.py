import asyncio
import unittest
import unittest.mock

from dali.address import DeviceShort, GearBroadcast, GearShort, InstanceNumber
from dali.device.general import QueryInstanceType
from dali.gear.general import QueryActualLevel, QueryControlGearPresent

from e2e.large_installation.dali_fakes import ControlDevice, DaliBus, EmulatedGear
from e2e.large_installation.fast_modbus import EventType, ModbusFunction
from e2e.large_installation.installation_profile import (
    ControlDeviceSpec,
    GearModel,
    GearSpec,
    TimingProfile,
)
from e2e.large_installation.template_device import DeviceTemplate, RegisterKind
from e2e.large_installation.wbdali_gateway import (
    DRIVER,
    REPLIES_REGISTER,
    EmulatedWbDali,
    GatewayQueue,
    decode_slot,
    reply_value,
)
from wb.mqtt_dali.wbdali import (
    FramePriority,
    GatewayReplyStatus,
    encode_frame_for_modbus,
)

PRESENT = encode_frame_for_modbus(
    QueryControlGearPresent(GearShort(1)).frame, False, FramePriority.USER_ACTION
)
OTHER = encode_frame_for_modbus(QueryActualLevel(GearShort(1)).frame, False, FramePriority.USER_ACTION)
ZERO_TIMING = TimingProfile(0, 0, 0, 0, 0, (0, 0, 0, 0, 0))
EMPTY_TEMPLATE = DeviceTemplate("WB-DALI", {kind: frozenset() for kind in RegisterKind})


def words(raw: int) -> list[int]:
    return [raw & 0xFFFF, raw >> 16]


def write_slots_request(first_register: int, raws: list[int]) -> bytes:
    payload = b"".join(w.to_bytes(2, "big") for raw in raws for w in words(raw))
    return (
        bytes([ModbusFunction.WRITE_MULTIPLE_REGISTERS])
        + first_register.to_bytes(2, "big")
        + (2 * len(raws)).to_bytes(2, "big")
        + bytes([len(payload)])
        + payload
    )


class SlotEncodingTest(unittest.TestCase):
    def test_decodes_what_the_driver_encodes(self):
        """A 24-bit sendtwice frame encoded by the driver decodes to the same frame and flags."""
        frame = QueryInstanceType(DeviceShort(5), InstanceNumber(2)).frame
        slot = decode_slot(encode_frame_for_modbus(frame, True, FramePriority.CONFIGURATION))
        self.assertEqual((len(slot.frame), slot.frame.as_integer), (24, frame.as_integer))
        self.assertTrue(slot.sendtwice)
        self.assertEqual(slot.priority, FramePriority.CONFIGURATION.value)


class GatewayQueueTest(unittest.TestCase):
    def test_executes_slots_in_order_from_the_pointer(self):
        """After slot 0 completes, the next execution is slot 1."""
        queue = GatewayQueue()
        queue.write_words(0, words(PRESENT) + words(OTHER))
        execution = queue.next_execution()
        self.assertEqual((execution.slot, execution.raw), (0, PRESENT))
        queue.complete(execution, 0x1FF)
        self.assertEqual((queue.next_execution().slot, queue.next_execution().raw), (1, OTHER))

    def test_executed_slot_is_zeroed_and_pointer_stops_on_empty_slot(self):
        """A completed slot is zeroed and the pointer stops on the empty slot 1 before slot 2."""
        queue = GatewayQueue()
        queue.write_words(0, words(PRESENT))
        queue.write_words(4, words(PRESENT))
        queue.complete(queue.next_execution(), 0x1FF)
        self.assertEqual((queue.slots[0], queue.pointer), (0, 1))
        self.assertIsNone(queue.next_execution())

    def test_overwrite_before_execution_runs_the_new_command(self):
        """A slot rewritten before the gateway reaches it executes the later command."""
        queue = GatewayQueue()
        queue.write_words(0, words(PRESENT))
        queue.write_words(0, words(OTHER))
        self.assertEqual(queue.next_execution().raw, OTHER)

    def test_zero_pointer_clears_pending_slots(self):
        """Writing 0 to the pointer empties every unexecuted slot and points at slot 0."""
        queue = GatewayQueue()
        queue.write_words(2, words(PRESENT) + words(OTHER))
        queue.write_pointer(0)
        self.assertEqual((queue.slots, queue.pointer), ([0] * len(queue.slots), 0))

    def test_slot_write_zeroes_its_reply_only(self):
        """Writing slot 0 zeroes reply 0 and leaves the reply of slot 1."""
        queue = GatewayQueue()
        queue.replies[0] = queue.replies[1] = 0x1FF
        queue.write_words(0, words(PRESENT))
        self.assertEqual(queue.replies[:2], [0, 0x1FF])

    def test_replies_survive_a_pointer_reset(self):
        """A reply outlives a pointer reset; only a write to its slot zeroes it."""
        queue = GatewayQueue()
        queue.write_words(0, words(PRESENT))
        queue.complete(queue.next_execution(), 0x1FF)
        queue.write_pointer(0)
        self.assertEqual(queue.replies[0], 0x1FF)

    def test_reset_while_slot_0_executes_leaves_the_pointer_at_0(self):
        """A pointer reset during slot 0's frame keeps the pointer at 0; the reply is still written."""
        queue = GatewayQueue()
        queue.write_words(0, words(PRESENT))
        in_flight = queue.next_execution()
        queue.write_pointer(0)
        queue.complete(in_flight, 0x1FF)
        self.assertEqual((queue.pointer, queue.replies[0]), (0, 0x1FF))

    def test_overwrite_while_executing_keeps_the_new_command(self):
        """The slot is overwritten while its first command is on the bus.
        The new command stays and runs next, the pointer having come round to it."""
        queue = GatewayQueue()
        queue.write_words(0, words(PRESENT))
        in_flight = queue.next_execution()
        queue.write_words(0, words(OTHER))
        queue.complete(in_flight, 0x1FF)
        self.assertEqual(queue.slots[0], OTHER)
        self.assertEqual(queue.pointer, 1)


class EmulatedWbDaliTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gear = [
            EmulatedGear(GearSpec(1, 0x111111, GearModel.PLAIN, frozenset())),
            EmulatedGear(GearSpec(2, 0x111112, GearModel.PLAIN, frozenset())),
        ]
        bus = DaliBus([*self.gear, ControlDevice(ControlDeviceSpec(5, 0x222222, (1, 4)))])
        self.gateway = EmulatedWbDali(7, EMPTY_TEMPLATE, ZERO_TIMING, [bus, DaliBus([])], "1.2.3")

    def start_gateway(self) -> None:
        task = asyncio.create_task(self.gateway.run())
        self.addCleanup(task.cancel)

    def enable_reply_events(self) -> None:
        enable = bytes([ModbusFunction.FAST_MODBUS, 0x18, 6, EventType.INPUT]) + REPLIES_REGISTER.to_bytes(
            2, "big"
        )
        self.assertEqual(self.gateway.handle_pdu(enable + bytes([2, 1, 1]))[3], 0b11)

    async def test_modbus_write_runs_the_frames_and_raises_reply_events(self):
        """A 0x10 write of two slots on bus 1 with reply events enabled.
        Replies hold the fakes' answers, the pointer moves on and events are pending."""
        self.start_gateway()
        self.enable_reply_events()
        instance_query = encode_frame_for_modbus(
            QueryInstanceType(DeviceShort(5), InstanceNumber(1)).frame, False, FramePriority.CONFIGURATION
        )
        request = write_slots_request(DRIVER.queue_start_modbus_address, [PRESENT, instance_query])
        self.assertEqual(self.gateway.handle_pdu(request), request[:5])
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(
            self.gateway.get(RegisterKind.INPUT, REPLIES_REGISTER),
            reply_value(GatewayReplyStatus.BACKWARD_FRAME, 0xFF),
        )
        self.assertEqual(
            self.gateway.get(RegisterKind.INPUT, REPLIES_REGISTER + 1),
            reply_value(GatewayReplyStatus.BACKWARD_FRAME, 4),
        )
        self.assertEqual(
            self.gateway.get(RegisterKind.HOLDING, DRIVER.queue_bulk_send_pointer_modbus_address), 2
        )
        self.assertTrue(self.gateway.events.has_pending())

    def test_reply_statuses_follow_the_bus(self):
        """Silence, a slot with no-send priority and two gear answering at once."""
        bus = self.gateway.buses[0]
        absent = encode_frame_for_modbus(
            QueryActualLevel(GearShort(9)).frame, False, FramePriority.USER_ACTION
        )
        broadcast = encode_frame_for_modbus(
            QueryControlGearPresent(GearBroadcast()).frame, False, FramePriority.USER_ACTION
        )
        self.assertEqual(
            bus.execute_now(decode_slot(absent)) >> 8, GatewayReplyStatus.NO_BACKWARD_FRAME.value
        )
        self.assertEqual(
            bus.execute_now(decode_slot(PRESENT & 0x1FFFFFFF)) >> 8, GatewayReplyStatus.NO_TRANSMISSION.value
        )
        self.assertEqual(
            bus.execute_now(decode_slot(broadcast)) >> 8, GatewayReplyStatus.BROKEN_BACKWARD_FRAME.value
        )

    def test_slot_write_zeroes_the_reply_register_without_an_event(self):
        """A slot write zeroes an old reply with reply events enabled, and no event is pending."""
        self.enable_reply_events()
        self.gateway.buses[0].queue.replies[0] = 0x1FF
        self.gateway.set_silently(RegisterKind.INPUT, REPLIES_REGISTER, 0x1FF)
        self.gateway.handle_pdu(write_slots_request(DRIVER.queue_start_modbus_address, [PRESENT]))
        self.assertEqual(self.gateway.get(RegisterKind.INPUT, REPLIES_REGISTER), 0)
        self.assertFalse(self.gateway.events.has_pending())

    def test_second_bus_uses_the_register_offset(self):
        """The pointer of bus 2 sits 1000 registers above bus 1's."""
        pointer_bus_2 = DRIVER.queue_bulk_send_pointer_modbus_address + DRIVER.queue_modbus_bus_offset
        self.gateway.handle_pdu(
            bytes([ModbusFunction.WRITE_SINGLE_REGISTER])
            + pointer_bus_2.to_bytes(2, "big")
            + (3).to_bytes(2, "big")
        )
        self.assertEqual((self.gateway.buses[1].queue.pointer, self.gateway.buses[0].queue.pointer), (3, 0))


TIMING = TimingProfile(
    forward_16_ms=16,
    forward_24_ms=24,
    backward_ms=9,
    backward_delay_ms=7,
    no_backward_wait_ms=11,
    settling_ms=(13, 15, 17, 19, 21),
)
REAL_SLEEP = asyncio.sleep


class RecordedSleepCase(unittest.IsolatedAsyncioTestCase):
    """A gateway with one bus whose every sleep is recorded instead of waited."""

    timing = TIMING

    def setUp(self):
        self.delays: list[float] = []
        bus = DaliBus(
            [
                EmulatedGear(GearSpec(1, 0x111111, GearModel.PLAIN, frozenset())),
                ControlDevice(ControlDeviceSpec(5, 0x222222, (1,))),
            ]
        )
        self.gateway = EmulatedWbDali(7, EMPTY_TEMPLATE, self.timing, [bus], "1.2.3")

    async def asyncSetUp(self):
        async def record(delay: float) -> None:
            self.delays.append(round(delay * 1000, 3))
            await REAL_SLEEP(0)

        patcher = unittest.mock.patch("e2e.large_installation.wbdali_gateway.asyncio.sleep", record)
        patcher.start()
        self.addCleanup(patcher.stop)
        task = asyncio.create_task(self.gateway.run())
        self.addCleanup(task.cancel)

    async def run_slots(self, raws: list[int]) -> None:
        self.gateway.handle_pdu(write_slots_request(DRIVER.queue_start_modbus_address, raws))
        for _ in range(20):
            await REAL_SLEEP(0)


class GatewayTimingTest(RecordedSleepCase):
    async def test_answered_16_bit_query(self):
        """A 16-bit query that gear answers takes the forward frame, then backward delay and frame."""
        await self.run_slots([PRESENT])
        self.assertEqual(self.delays, [16, 7 + 9])

    async def test_unanswered_query_waits_for_the_backward_frame(self):
        """A query nobody answers takes the forward frame, then the wait for a backward frame."""
        absent = encode_frame_for_modbus(
            QueryActualLevel(GearShort(9)).frame, False, FramePriority.USER_ACTION
        )
        await self.run_slots([absent])
        self.assertEqual(self.delays, [16, 11])

    async def test_sendtwice_24_bit_frame_is_sent_twice_with_settling(self):
        """A configuration-priority 24-bit frame sent twice, answered by the control device."""
        frame = encode_frame_for_modbus(
            QueryInstanceType(DeviceShort(5), InstanceNumber(0)).frame, True, FramePriority.CONFIGURATION
        )
        await self.run_slots([frame])
        self.assertEqual(self.delays, [2 * 24 + 17, 7 + 9])

    async def test_back_to_back_slots_wait_the_settling_time(self):
        """The second user-action frame waits the priority-2 settling time after the first ends."""
        await self.run_slots([PRESENT, PRESENT])
        self.assertEqual(self.delays[:2], [16, 16])
        self.assertAlmostEqual(self.delays[2], 15, delta=1)
        self.assertEqual(self.delays[3:], [16, 16])
