from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional, Sequence, Union

import aiomqtt
from dali.command import Command, Response, from_frame
from dali.device.general import DTR0 as DeviceDTR0
from dali.device.general import DTR1 as DeviceDTR1
from dali.device.general import DTR2 as DeviceDTR2
from dali.device.general import _Event
from dali.device.helpers import DeviceInstanceTypeMapper
from dali.frame import BackwardFrame, BackwardFrameError, ForwardFrame, Frame
from dali.gear.general import DTR0 as GearDTR0
from dali.gear.general import DTR1 as GearDTR1
from dali.gear.general import DTR2 as GearDTR2
from dali.gear.general import EnableDeviceType
from dali.sequences import progress as seq_progress
from dali.sequences import sleep as seq_sleep

from .bus_traffic import BusTrafficCallbacks, BusTrafficSource
from .mqtt_dispatcher import MQTTDispatcher, get_str_payload
from .mqtt_rpc_client import MQTTRPCClient, MQTTRPCError, MQTTRPCRequestTimeoutError
from .overheat_rate_limiter import OverheatRateLimiter
from .send_command import LazyCommandExpression, format_command_expression
from .wbdali_error_response import (
    GatewayUnavailable,
    GatewayWriteFailed,
    GatewayWriteSkipped,
    NoPowerOnBus,
    NoResponseFromGateway,
    Overheat,
    TransmissionCancelled,
    TransmissionFailed,
    UnknownResponseStatus,
    WbGatewayTransmissionError,
)


class GatewayMetaErrorPayload(Enum):
    """Values wb-mqtt-serial publishes to a device's /meta/error topic."""

    OK = ""
    UNREACHABLE = "r"


class GatewayCondition(Enum):
    """Bus state reported by the gateway's status of the last answered command."""

    NORMAL = "normal"
    NO_POWER_ON_BUS = "no power on bus"
    OVERHEAT = "overheat"


class GatewayReplyStatus(Enum):
    """Status byte [15..8] of the gateway's reply to a sent frame."""

    UNKNOWN = -1
    NO_TRANSMISSION = 0  # slot written, frame not sent yet: the gateway zeroes the reply on a slot write
    BACKWARD_FRAME = 1
    NO_BACKWARD_FRAME = 2
    BROKEN_BACKWARD_FRAME = 3
    NO_POWER_ON_BUS = 4
    OVERHEAT = 5
    LEGACY_BROKEN_BACKWARD_FRAME = 6  # legacy code, same as BROKEN_BACKWARD_FRAME
    TRANSMISSION_FAILED = 7

    @classmethod
    def _missing_(cls, value):
        return cls.UNKNOWN


class GatewayWriteResult(Enum):
    DONE = "done"
    SKIPPED = "skipped"  # wb-mqtt-serial dropped the write before it reached the port
    FAILED = "failed"  # wb-mqtt-serial answered with an error or did not answer in time


_WRITE_ERROR_BY_RESULT = {
    GatewayWriteResult.SKIPPED: GatewayWriteSkipped,
    GatewayWriteResult.FAILED: GatewayWriteFailed,
}


# Statuses absent here (0, 7 and unknown ones) say nothing about the bus and keep the condition.
_GATEWAY_CONDITION_BY_STATUS = {
    GatewayReplyStatus.BACKWARD_FRAME: GatewayCondition.NORMAL,
    GatewayReplyStatus.NO_BACKWARD_FRAME: GatewayCondition.NORMAL,
    GatewayReplyStatus.BROKEN_BACKWARD_FRAME: GatewayCondition.NORMAL,
    GatewayReplyStatus.NO_POWER_ON_BUS: GatewayCondition.NO_POWER_ON_BUS,
    GatewayReplyStatus.OVERHEAT: GatewayCondition.OVERHEAT,
    GatewayReplyStatus.LEGACY_BROKEN_BACKWARD_FRAME: GatewayCondition.NORMAL,
}


class FramePriority(Enum):
    """DALI forward-frame priority per IEC 62386-103:2022 §9.14.1.

    Selects the multi-master arbitration class for an outgoing forward frame.
    Lower values win arbitration.

    The same numeric priorities are also used for input-device
    "eventPriority": pushbutton defaults to 3 (IEC 62386-301:2017 §9.4.1),
    other instance types default to 4 (IEC 62386-103:2022 §9.14.2).

    The value is the on-wire priority code embedded in the encoded Modbus
    register (bits [31..29]).
    """

    TRANSACTION_CONTINUATION = 1
    USER_ACTION = 2
    CONFIGURATION = 3
    AUTOMATIC = 4
    PERIODIC_QUERY = 5


# pylint: disable=duplicate-code


# Arbitrary: wb-mqtt-serial gives no bound while its port is busy.
WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS = 5000
# Arbitrary and kept generous. Counted from the write ack, it has to cover:
# - the gateway executing every command queued ahead of this one: up to 16 x ~95 ms by the
#   IEC 62386-101 timings (24-bit frame sent twice at the lowest priority, no answer), longer
#   when other masters win the bus; the WB-DALI firmware timings are not measured;
# - wb-mqtt-serial reading the reply event: its port thread does it, with no bound while the port is busy.
# Too short: a command that ran gets NoResponseFromGateway and the gateway queue is reset.
# Too long: only a reply that is really lost is noticed later.
WAIT_DALI_RESPONSE_TIMEOUT_S = 5.0

# wb-mqtt-serial checks total_timeout only when its port is free, so this does not bound the ack.
PORT_LOAD_ACK_TIMEOUT_S = WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS / 1000.0 + 1.0
# wb-mqtt-serial's error for a request dropped by `total_timeout` before it went to the port.
RPC_REQUEST_TIMEOUT_REASON = "RPC request timeout"

WAIT_COMMANDS_FOR_BATCH_TIMEOUT_S = 0.01

FRAME_COUNTER_MODULO = 1 << 16

BUS_MONITOR_RING_SIZE = 4


# Maximum number of out-of-order frames `BusMonitorFrameHandler` holds while
# waiting for the gap to close. Bounded by `ring_size - 1` of the gateway's
# 4-slot bus_monitor ring: once the 4th ahead-of-expected frame arrives, the
# slot that would have held the missing earlier frame has been overwritten,
# so it is a real gap rather than an in-flight reorder.
BUS_MONITOR_REORDER_WINDOW = BUS_MONITOR_RING_SIZE - 1


# Distinct counters landing further behind expected than the ring can hold, after
# which the handler resynchronises to the stream instead of its own position.
BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND = BUS_MONITOR_RING_SIZE


