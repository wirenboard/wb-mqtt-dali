# Modbus RTU slave side of an RS-485 line emulated on the master end of a pty: frames requests by
# length and CRC, dispatches them to the slaves, and paces responses so they arrive no earlier
# than on a wire.

from __future__ import annotations

import asyncio
import os
import termios
import tty
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from e2e.large_installation.fast_modbus import (
    ENABLE_EVENTS,
    EVENTS_REQUEST,
    EVENTS_REQUEST_PDU_SIZE,
    FAST_MODBUS_BROADCAST_ID,
    DeviceEvents,
    EventPriority,
    ModbusFunction,
    no_events_pdu,
    parse_events_request,
)
from e2e.large_installation.installation_profile import PortProfile

CRC_SIZE = 2
FIXED_REQUEST_SIZE = 8
WRITE_MULTIPLE_HEADER_SIZE = 7
FAST_MODBUS_HEADER_SIZE = 4
# Arbitration window of a Fast Modbus events request: 9 x 13 bit times.
ARBITRATION_BITS = 9 * 13
STALE_INPUT_S = 0.05
WRITE_CHUNK_S = 0.002
# Request end to response start seen by software on a WB controller (16550A UART) with WB-DALI.
RESPONSE_GAP_FIXED_S = 0.00023
RESPONSE_GAP_BYTES = 8
FIXED_SIZE_FUNCTIONS = (
    ModbusFunction.READ_COILS,
    ModbusFunction.READ_DISCRETE_INPUTS,
    ModbusFunction.READ_HOLDING_REGISTERS,
    ModbusFunction.READ_INPUT_REGISTERS,
    ModbusFunction.WRITE_SINGLE_COIL,
    ModbusFunction.WRITE_SINGLE_REGISTER,
)
WRITE_MULTIPLE_FUNCTIONS = (ModbusFunction.WRITE_MULTIPLE_COILS, ModbusFunction.WRITE_MULTIPLE_REGISTERS)


class ModbusSlave(Protocol):
    slave_id: int
    events: DeviceEvents

    def handle_pdu(self, pdu: bytes) -> Optional[bytes]: ...

    def response_delay_for(self, function: int) -> float: ...


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(frame: bytes) -> bytes:
    return frame + crc16(frame).to_bytes(2, "little")


def crc_ok(frame: bytes) -> bool:
    return len(frame) > CRC_SIZE and crc16(frame[:-CRC_SIZE]).to_bytes(2, "little") == frame[-CRC_SIZE:]


def expected_request_size(buffer: bytes) -> Optional[int]:
    """Length of the request at the head of `buffer`, None while it cannot be told yet, 0 if unknown."""
    if len(buffer) < 2:
        return None
    function = buffer[1]
    if function in FIXED_SIZE_FUNCTIONS:
        return FIXED_REQUEST_SIZE
    if function in WRITE_MULTIPLE_FUNCTIONS:
        return _sized_by(buffer, WRITE_MULTIPLE_HEADER_SIZE - 1, WRITE_MULTIPLE_HEADER_SIZE)
    if function == ModbusFunction.FAST_MODBUS:
        return _fast_modbus_request_size(buffer)
    return 0


def _fast_modbus_request_size(buffer: bytes) -> Optional[int]:
    if len(buffer) < 3:
        return None
    if buffer[2] == EVENTS_REQUEST:
        return 1 + EVENTS_REQUEST_PDU_SIZE + CRC_SIZE
    if buffer[2] == ENABLE_EVENTS:
        return _sized_by(buffer, FAST_MODBUS_HEADER_SIZE - 1, FAST_MODBUS_HEADER_SIZE)
    return 0


def _sized_by(buffer: bytes, size_position: int, header_size: int) -> Optional[int]:
    if len(buffer) <= size_position:
        return None
    return header_size + buffer[size_position] + CRC_SIZE


@dataclass(frozen=True)
class FramedRequest:
    frame: bytes
    received_at: float


class RequestFramer:  # pylint: disable=too-few-public-methods
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._first_byte_at = 0.0
        self._last_byte_at = 0.0

    def feed(self, data: bytes, now: float) -> list[FramedRequest]:
        if self._buffer and now - self._last_byte_at > STALE_INPUT_S:
            self._buffer.clear()
        if not self._buffer:
            self._first_byte_at = now
        self._buffer += data
        self._last_byte_at = now
        frames = []
        while self._buffer:
            size = expected_request_size(bytes(self._buffer))
            if size is None or (size and len(self._buffer) < size):
                break
            if size and crc_ok(bytes(self._buffer[:size])):
                frames.append(FramedRequest(bytes(self._buffer[:size]), self._first_byte_at))
                del self._buffer[:size]
                self._first_byte_at = now
                continue
            del self._buffer[:1]
        return frames


