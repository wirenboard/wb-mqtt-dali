# Device side of WB Fast Modbus (0x46): event enabling and event reading with confirmation.
# Wire format follows wb-mqtt-serial `src/modbus_ext_common.cpp`.

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable, Optional

FAST_MODBUS_BROADCAST_ID = 0xFD
EVENTS_REQUEST = 0x10
HAS_EVENTS_RESPONSE = 0x11
NO_EVENTS_RESPONSE = 0x12
ENABLE_EVENTS = 0x18
EVENTS_REQUEST_PDU_SIZE = 6
EVENT_HEADER_SIZE = 4
ENABLE_RECORD_HEADER_SIZE = 4


class ModbusFunction(enum.IntEnum):
    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_COILS = 0x0F
    WRITE_MULTIPLE_REGISTERS = 0x10
    FAST_MODBUS = 0x46


class EventType(enum.IntEnum):
    COIL = 1
    DISCRETE = 2
    HOLDING = 3
    INPUT = 4


class EventPriority(enum.IntEnum):
    DISABLED = 0
    LOW = 1
    HIGH = 2


@dataclass(frozen=True)
class EventKey:
    type: EventType
    address: int


@dataclass(frozen=True)
class EventsRequest:
    starting_slave_id: int
    max_data_bytes: int
    confirm_slave_id: int
    confirm_flag: int


@dataclass
class _InflightBatch:
    flag: int
    keys: dict[EventKey, int]


class DeviceEvents:
    """Events of one slave; `read_data` returns the event payload of a register as it is now."""

    def __init__(
        self, can_enable: Callable[[EventKey], bool], read_data: Callable[[EventKey], bytes]
    ) -> None:
        self.can_enable = can_enable
        self.read_data = read_data
        self.enabled: dict[EventKey, EventPriority] = {}
        self._pending: dict[EventKey, int] = {}
        self._sequence = 0
        self._flag = 0
        self._inflight: Optional[_InflightBatch] = None

    def handle_enable(self, pdu: bytes) -> bytes:
        """Answer a `0x46 0x18` request PDU; one response bit per requested register."""
        size = pdu[2]
        records = pdu[3 : 3 + size]
        if len(records) != size:
            raise ValueError("truncated enable events request")
        answer = bytearray()
        pos = 0
        while pos < len(records):
            raw_type = records[pos]
            address = int.from_bytes(records[pos + 1 : pos + 3], "big")
            count = records[pos + 3]
            priorities = records[pos + ENABLE_RECORD_HEADER_SIZE : pos + ENABLE_RECORD_HEADER_SIZE + count]
            pos += ENABLE_RECORD_HEADER_SIZE + count
            bits = bytearray((count + 7) // 8)
            for offset, raw_priority in enumerate(priorities):
                if raw_type not in EventType:
                    continue
                key = EventKey(EventType(raw_type), address + offset)
                priority = EventPriority(raw_priority)
                if priority == EventPriority.DISABLED or not self.can_enable(key):
                    self.enabled.pop(key, None)
                    self._pending.pop(key, None)
                    continue
                self.enabled[key] = priority
                bits[offset // 8] |= 1 << (offset % 8)
            answer += bits
        return bytes([ModbusFunction.FAST_MODBUS, ENABLE_EVENTS, len(answer)]) + bytes(answer)

    def notify_change(self, key: EventKey) -> None:
        if key not in self.enabled:
            return
        self._sequence += 1
        self._pending[key] = self._sequence

    def has_pending(self) -> bool:
        return self.priority() != EventPriority.DISABLED

    def priority(self) -> EventPriority:
        keys = list(self._pending)
        if self._inflight is not None:
            keys.extend(self._inflight.keys)
        return max((self.enabled.get(k, EventPriority.LOW) for k in keys), default=EventPriority.DISABLED)

    def confirm(self, flag: int) -> None:
        batch = self._inflight
        if batch is None or batch.flag != flag:
            return
        for key, sequence in batch.keys.items():
            if self._pending.get(key) == sequence:
                del self._pending[key]
        self._inflight = None

    def build_response(self, max_data_bytes: int) -> bytes:
        """`0x46 0x11` PDU; an unconfirmed batch is sent again under its flag."""
        if self._inflight is None:
            self._inflight = self._next_batch(max_data_bytes)
        batch = self._inflight
        data = bytearray()
        for key in batch.keys:
            payload = self.read_data(key)
            data += bytes([len(payload), key.type]) + key.address.to_bytes(2, "big") + payload
        header = bytes(
            [ModbusFunction.FAST_MODBUS, HAS_EVENTS_RESPONSE, batch.flag, len(batch.keys), len(data)]
        )
        return header + bytes(data)

    # --- Private ---

    def _next_batch(self, max_data_bytes: int) -> _InflightBatch:
        self._flag = (self._flag + 1) & 0xFF
        used = 0
        ordered = sorted(
            self._pending.items(), key=lambda item: (-self.enabled.get(item[0], EventPriority.LOW), item[1])
        )
        keys: dict[EventKey, int] = {}
        for key, sequence in ordered:
            size = EVENT_HEADER_SIZE + len(self.read_data(key))
            if used + size > max_data_bytes and keys:
                break
            keys[key] = sequence
            used += size
        return _InflightBatch(self._flag, keys)


def parse_events_request(pdu: bytes) -> EventsRequest:
    if len(pdu) != EVENTS_REQUEST_PDU_SIZE or pdu[1] != EVENTS_REQUEST:
        raise ValueError("not an events request")
    return EventsRequest(pdu[2], pdu[3], pdu[4], pdu[5])


def no_events_pdu() -> bytes:
    return bytes([ModbusFunction.FAST_MODBUS, NO_EVENTS_RESPONSE])


def register_event_payload(event_type: EventType, value: int) -> bytes:
    # wb-mqtt-serial copies the payload into a little-endian integer.
    if event_type in (EventType.COIL, EventType.DISCRETE):
        return bytes([1 if value else 0])
    return (value & 0xFFFF).to_bytes(2, "little")