# Frame sizes a DALI bus can carry: FF16 (IEC 62386-102), FF24 (IEC 62386-103),
# FF25 (proprietary), FF17 (proprietary, Helvar), BF8.
FORWARD_FRAME_BIT_LENGTHS = frozenset((16, 17, 24, 25))
BACKWARD_FRAME_BIT_LENGTH = 8

# Forward-frame sizes `dali.command.from_frame` can decode; FF17 and FF25 have no decoding.
DECODABLE_FRAME_BIT_LENGTHS = frozenset((16, 24))

UNREADABLE_FRAME_BIT_LENGTH = 16


def get_int_payload(message: aiomqtt.Message) -> int:
    """Parse a message payload as an integer; every failure comes out as `ValueError`."""
    try:
        if message.payload is None:
            raise ValueError("payload is empty")
        if isinstance(message.payload, (bytes, bytearray)):
            return int(message.payload.decode().strip(), 0)
        if isinstance(message.payload, memoryview):
            return int(message.payload.tobytes().decode().strip(), 0)
        if isinstance(message.payload, str):
            return int(message.payload.strip(), 0)
        return int(message.payload)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"unsupported payload type {type(message.payload).__name__}: {exc}") from exc


@dataclass
class WBDALIConfig:
    """Configuration for WBDALIDriver."""

    device_name: str = "wb-dali_1"

    # DALI bus number, starting from 1 as printed on the gateway label
    bus: int = 1
    queue_size: int = 16
    queue_start_modbus_address: int = 1400
    queue_bulk_send_pointer_modbus_address: int = 1432
    queue_modbus_bus_offset: int = 1000
    # Per-command wait for a DALI reply, counted from the write ack
    response_timeout_s: float = WAIT_DALI_RESPONSE_TIMEOUT_S


@dataclass(frozen=True)
class BusMonitorSlot:
    raw_value: int
    frame_counter: int
    frame_length: int
    frame_data: int
    is_backward: bool
    is_broken: bool

    @classmethod
    def from_raw(cls, message: aiomqtt.Message) -> BusMonitorSlot:
        """Decode a slot publication; raises `ValueError` if the payload is not a slot value."""
        raw_value = get_int_payload(message)
        if raw_value >> 64:
            raise ValueError(f"does not fit the slot register: {raw_value.bit_length()} bit(s)")
        frame_length = (raw_value >> 32) & 0xFF
        return cls(
            raw_value=raw_value,
            frame_counter=(raw_value >> 48) & 0xFFFF,
            frame_length=frame_length,
            frame_data=raw_value & ((1 << frame_length) - 1),
            is_backward=bool((raw_value >> 40) & 0x1),
            is_broken=bool((raw_value >> 41) & 0x1),
        )

    @property
    def is_empty(self) -> bool:
        """Ring slot the gateway has not filled in yet."""
        return self.raw_value == 0

    @property
    def is_valid(self) -> bool:
        return self.has_readable_frame or self.is_broken

    @property
    def has_readable_frame(self) -> bool:
        if self.is_backward:
            return self.frame_length == BACKWARD_FRAME_BIT_LENGTH
        return self.frame_length in FORWARD_FRAME_BIT_LENGTHS

    def build_frame(self) -> Frame:
        if self.is_backward:
            if self.is_broken:
                if self.has_readable_frame:
                    return BackwardFrameError(self.frame_data)
                return BackwardFrameError((1 << BACKWARD_FRAME_BIT_LENGTH) - 1)
            return BackwardFrame(self.frame_data)
        if self.has_readable_frame:
            frame = ForwardFrame(self.frame_length, self.frame_data)
        else:
            frame = ForwardFrame(UNREADABLE_FRAME_BIT_LENGTH, (1 << UNREADABLE_FRAME_BIT_LENGTH) - 1)
        if self.is_broken:
            frame._error = True  # pylint: disable=protected-access
        return frame