class RtuLine:  # pylint: disable=too-many-instance-attributes
    """One RS-485 segment shared by `slaves`."""

    def __init__(self, slaves: Sequence[ModbusSlave], port: PortProfile) -> None:
        self._slaves = {slave.slave_id: slave for slave in slaves}
        self._byte_time_s = port.byte_time_s
        self._bit_time_s = 1.0 / port.baud_rate
        self._response_gap_s = RESPONSE_GAP_FIXED_S + RESPONSE_GAP_BYTES * port.byte_time_s
        self._framer = RequestFramer()
        self._line_free_at = 0.0
        self._fd: Optional[int] = None
        self._queue: asyncio.Queue[Optional[FramedRequest]] = asyncio.Queue()

    async def serve(self, fd: int) -> None:
        """Serve the pty master `fd`; it stays open while the caller holds the slave end."""
        loop = asyncio.get_running_loop()
        self._fd = fd
        tty.setraw(fd, termios.TCSANOW)
        os.set_blocking(fd, False)
        loop.add_reader(fd, self._on_readable)
        try:
            while True:
                request = await self._queue.get()
                if request is None:
                    raise ConnectionError("pty hung up")
                response = self.respond(request.frame)
                if response is None:
                    continue
                payload, delay_s = response
                await self._transmit(request, payload, self._response_gap_s + delay_s)
        finally:
            loop.remove_reader(fd)

    def respond(self, frame: bytes) -> Optional[tuple[bytes, float]]:
        """Response frame and turnaround delay for a request, or None if nobody answers."""
        slave_id, pdu = frame[0], frame[1:-CRC_SIZE]
        if (
            slave_id == FAST_MODBUS_BROADCAST_ID
            and pdu[0] == ModbusFunction.FAST_MODBUS
            and pdu[1] == EVENTS_REQUEST
        ):
            return self._respond_events(pdu)
        slave = self._slaves.get(slave_id)
        if slave is None:
            return None
        answer = slave.handle_pdu(pdu)
        if answer is None:
            return None
        return with_crc(bytes([slave_id]) + answer), slave.response_delay_for(pdu[0])

    # --- Private ---

    def _on_readable(self) -> None:
        assert self._fd is not None
        try:
            data = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            # Every slave fd is closed; a pty master reports that as readable forever.
            asyncio.get_running_loop().remove_reader(self._fd)
            self._queue.put_nowait(None)
            return
        for request in self._framer.feed(data, asyncio.get_running_loop().time()):
            self._queue.put_nowait(request)

    def _respond_events(self, pdu: bytes) -> Optional[tuple[bytes, float]]:
        request = parse_events_request(pdu)
        confirmed = self._slaves.get(request.confirm_slave_id)
        if confirmed is not None:
            confirmed.events.confirm(request.confirm_flag)
        candidates = [
            s
            for s in self._slaves.values()
            if s.slave_id >= request.starting_slave_id and s.events.has_pending()
        ]
        arbitration_s = ARBITRATION_BITS * self._bit_time_s
        if not candidates:
            return with_crc(bytes([FAST_MODBUS_BROADCAST_ID]) + no_events_pdu()), arbitration_s
        winner = min(candidates, key=lambda s: (-int(s.events.priority()), s.slave_id))
        assert winner.events.priority() != EventPriority.DISABLED
        answer = winner.events.build_response(request.max_data_bytes)
        return with_crc(bytes([winner.slave_id]) + answer), arbitration_s

    async def _transmit(self, request: FramedRequest, payload: bytes, delay_s: float) -> None:
        loop = asyncio.get_running_loop()
        start = (
            max(request.received_at + len(request.frame) * self._byte_time_s, self._line_free_at) + delay_s
        )
        chunk = max(1, int(WRITE_CHUNK_S / self._byte_time_s))
        assert self._fd is not None
        for offset in range(0, len(payload), chunk):
            part = payload[offset : offset + chunk]
            due = start + (offset + len(part)) * self._byte_time_s
            wait = due - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            os.write(self._fd, part)
        self._line_free_at = start + len(payload) * self._byte_time_s
