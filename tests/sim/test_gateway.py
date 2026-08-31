"""Register-level behaviour of the virtual WB-DALI module.

These pin what was measured on a real module: the firmware transmits armed
slots strictly from its pointer forward, clears a slot as it consumes it, and
a written slot's previous answer is void the moment it is written.
"""

from dali.address import GearShort
from dali.gear.general import DAPC, QueryActualLevel
from dali.tests import fakes

from wb.mqtt_dali.sim.dali_bus import SimulatedDaliBus
from wb.mqtt_dali.sim.gateway import VirtualWbDaliGateway
from wb.mqtt_dali.sim.registers import (
    MONITOR_REGISTERS_PER_SLOT,
    MONITOR_RING_SIZE,
    TransmissionStatus,
    decode_reply,
    encode_frame,
    from_monitor_registers,
    monitor_address,
    queue_pointer_address,
    queue_slot_address,
    reply_address,
    to_registers,
)


def _armed(command):
    frame = command.frame
    return to_registers(encode_frame(frame.as_integer, len(frame), sendtwice=False, priority=1))


def _module():
    gear = fakes.Gear(shortaddr=0)
    return VirtualWbDaliGateway({1: SimulatedDaliBus([gear])}), gear


def test_rewind_then_one_batch_write_sends_every_slot_in_order():
    """Slots 0..n-1 written in one fc16 after a rewind go out back to back;
    each reply register carries its own frame's answer, consumed slots are cleared."""
    gateway, gear = _module()
    gateway.write_holding(queue_pointer_address(1), [0])
    gateway.write_holding(
        queue_slot_address(1, 0), _armed(DAPC(GearShort(0), 100)) + _armed(QueryActualLevel(GearShort(0)))
    )

    first, second = gateway.read_input(reply_address(1, 0), 2)
    assert decode_reply(first)[0] is TransmissionStatus.WITHOUT_RESPONSE
    assert decode_reply(second) == (TransmissionStatus.WITH_BACKWARD_RESPONSE, 100)
    assert gear.level == 100
    bus = gateway.buses[1]
    assert bus.queue[:4] == [0, 0, 0, 0]
    assert bus.pointer == 2


def test_a_slot_behind_the_pointer_waits_for_the_pointer_to_come_round():
    """The firmware stops at the first empty slot: slot 1 armed with the pointer at 0
    is not transmitted until the pointer is rewound onto it."""
    gateway, _gear = _module()
    gateway.write_holding(queue_pointer_address(1), [0])
    gateway.write_holding(queue_slot_address(1, 1), _armed(QueryActualLevel(GearShort(0))))
    assert gateway.read_input(reply_address(1, 1), 1) == [0]

    gateway.write_holding(queue_pointer_address(1), [1])
    assert decode_reply(gateway.read_input(reply_address(1, 1), 1)[0])[0] is (
        TransmissionStatus.WITH_BACKWARD_RESPONSE
    )


def test_writing_a_slot_voids_its_previous_answer():
    """A reply register is cleared when its slot is written, before any transmission —
    a non-zero status always belongs to the frame currently in the slot."""
    gateway, _gear = _module()
    gateway.write_holding(queue_pointer_address(1), [0])
    gateway.write_holding(queue_slot_address(1, 0), _armed(QueryActualLevel(GearShort(0))))
    assert gateway.read_input(reply_address(1, 0), 1) != [0]

    # The pointer has moved on to slot 1; re-arming slot 0 is not transmitted,
    # yet its old answer is gone.
    gateway.write_holding(queue_slot_address(1, 0), _armed(QueryActualLevel(GearShort(0))))
    assert gateway.read_input(reply_address(1, 0), 1) == [0]


def test_monitor_ring_keeps_the_last_four_foreign_frames_with_a_running_counter():
    gateway, _gear = _module()
    for frame in range(1, 6):
        gateway.record_bus_frame(1, 24, frame)

    words = gateway.read_input(monitor_address(1, 0), MONITOR_RING_SIZE * MONITOR_REGISTERS_PER_SLOT)
    slots = [
        from_monitor_registers(words[i * MONITOR_REGISTERS_PER_SLOT : (i + 1) * MONITOR_REGISTERS_PER_SLOT])
        for i in range(MONITOR_RING_SIZE)
    ]
    counters = [raw >> 48 for raw in slots]
    assert counters == [5, 2, 3, 4]
    assert [raw & 0xFF for raw in slots] == [5, 2, 3, 4]