class BusMonitorFrameHandler:  # pylint: disable=too-few-public-methods
    """Decode and reorder sporadic-frame bus_monitor publications.

    wb-mqtt-serial reads the gateway's 4-slot ring and publishes each slot on
    `bus_<N>_monitor_sporadic_frame_{1..4}` — in slot order, not counter order, so a
    frame written later can arrive before its predecessor. Frames reach the callbacks
    in counter order, buffered until the gap in front of them closes.

    What an arriving counter can be, relative to the one expected next:

    - ahead by up to `BUS_MONITOR_REORDER_WINDOW`: buffered until its predecessors come.
    - further ahead: the ring overran. Only the counters older than the ring edge are
      reported missed; the frame waits at that edge for its neighbours, which the same
      read pass delivers. Nothing but later traffic releases it.
    - behind: a gateway anomaly — a republished frame, or a reorder wider than the ring.
      Dropped with a warning rather than spliced back into the stream out of order.
    - behind further than the ring can hold, `BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND`
      distinct counters of them (one landing nearer resets the count): the expected
      counter itself is wrong, so the stream resynchronises to the last of them. The
      only case where the dispatched counter restarts lower.

    A slot with no readable frame goes out as an all-ones framing error, its counter
    counting like any other. One that is not `is_valid` gets a warning instead, and past
    the reorder window it is dropped whole. An empty slot is ignored.
    """

    def __init__(
        self,
        bus_traffic: BusTrafficCallbacks,
        logger: logging.Logger,
        dev_inst_map: Optional[DeviceInstanceTypeMapper],
    ) -> None:
        self._next_expected_fc: Optional[int] = None
        self._far_behind_fcs: set[int] = set()
        self._buffer: dict[int, BusMonitorSlot] = {}
        self._logger = logger
        self._bus_traffic = bus_traffic
        self._dev_inst_map = dev_inst_map

    def handle(self, message: aiomqtt.Message) -> None:
        if message.retain or not message.payload:
            return

        try:
            slot = BusMonitorSlot.from_raw(message)
        except ValueError as exc:
            self._logger.error(
                "Failed to parse bus monitor payload '%s' from topic '%s': %s",
                message.payload,
                message.topic,
                exc,
            )
            return

        if slot.is_empty:
            self._logger.debug("Empty bus monitor slot on topic '%s'", message.topic)
            return

        if not slot.is_valid:
            self._logger.warning(
                "Bus monitor slot holds no DALI frame: %d bit(s), raw 0x%016x, fc=%d — dropping",
                slot.frame_length,
                slot.raw_value,
                slot.frame_counter,
            )

        self._order_and_publish(slot)

    # --- Private ---

    def _order_and_publish(self, slot: BusMonitorSlot) -> None:
        if self._next_expected_fc is None:
            if not slot.is_valid:
                return
            self._next_expected_fc = (slot.frame_counter + 1) % FRAME_COUNTER_MODULO
            self._publish([slot])
            return

        # Modular forward distance from expected to received: 0 = right on
        # time; small positive = ahead by a few slots (out-of-order ahead);
        # large positive (near FRAME_COUNTER_MODULO) = arrived behind expected
        # (modular wrap), i.e. a late frame we have already given up on.
        distance = (slot.frame_counter - self._next_expected_fc) % FRAME_COUNTER_MODULO

        if distance == 0:
            self._far_behind_fcs.clear()
            ready = [slot]
            ready.extend(self._take_contiguous((slot.frame_counter + 1) % FRAME_COUNTER_MODULO))
            self._publish(ready)
            return

        if distance <= BUS_MONITOR_REORDER_WINDOW:
            # Future frame within the reorder window
            self._far_behind_fcs.clear()
            self._buffer[slot.frame_counter] = slot
            return

        # Must stay below the window check: an invalid slot still reserves its number
        if not slot.is_valid:
            self._logger.debug(
                "Not trusting frame counter %d of dropped bus monitor slot 0x%016x (expected %d)",
                slot.frame_counter,
                slot.raw_value,
                self._next_expected_fc,
            )
            return

        if distance < FRAME_COUNTER_MODULO // 2:
            # Frames were lost, but only up to the ring edge: this frame's neighbours
            # are still in the ring and come in the same read pass, so it waits for them.
            self._far_behind_fcs.clear()
            self._buffer[slot.frame_counter] = slot
            ring_edge = (slot.frame_counter - BUS_MONITOR_REORDER_WINDOW) % FRAME_COUNTER_MODULO
            conceded_from = self._next_expected_fc
            ready = self._drain_to(ring_edge)
            self._log_counter_jumps(conceded_from, ready, ring_edge)
            ready.extend(self._take_contiguous(ring_edge))
            self._publish(ready)
            return

        self._handle_frame_behind_expected(slot)

    def _handle_frame_behind_expected(self, slot: BusMonitorSlot) -> None:
        if (self._next_expected_fc - slot.frame_counter) % FRAME_COUNTER_MODULO <= BUS_MONITOR_RING_SIZE:
            self._far_behind_fcs.clear()
        else:
            self._far_behind_fcs.add(slot.frame_counter)

        if len(self._far_behind_fcs) < BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND:
            self._logger.warning(
                "Bus monitor frame counter went backwards: fc=%d, expected=%d — dropping",
                slot.frame_counter,
                self._next_expected_fc,
            )
            return

        self._logger.warning(
            "Bus monitor resynchronising to fc=%d: %d frame(s) with distinct counters landed "
            "behind expected %d, so the expected counter itself was wrong",
            slot.frame_counter,
            len(self._far_behind_fcs),
            self._next_expected_fc,
        )
        if self._buffer:
            self._logger.warning(
                "Bus monitor discarding %d buffered frame(s) on resynchronisation: fc=%s",
                len(self._buffer),
                sorted(self._buffer),
            )
        self._far_behind_fcs.clear()
        self._buffer.clear()
        self._next_expected_fc = (slot.frame_counter + 1) % FRAME_COUNTER_MODULO
        self._publish([slot])

    def _take_contiguous(self, expected: int) -> list[BusMonitorSlot]:
        """Take buffered slots that directly follow `expected` and commit the
        counter past them. Slots beyond a gap stay buffered until it closes.
        """
        taken: list[BusMonitorSlot] = []
        while expected in self._buffer:
            taken.append(self._buffer.pop(expected))
            expected = (expected + 1) % FRAME_COUNTER_MODULO
        self._next_expected_fc = expected
        return taken

    def _drain_to(self, limit: int) -> list[BusMonitorSlot]:
        """Take the buffered slots older than `limit` in counter order and park the expected
        position on `limit`. Slots at or past `limit` stay buffered — the ring can still
        deliver their predecessors.
        """
        expected = self._next_expected_fc

        def offset(frame_counter: int) -> int:
            return (frame_counter - expected) % FRAME_COUNTER_MODULO

        ordered = sorted(
            (slot for slot in self._buffer.values() if offset(slot.frame_counter) < offset(limit)),
            key=lambda slot: offset(slot.frame_counter),
        )
        for slot in ordered:
            del self._buffer[slot.frame_counter]
        self._next_expected_fc = limit
        return ordered

    def _log_counter_jumps(self, expected: int, drained: list[BusMonitorSlot], limit: int) -> None:
        """Warn about every counter from `expected` up to `limit` that `drained` does not cover."""
        gap_start = expected
        for gap_end in [slot.frame_counter for slot in drained] + [limit]:
            if gap_end != gap_start:
                self._logger.warning(
                    "Bus monitor frame counter jump from %d to %d, %d frame(s) missed",
                    (gap_start - 1) % FRAME_COUNTER_MODULO,
                    gap_end,
                    (gap_end - gap_start) % FRAME_COUNTER_MODULO,
                )
            gap_start = (gap_end + 1) % FRAME_COUNTER_MODULO

    def _publish(self, slots: list[BusMonitorSlot]) -> None:
        for slot in slots:
            if not slot.is_valid:
                continue  # already reported when it arrived
            try:
                frame = slot.build_frame()
                self._log_frame(frame, slot)
                self._bus_traffic.notify_bus_frame(frame, slot.frame_counter)
            except Exception:  # pylint: disable=broad-exception-caught
                self._logger.exception("Failed to dispatch bus monitor slot 0x%016x", slot.raw_value)

    def _log_frame(self, frame: Frame, slot: BusMonitorSlot) -> None:
        if slot.is_backward or slot.is_broken or slot.frame_length not in DECODABLE_FRAME_BIT_LENGTHS:
            self._logger.debug(
                "Unexpected %s%s: %s",
                "broken " if slot.is_broken else "",
                "BF" if slot.is_backward else f"FF{slot.frame_length}",
                hex(slot.frame_data),
            )
            return
        cmd = from_frame(frame, dev_inst_map=self._dev_inst_map)
        if isinstance(cmd, _Event):
            self._logger.debug("Event: %s", LazyCommandExpression(cmd))
        else:
            self._logger.debug("Unexpected FF%d: %s", slot.frame_length, LazyCommandExpression(cmd))


