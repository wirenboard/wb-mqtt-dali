"""The WB-DALI module's Modbus register map.

Taken from the WB-DALI device template shipped with wb-mqtt-serial
(`config-wb-dali.json`). Bus 2 and 3 repeat the map at +1000 and +2000.

| Registers   | Type    | Meaning                          |
| ----------- | ------- | -------------------------------- |
| 1400..1431  | holding | send queue, 16 slots × 2         |
| 1432        | holding | bulk send pointer / queue reset  |
| 1500..1515  | input   | per-slot transmission result     |
| 1900..1915  | input   | bus monitor ring, 4 slots × u64  |

A queue slot holds a 32-bit value in two registers, low word first:

    [24..0]  frame data, right-aligned      [27..25] frame size (0=FF16, 1=FF24, 2=FF25)
    [28]     send twice                     [31..29] priority (0 = do not send)

A reply register holds `status << 8 | backward_frame`.

A bus monitor slot is 64 bits, `word_order: little_endian`:

    [63..48] frame counter, mod 2^16   [41] broken   [40] backward frame
    [39..32] frame length in bits      [24..0] frame data      0 = empty slot
"""

from __future__ import annotations

from enum import IntEnum
from typing import List, Optional, Tuple

BUS_ADDRESS_OFFSET = 1000

QUEUE_BASE = 1400
QUEUE_SIZE = 16
QUEUE_POINTER = 1432
REPLY_BASE = 1500

MONITOR_BASE = 1900
MONITOR_RING_SIZE = 4
MONITOR_REGISTERS_PER_SLOT = 4

# The frame counter in a monitor slot is 16 bits wide.
FRAME_COUNTER_MODULO = 1 << 16

FRAME_SIZE_BITS = {0: 16, 1: 24, 2: 25}
FRAME_SIZE_CODES = {bits: code for code, bits in FRAME_SIZE_BITS.items()}


class TransmissionStatus(IntEnum):
    """The status byte the gateway reports for a sent frame.

    Mirrors the layout `WBDALIDriver._handle_reply_message` decodes.
    """

    NO_TRANSMISSION = 0
    WITH_BACKWARD_RESPONSE = 1
    WITHOUT_RESPONSE = 2
    BROKEN_RESPONSE = 3
    NO_POWER_ON_BUS = 4
    OVERHEAT = 5


def bus_offset(bus: int) -> int:
    """The register offset of a bus: bus 2 and 3 repeat the map at +1000 and +2000."""
    return (bus - 1) * BUS_ADDRESS_OFFSET


def queue_slot_address(bus: int, slot: int) -> int:
    """First of the two holding registers of a send-queue slot."""
    return QUEUE_BASE + bus_offset(bus) + slot * 2


def queue_pointer_address(bus: int) -> int:
    """The holding register of the bulk-send pointer."""
    return QUEUE_POINTER + bus_offset(bus)


def reply_address(bus: int, slot: int) -> int:
    """The input register carrying the answer to the frame sent from ``slot``."""
    return REPLY_BASE + bus_offset(bus) + slot


def encode_frame(frame: int, bit_length: int, sendtwice: bool, priority: int) -> int:
    """Pack one forward frame into the 32-bit value a queue slot carries."""
    size_code = FRAME_SIZE_CODES.get(bit_length)
    if size_code is None:
        raise ValueError(f"unsupported frame length {bit_length}")
    return (frame & 0x1FFFFFF) | (size_code << 25) | (int(sendtwice) << 28) | ((priority & 0x7) << 29)


def decode_frame(value: int) -> Optional[Tuple[int, int, bool, int]]:
    """Unpack a queue slot into ``(frame, bit_length, sendtwice, priority)``.

    Returns ``None`` for priority 0, which means "do not send".
    """
    priority = (value >> 29) & 0x7
    if priority == 0:
        return None
    bit_length = FRAME_SIZE_BITS.get((value >> 25) & 0x7)
    if bit_length is None:
        return None
    return value & ((1 << bit_length) - 1), bit_length, bool(value & (1 << 28)), priority


def to_registers(value: int) -> List[int]:
    """Split a 32-bit queue value into its two registers, low word first."""
    return [value & 0xFFFF, (value >> 16) & 0xFFFF]


def from_registers(registers: List[int]) -> int:
    """Join two registers, low word first, into a 32-bit queue value."""
    return (registers[1] << 16) | registers[0]


def decode_reply(value: int) -> Tuple[Optional[TransmissionStatus], int]:
    """Split a reply register into its status and backward frame.

    A status this enum does not name comes back as ``None`` rather than folded
    into one of the known ones: "the gateway said something we do not
    understand" is not the same as "the gateway did not transmit".
    """
    try:
        return TransmissionStatus((value >> 8) & 0xFF), value & 0xFF
    except ValueError:
        return None, value & 0xFF


def encode_reply(status: TransmissionStatus, backward: int) -> int:
    """Pack a reply register: status in the high byte, backward frame in the low."""
    return (int(status) << 8) | (backward & 0xFF)


def monitor_address(bus: int, slot: int) -> int:
    """First of the four input registers of a bus monitor ring slot."""
    return MONITOR_BASE + bus_offset(bus) + slot * MONITOR_REGISTERS_PER_SLOT


def encode_monitor_slot(
    counter: int, bit_length: int, frame: int, backward: bool = False, broken: bool = False
) -> int:
    """Pack a monitor ring slot: counter, frame length in bits, frame, and the flags."""
    return (
        ((counter % FRAME_COUNTER_MODULO) << 48)
        | (int(broken) << 41)
        | (int(backward) << 40)
        | ((bit_length & 0xFF) << 32)
        | (frame & 0x1FFFFFF)
    )


def to_monitor_registers(value: int) -> List[int]:
    """Split a monitor slot into its four registers, least significant word first."""
    return [(value >> (16 * word)) & 0xFFFF for word in range(MONITOR_REGISTERS_PER_SLOT)]


def from_monitor_registers(registers: List[int]) -> int:
    """Join four registers, low word first, into a 64-bit monitor slot value."""
    return sum(register << (16 * word) for word, register in enumerate(registers))