@dataclass
class SendQueueItem:
    future: asyncio.Future[Response]
    command: Command
    source: BusTrafficSource
    priority: FramePriority
    logger: logging.Logger
    # Taken into a gateway write: the caller's cancellation no longer cancels the command
    sent: bool = False


@dataclass
class WaitResponseItem:
    send_item: SendQueueItem
    sequence_id: int
    timeout_handler: Optional[asyncio.Handle] = None

    def cancel_timeout(self) -> None:
        if self.timeout_handler is not None:
            self.timeout_handler.cancel()


@dataclass
class GatewaySlot:
    waiter: Optional[WaitResponseItem] = None
    # The next reply in this slot is the late one of a timed-out command
    reply_timed_out: bool = False


class GatewayQueueMirror:
    def __init__(self, size: int) -> None:
        self._slots = [GatewaySlot() for _ in range(size)]
        self._write_slot = 0
        self._pointer_reset_needed = False
        self._any_reply_timed_out = False

    @property
    def write_slot(self) -> int:
        return self._write_slot

    @property
    def slots_to_ring_end(self) -> int:
        return len(self._slots) - self._write_slot

    @property
    def pointer_reset_needed(self) -> bool:
        return self._pointer_reset_needed

    @property
    def any_reply_timed_out(self) -> bool:
        return self._any_reply_timed_out

    def commands_in_slots(self) -> list[WaitResponseItem]:
        return [slot.waiter for slot in self._slots if slot.waiter is not None]

    def on_pointer_reset(self) -> None:
        self.clear_slots()
        self._write_slot = 0
        self._pointer_reset_needed = False
        self._any_reply_timed_out = False

    def on_write_acked(self, waiter: WaitResponseItem) -> None:
        """Put the command at the write pointer and advance it."""
        self._slots[self._write_slot] = GatewaySlot(waiter)
        self._write_slot = (self._write_slot + 1) % len(self._slots)

    def is_late_reply(self, slot: int) -> bool:
        """True for the first reply in a slot whose command timed out."""
        if not self._slots[slot].reply_timed_out:
            return False
        self._slots[slot].reply_timed_out = False
        return True

    def command_awaiting_reply(self, slot: int) -> Optional[WaitResponseItem]:
        waiter = self._slots[slot].waiter
        if waiter is None or waiter.send_item.future.done():
            return None
        return waiter

    def on_reply_timeout(self, slot: int) -> None:
        self._slots[slot].reply_timed_out = True
        self._pointer_reset_needed = True
        self._any_reply_timed_out = True

    def request_pointer_reset(self) -> None:
        self._pointer_reset_needed = True

    def clear_slots(self) -> None:
        self._slots = [GatewaySlot() for _ in self._slots]


def encode_frame_for_modbus(dali_frame: Frame, sendtwice: bool, priority: FramePriority) -> int:
    """Encode DALI frame for Modbus transmission.

    Format:
    [24..0]   - frame data, up to 25 bits, right-aligned
    [27..25]  - frame size: 0=FF16, 1=FF24, 2=FF25
    [28]      - send twice flag
    [31..29]  - priority: 0=no send, 1-5=priority level

    Args:
        dali_frame: DALI frame to encode
        sendtwice: Whether to send the frame twice
        priority: Send priority

    Returns:
        Encoded 32-bit value for Modbus register
    """
    frame_len = len(dali_frame)
    frame_int = dali_frame.as_integer

    # Bits [24..0] - frame data, right-aligned
    result = frame_int & 0x1FFFFFF

    # Bits [27..25] - frame size
    if frame_len == 16:
        frame_size = 0
    elif frame_len == 24:
        frame_size = 1
    elif frame_len == 25:
        frame_size = 2
    else:
        raise ValueError(f"Unsupported frame length: {frame_len}")

    result |= (frame_size & 0x7) << 25

    # Bit [28] - send twice
    if sendtwice:
        result |= 1 << 28

    # Bits [31..29] - priority
    result |= (priority.value & 0x7) << 29

    return result


class BusDeviceInstanceTypeMapper(DeviceInstanceTypeMapper):
    def remove_short_address(self, short_address: int) -> None:
        for key in [k for k in self._mapping if k[0] == short_address]:
            self._mapping.pop(key, None)


class WBDALIDriver:  # pylint: disable=too-many-instance-attributes
    def __init__(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        self,
        config: WBDALIConfig,
        mqtt_dispatcher: MQTTDispatcher,
        logger: logging.Logger,
        rpc_client: MQTTRPCClient,
        dev_inst_map: Optional[DeviceInstanceTypeMapper] = None,
    ) -> None:
        self.logger = logger
        self.logger.debug("device=%s, dev_inst_map=%s", config.device_name, dev_inst_map)

        self.config = config
        if self.config.bus not in [1, 2, 3]:
            raise ValueError("Bus number must be 1, 2 or 3")

        self.dev_inst_map = dev_inst_map

        # Register to be called back with bus traffic
        self.bus_traffic = BusTrafficCallbacks(self.config.queue_size)

        self._send_queue: asyncio.Queue[SendQueueItem] = asyncio.Queue(maxsize=self.config.queue_size)

        self._queue_mirror = GatewayQueueMirror(self.config.queue_size)
        self._batch: list[SendQueueItem] = []

        # Lock to ensure only one sender at a time
        self._send_queue_lock = asyncio.Lock()
        self._queue_sender_task: Optional[asyncio.Task] = None

        self._mqtt_dispatcher = mqtt_dispatcher
        self._overheat_rate_limiter = OverheatRateLimiter()
        self._gateway_condition = GatewayCondition.NORMAL

        self._rpc_client = rpc_client

        self._bus_monitor_frame_handler = BusMonitorFrameHandler(
            self.bus_traffic, self.logger, self.dev_inst_map
        )

        # The index of the next item to send to the gateway, used for bus monitor tracking
        self._send_queue_item_index = 0

        self._meta_error_topic = f"/devices/{self.config.device_name}/meta/error"
        # Broker session in which wb-mqtt-serial reported the gateway unreachable; None once withdrawn.
        self._unreachable_reported_in_session: Optional[int] = None
        # Broker session the gateway queue was last synced with; a newer one means a reconnect to catch up.
        self._synced_session = mqtt_dispatcher.session

    @property
    def gateway_unavailable(self) -> bool:
        return self._gateway_reported_unreachable or not self._mqtt_dispatcher.connected

    async def initialize(self) -> None:
        self.logger.debug("Initializing...")

        self._queue_sender_task = asyncio.create_task(self._queue_sender())

        await self._rpc_client.start(self._mqtt_dispatcher)
        self._synced_session = self._mqtt_dispatcher.session
        self._queue_mirror.request_pointer_reset()

        # Subscribe to all reply topics
        self.logger.debug("Subscribing to reply topics...")
        for i in range(self.config.queue_size):
            topic = f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_{i}"
            await self._mqtt_dispatcher.subscribe(topic, self._handle_reply_message)

        # Subscribe to FF24 topic
        self.logger.debug("Subscribing to FF24 topic...")
        for i in range(1, BUS_MONITOR_RING_SIZE + 1):
            await self._mqtt_dispatcher.subscribe(
                f"/devices/{self.config.device_name}/controls/"
                f"bus_{self.config.bus}_monitor_sporadic_frame_{i}",
                self._bus_monitor_frame_handler.handle,
            )

        await self._mqtt_dispatcher.subscribe(self._meta_error_topic, self._handle_meta_error_message)

        self.logger.debug("Initialized successfully")

    async def deinitialize(self) -> None:
        self.logger.debug("Deinitializing...")
        if self._queue_sender_task is not None:
            self._queue_sender_task.cancel()
            try:
                await self._queue_sender_task
            except asyncio.CancelledError:
                # Task cancellation is expected during deinitialization
                pass
            self._queue_sender_task = None

        self._fail_waiters(self._queue_mirror.commands_in_slots(), TransmissionCancelled)
        self._queue_mirror.clear_slots()

        # Unsubscribe from all reply topics
        for i in range(self.config.queue_size):
            topic = f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_{i}"
            await self._mqtt_dispatcher.unsubscribe(topic)

        # Unsubscribe from FF24 topic
        for i in range(1, BUS_MONITOR_RING_SIZE + 1):
            await self._mqtt_dispatcher.unsubscribe(
                f"/devices/{self.config.device_name}/controls/"
                f"bus_{self.config.bus}_monitor_sporadic_frame_{i}",
            )
        await self._mqtt_dispatcher.unsubscribe(self._meta_error_topic)
        await self._rpc_client.stop()
        self.logger.debug("Deinitialized successfully")

    async def send_modbus_rpc(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        self,
        function: int,
        address: int,
        count: int,
        msg: str,
        ensure_configured_device: bool = False,
    ) -> GatewayWriteResult:
        """Write and wait for the ack."""
        self.logger.debug(
            "Sending Modbus RPC command: function=%d, address=%d, count=%d, msg=%s",
            function,
            address,
            count,
            msg,
        )

        params: dict[str, Any] = {
            "device_id": self.config.device_name,
            "function": function,
            "address": address,
            "count": count,
            # "response_timeout": 8,
            "total_timeout": WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS,
            "frame_timeout": 0,
            "format": "HEX",
            "msg": msg,
        }
        if ensure_configured_device:
            params["ensure"] = "configured_device"
        try:
            await self._rpc_client.call("wb-mqtt-serial", "port", "Load", params, PORT_LOAD_ACK_TIMEOUT_S)
        except MQTTRPCError as exc:
            reason = exc.data or exc.rpc_message
            if isinstance(exc, MQTTRPCRequestTimeoutError) and reason == RPC_REQUEST_TIMEOUT_REASON:
                self.logger.debug("Gateway write failed (%s)", reason)
                return GatewayWriteResult.SKIPPED
            self.logger.warning("Gateway write failed (%s)", reason)
            return GatewayWriteResult.FAILED
        return GatewayWriteResult.DONE

    async def _reset_queue_in_gateway(self) -> GatewayWriteResult:
        self.logger.debug("Resetting message queue in gateway")
        self._fail_waiters(self._queue_mirror.commands_in_slots(), NoResponseFromGateway)

        pointer_address = (
            self.config.queue_bulk_send_pointer_modbus_address
            + (self.config.bus - 1) * self.config.queue_modbus_bus_offset
        )

        result = await self.send_modbus_rpc(
            function=6,
            address=pointer_address,
            count=1,
            msg="0000",
            ensure_configured_device=True,
        )
        if result == GatewayWriteResult.DONE:
            if self._queue_mirror.any_reply_timed_out:
                self.logger.warning("Gateway did not reply, queue reset")
            self._queue_mirror.on_pointer_reset()
        return result

    @property
    def _gateway_reported_unreachable(self) -> bool:
        # A report from an older broker session is stale: its withdrawal was lost with that session.
        return self._unreachable_reported_in_session == self._mqtt_dispatcher.session

    def _handle_meta_error_message(self, message: aiomqtt.Message) -> None:
        try:
            payload = get_str_payload(message).strip()
        except (AttributeError, UnicodeDecodeError) as exc:
            self.logger.error("Failed to parse /meta/error payload: %s", exc)
            return

        if payload == GatewayMetaErrorPayload.UNREACHABLE.value:
            should_unavailable = True
        elif payload == GatewayMetaErrorPayload.OK.value:
            should_unavailable = False
        else:
            # Any other code (`p`, `w`, ...) is unrelated to gateway availability.
            self.logger.debug("Ignoring /meta/error payload %r", payload)
            return

        if should_unavailable == self._gateway_reported_unreachable:
            return

        if should_unavailable:
            self.logger.warning("Gateway reported unreachable; failing pending DALI traffic")
            self._unreachable_reported_in_session = self._mqtt_dispatcher.session
            self._fail_waiters(self._queue_mirror.commands_in_slots(), GatewayUnavailable)
            self._queue_mirror.clear_slots()
        else:
            self.logger.info("Gateway reported reachable; queue resync deferred to next batch")
            self._queue_mirror.request_pointer_reset()
            self._unreachable_reported_in_session = None

    def _sync_with_broker_session(self) -> None:
        """Catch up with a broker reconnect: fail pending waiters, schedule the reset."""
        session = self._mqtt_dispatcher.session
        if session == self._synced_session:
            return
        self.logger.info(
            "Broker session %d -> %d; failing pending DALI traffic, queue resync deferred to next batch",
            self._synced_session,
            session,
        )
        self._synced_session = session
        self._fail_waiters(self._queue_mirror.commands_in_slots(), GatewayUnavailable)
        self._queue_mirror.clear_slots()
        self._queue_mirror.request_pointer_reset()

    def _handle_reply_message(  # pylint: disable=too-many-return-statements, too-many-branches, too-many-statements
        self, message: aiomqtt.Message
    ) -> None:
        """Handle reply message from the DALI bus.

        Message payload format:
        [7..0]   - Backward Frame (8 bit)
        [15..8]  - status, see GatewayReplyStatus
        """
        if message.retain:
            self.logger.debug("Received retained message, ignoring...")
            return  # Ignore retained messages
        if not message.payload:
            # wb-mqtt-serial clears every reply topic when it starts.
            self.logger.debug("Dropping empty reply on topic '%s'", message.topic)
            return

        try:
            resp = get_int_payload(message)
        except ValueError as exc:
            self.logger.error(
                "Failed to parse reply payload '%s' from topic '%s': %s",
                message.payload,
                message.topic,
                exc,
            )
            resp = None

        # Process the message as needed
        resp_pointer = int(
            str(message.topic)
            .rsplit("/", maxsplit=1)[-1]
            .replace(f"bus_{self.config.bus}_bulk_send_reply_", "")
        )

        if resp == 0:
            # A direct read between the slot write and its execution, not a reply.
            self.logger.debug("Slot %d is not executed yet, waiting for its reply", resp_pointer)
            return

        # A slot takes a command only after its write ack: anything published before belongs to no command.
        if self._queue_mirror.is_late_reply(resp_pointer):
            self.logger.warning("Gateway reply came after the timeout, slot %d", resp_pointer)
            return
        resp_waiter = self._queue_mirror.command_awaiting_reply(resp_pointer)
        if resp_waiter is None:
            self.logger.debug("Dropping reply in slot %d: no command waits for it", resp_pointer)
            return
        resp_waiter.cancel_timeout()
        resp_future = resp_waiter.send_item.future

        if resp is None:
            # Unparseable payload: fail the waiter now so the caller does not
            # block until the full response timeout.
            response = WbGatewayTransmissionError()
            resp_future.set_result(response)
            self.bus_traffic.notify_command(
                resp_waiter.send_item.command.frame,
                response,
                resp_waiter.send_item.source,
                resp_waiter.sequence_id,
            )
            return

        logger = resp_waiter.send_item.logger
        backward_frame_byte = resp & 0xFF
        status_byte = (resp >> 8) & 0xFF
        status = GatewayReplyStatus(status_byte)

        if status != GatewayReplyStatus.OVERHEAT:
            self._overheat_rate_limiter.on_non_overheat_response()
        self._update_gateway_condition(status)

        if status == GatewayReplyStatus.BACKWARD_FRAME:
            # Transmission with backward response
            logger.debug(
                "%s (%d) status 1: Transmission with backward response, backward_frame=0x%02x",
                LazyCommandExpression(resp_waiter.send_item.command),
                resp_pointer,
                backward_frame_byte,
            )
            if resp_waiter.send_item.command.response is not None:
                response = resp_waiter.send_item.command.response(BackwardFrame(backward_frame_byte))
            else:
                response = Response(BackwardFrame(backward_frame_byte))
            resp_future.set_result(response)
            self.bus_traffic.notify_command(
                resp_waiter.send_item.command.frame,
                response,
                resp_waiter.send_item.source,
                resp_waiter.sequence_id,
            )
            return
        if status == GatewayReplyStatus.NO_BACKWARD_FRAME:
            # Transmission without response
            logger.debug(
                "%s (%d) status 2: Transmission without response",
                LazyCommandExpression(resp_waiter.send_item.command),
                resp_pointer,
            )
            if resp_waiter.send_item.command.response is not None:
                response = resp_waiter.send_item.command.response(None)
            else:
                response = Response(None)
            resp_future.set_result(response)
            self.bus_traffic.notify_command(
                resp_waiter.send_item.command.frame,
                response,
                resp_waiter.send_item.source,
                resp_waiter.sequence_id,
            )
            return
        if status in (
            GatewayReplyStatus.BROKEN_BACKWARD_FRAME,
            GatewayReplyStatus.LEGACY_BROKEN_BACKWARD_FRAME,
        ):
            # Broken response (framing error)
            logger.debug(
                "%s (%d) status %d: Broken response, backward_frame=0x%02x",
                LazyCommandExpression(resp_waiter.send_item.command),
                resp_pointer,
                status_byte,
                backward_frame_byte,
            )
            if resp_waiter.send_item.command.response is not None:
                response = resp_waiter.send_item.command.response(BackwardFrameError(backward_frame_byte))
            else:
                response = Response(BackwardFrameError(backward_frame_byte))
            resp_future.set_result(response)
            self.bus_traffic.notify_command(
                resp_waiter.send_item.command.frame,
                response,
                resp_waiter.send_item.source,
                resp_waiter.sequence_id,
            )
            return
        if status == GatewayReplyStatus.NO_POWER_ON_BUS:
            # Transmission impossible (no power on bus)
            logger.debug(
                "%s (%d) status 4: Transmission impossible - no power on bus",
                LazyCommandExpression(resp_waiter.send_item.command),
                resp_pointer,
            )
            response = NoPowerOnBus()
            resp_future.set_result(response)
            self.bus_traffic.notify_command(
                resp_waiter.send_item.command.frame,
                response,
                resp_waiter.send_item.source,
                resp_waiter.sequence_id,
            )
            return
        if status == GatewayReplyStatus.OVERHEAT:
            # Gateway overheat
            logger.debug(
                "%s (%d) status 5: Gateway overheat",
                LazyCommandExpression(resp_waiter.send_item.command),
                resp_pointer,
            )
            self._overheat_rate_limiter.on_overheat()
            response = Overheat()
            resp_future.set_result(response)
            self.bus_traffic.notify_command(
                resp_waiter.send_item.command.frame,
                response,
                resp_waiter.send_item.source,
                resp_waiter.sequence_id,
            )
            return
        if status == GatewayReplyStatus.TRANSMISSION_FAILED:
            logger.debug(
                "%s (%d) status 7: Transmission failed",
                LazyCommandExpression(resp_waiter.send_item.command),
                resp_pointer,
            )
            response = TransmissionFailed()
            resp_future.set_result(response)
            self.bus_traffic.notify_command(
                resp_waiter.send_item.command.frame,
                response,
                resp_waiter.send_item.source,
                resp_waiter.sequence_id,
            )
            return

        # Unknown status
        logger.debug(
            "%s (%d) unknown status %d, backward_frame=0x%02x, full response=0x%04x",
            LazyCommandExpression(resp_waiter.send_item.command),
            resp_pointer,
            status_byte,
            backward_frame_byte,
            resp,
        )
        response = UnknownResponseStatus()
        resp_future.set_result(response)
        self.bus_traffic.notify_command(
            resp_waiter.send_item.command.frame,
            response,
            resp_waiter.send_item.source,
            resp_waiter.sequence_id,
        )

    def _update_gateway_condition(self, status: GatewayReplyStatus) -> None:
        condition = _GATEWAY_CONDITION_BY_STATUS.get(status)
        if condition is None or condition == self._gateway_condition:
            return
        if self._gateway_condition == GatewayCondition.NO_POWER_ON_BUS:
            self.logger.info("DALI bus power restored")
        elif self._gateway_condition == GatewayCondition.OVERHEAT:
            self.logger.info("DALI gateway overheat is over")
        if condition == GatewayCondition.NO_POWER_ON_BUS:
            self.logger.warning("No power on DALI bus")
        elif condition == GatewayCondition.OVERHEAT:
            self.logger.warning("DALI gateway overheat")
        self._gateway_condition = condition

    async def run_sequence(
        self,
        seq,
        priority: FramePriority,
        logger: logging.Logger,
    ) -> Any:
        """Run a generator-based DALI command sequence.

        All forward frames yielded by the sequence are sent with the given
        ``priority`` (IEC 62386-103:2022 §9.14.1).

        :param seq: A "generator" function to use as a sequence. These are
        available in various places in the python-dali library.
        :param priority: Forward-frame arbitration priority applied to every
        frame emitted by the sequence.
        :param logger: Logger of the sender for the commands' exchange.
        :return: Depends on the sequence being used
        """

        response: Union[Response, List[Response]] = Response(None)
        started = False
        try:
            async with self._send_queue_lock:
                while True:
                    try:
                        # Note that 'send()' here refers to the Python
                        # 'generator' paradigm, not to the DALI driver!
                        if not started:
                            cmd = next(seq)
                            started = True
                        else:
                            cmd = seq.send(response)
                    except StopIteration as r:
                        return r.value
                    response = Response(None)
                    logging.debug("got command from sequence: %s", LazyCommandExpression(cmd))
                    if isinstance(cmd, seq_sleep):
                        await asyncio.sleep(cmd.delay)
                    elif isinstance(cmd, seq_progress):
                        pass
                    elif isinstance(cmd, list):
                        response = await self._send_commands_internal(
                            cmd, BusTrafficSource.WB, priority, False, logger
                        )
                    else:
                        response = (
                            await self._send_commands_internal(
                                [cmd], BusTrafficSource.WB, priority, False, logger
                            )
                        )[0]
        finally:
            seq.close()

    async def send(
        self,
        cmd: Command,
        source: BusTrafficSource,
        priority: FramePriority,
        logger: logging.Logger,
    ) -> Response:
        """Send a single DALI command and optionally wait for a response.
        Args:
            cmd: The DALI command to send.
            source: Source identifier for bus-traffic logging.
            priority: Forward-frame arbitration priority.
            logger: Logger of the sender for the command's exchange.
        Returns:
            Response from the DALI device when ``cmd.response`` is set,
            otherwise ``Response(None)``. Internal transmission errors are
            returned as ``WbGatewayTransmissionError`` or its subclasses.
        """

        return (await self.send_commands([cmd], source, priority, logger))[0]

    async def send_commands(
        self,
        commands: Sequence[Command],
        source: BusTrafficSource,
        priority: FramePriority,
        logger: logging.Logger,
    ) -> List[Response]:
        """Send a sequence of DALI commands as one ordered batch.

        Order is preserved within the batch and the batch is not interleaved
        with other ``send``/``send_commands`` calls. ``priority`` selects
        forward-frame arbitration per IEC 62386-103:2022 §9.14.1 and is
        applied to the leading frame; subsequent frames may be auto-promoted
        to ``TRANSACTION_CONTINUATION`` when they form a protocol-level
        transaction (DTR set followed by a consumer, EnableDeviceType prefix followed by
        a DT command, etc.).

        Args:
            commands: DALI commands to send.
            source: Source identifier for bus-traffic logging.
            priority: Forward-frame arbitration priority for the first frame.
            logger: Logger of the sender for the commands' exchange.
        Returns:
            List of responses aligned with ``commands``: response objects
            when the command has ``response`` set, otherwise
            ``Response(None)``. Internal transmission errors are returned
            as ``WbGatewayTransmissionError`` or its subclasses.
        """

        return await self._send_commands_internal(commands, source, priority, True, logger)

    async def _queue_sender(self) -> None:
        timeout = None
        while True:
            item = None
            try:
                resp_waiter = self._queue_mirror.command_awaiting_reply(
                    self._queue_mirror.write_slot + len(self._batch)
                )
                if resp_waiter is not None:
                    await self._send_to_gateway()
                    await resp_waiter.send_item.future

                try:
                    item = await asyncio.wait_for(self._send_queue.get(), timeout)
                except asyncio.TimeoutError:
                    await self._send_to_gateway()
                    timeout = None
                    continue

                self.logger.debug("Processing queue item: %s", LazyCommandExpression(item.command))
                timeout = WAIT_COMMANDS_FOR_BATCH_TIMEOUT_S

                if item.future.cancelled():
                    self.logger.debug(
                        "Skipping cancelled queue item: %s", LazyCommandExpression(item.command)
                    )
                    continue

                self._batch.append(item)

                # A write is contiguous: the batch ends at the end of the ring.
                if len(self._batch) == self._queue_mirror.slots_to_ring_end:
                    await self._send_to_gateway()

            except Exception as e:  # pylint: disable=broad-exception-caught
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    # No running loop => we're being torn down by GC (coroutine.close()
                    # surfaced a RuntimeError from asyncio internals).
                    # Exit instead of hot-looping.
                    return
                self.logger.error("Error processing queue item: %s", e)
                if item is not None and not item.future.done():
                    item.future.set_result(WbGatewayTransmissionError())

    async def _send_to_gateway(self) -> None:
        """Write the batch at the gateway write pointer."""
        items, self._batch = self._batch, []
        if not items:
            return
        await self._overheat_rate_limiter.wait_before_send()
        self._sync_with_broker_session()
        waiters = []
        for item in items:
            waiters.append(WaitResponseItem(item, self._send_queue_item_index))
            self._send_queue_item_index += 1
        if self.gateway_unavailable:
            self._fail_waiters(waiters, GatewayUnavailable)
            return
        try:
            if self._queue_mirror.pointer_reset_needed:
                reset_result = await self._reset_queue_in_gateway()
                if reset_result != GatewayWriteResult.DONE:
                    self._fail_waiters(waiters, _WRITE_ERROR_BY_RESULT[reset_result])
                    return

            self._fail_waiters(
                [waiter for waiter in waiters if waiter.send_item.future.cancelled()], TransmissionCancelled
            )
            waiters = [waiter for waiter in waiters if not waiter.send_item.future.cancelled()]
            if not waiters:
                return
            for waiter in waiters:
                waiter.send_item.sent = True
            regs_32bit = [
                encode_frame_for_modbus(
                    waiter.send_item.command.frame,
                    waiter.send_item.command.sendtwice,
                    waiter.send_item.priority,
                )
                for waiter in waiters
            ]
            msg = "".join([f"{((reg & 0xFFFF) << 16) | ((reg >> 16) & 0xFFFF):08x}" for reg in regs_32bit])
            buffer_address = (
                self.config.queue_start_modbus_address
                + (self.config.bus - 1) * self.config.queue_modbus_bus_offset
                + self._queue_mirror.write_slot * 2
            )
            result = await self.send_modbus_rpc(
                function=16,
                address=buffer_address,
                count=len(regs_32bit) * 2,
                msg=msg,
            )
            if result != GatewayWriteResult.DONE:
                self._fail_waiters(waiters, _WRITE_ERROR_BY_RESULT[result])
                if result == GatewayWriteResult.FAILED:
                    # The write may have run, and the gateway may still be executing these slots.
                    self._queue_mirror.request_pointer_reset()
                # A skipped write left the gateway pointer here: the next batch takes these slots.
                return
            for waiter in waiters:
                waiter.timeout_handler = asyncio.get_running_loop().call_later(
                    self.config.response_timeout_s,
                    self._handle_reply_timeout,
                    waiter,
                    self._queue_mirror.write_slot,
                )
                self._queue_mirror.on_write_acked(waiter)
        except asyncio.CancelledError:
            self._fail_waiters(waiters, TransmissionCancelled)
            raise
        except Exception:
            self._fail_waiters(waiters, WbGatewayTransmissionError)
            self._queue_mirror.request_pointer_reset()
            raise

    def _handle_reply_timeout(self, waiter: WaitResponseItem, slot: int) -> None:
        if waiter.send_item.future.done():
            return
        self._queue_mirror.on_reply_timeout(slot)
        self._fail_waiters([waiter], NoResponseFromGateway)
        waiter.send_item.logger.debug(
            "Timeout waiting for response %s for queue index %d",
            LazyCommandExpression(waiter.send_item.command),
            slot,
        )

    def _fail_waiters(
        self, waiters: list[WaitResponseItem], response_type: type[WbGatewayTransmissionError]
    ) -> None:
        for waiter in waiters:
            waiter.cancel_timeout()
            future = waiter.send_item.future
            # A cancelled command is still reported: bus traffic goes out in sequence_id order.
            if future.done() and not future.cancelled():
                continue
            response = response_type()
            if not future.cancelled():
                future.set_result(response)
            self.bus_traffic.notify_command(
                waiter.send_item.command.frame, response, waiter.send_item.source, waiter.sequence_id
            )

    # pylint: disable-next=too-many-arguments, too-many-positional-arguments, too-many-locals, too-many-branches
    async def _send_commands_internal(
        self,
        commands: Sequence[Command],
        source: BusTrafficSource,
        priority: FramePriority,
        lock_queue: bool,
        logger: logging.Logger,
    ) -> list[Response]:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("send: %s", ", ".join(format_command_expression(cmd) for cmd in commands))
        if self.gateway_unavailable:
            # Synthesise bus-traffic so listeners see the dropped frames just like real errors.
            for cmd in commands:
                response = GatewayUnavailable()
                self.bus_traffic.notify_command(cmd.frame, response, source, self._send_queue_item_index)
                self._send_queue_item_index += 1
            return [GatewayUnavailable() for _ in commands]
        commands_to_send = []
        for cmd in commands:
            if cmd.devicetype != 0:
                commands_to_send.append(EnableDeviceType(cmd.devicetype))
            commands_to_send.append(cmd)
        priorities = _compute_frame_priorities(commands_to_send, priority)
        items: list[SendQueueItem] = []
        try:
            if lock_queue:
                await self._send_queue_lock.acquire()
            try:
                for cmd, frame_priority in zip(commands_to_send, priorities):
                    item = SendQueueItem(
                        asyncio.get_running_loop().create_future(), cmd, source, frame_priority, logger
                    )
                    items.append(item)
                    await self._send_queue.put(item)
            finally:
                if lock_queue:
                    self._send_queue_lock.release()

            responses = await asyncio.gather(*(asyncio.shield(item.future) for item in items))
        except asyncio.CancelledError:
            for item in items:
                if not item.sent:
                    item.future.cancel()
            raise
        filtered_responses: list[Response] = []
        i = 0
        for cmd in commands:
            # Skip additional EnableDeviceType commands
            if cmd.devicetype != 0:
                i += 1
            filtered_responses.append(responses[i])
            i += 1

        return filtered_responses


def _is_dtr_set(cmd: Command) -> bool:
    return isinstance(cmd, (GearDTR0, GearDTR1, GearDTR2, DeviceDTR0, DeviceDTR1, DeviceDTR2))


def _uses_dtr(cmd: Command) -> bool:
    return (
        getattr(cmd, "uses_dtr0", False)
        or getattr(cmd, "uses_dtr1", False)
        or getattr(cmd, "uses_dtr2", False)
    )


def _compute_frame_priorities(
    commands: Sequence[Command], caller_priority: FramePriority
) -> list[FramePriority]:
    """Apply IEC 62386-103:2022 §9.14.1 transaction-continuation auto-promotion.

    The first frame keeps the caller's priority. A subsequent frame is promoted
    to ``TRANSACTION_CONTINUATION`` when it forms a protocol-level transaction
    with the previous one:

    - the previous frame is a ``DTR0`` / ``DTR1`` / ``DTR2`` set, **or**
    - the previous frame is an ``EnableDeviceType`` prefix, **or**
    - the current frame is a DTR consumer (``uses_dtr*``) — but not a DTR set
      itself: a fresh DTR set starts a new segment, not continues one.
    """
    if not commands:
        return []
    result = [caller_priority]
    for i in range(1, len(commands)):
        prev = commands[i - 1]
        curr = commands[i]
        if (
            _is_dtr_set(prev)
            or isinstance(prev, EnableDeviceType)
            or (_uses_dtr(curr) and not _is_dtr_set(curr))
        ):
            result.append(FramePriority.TRANSACTION_CONTINUATION)
        else:
            result.append(caller_priority)
    return result
