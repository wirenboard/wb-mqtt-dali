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
from .gateway_link import GatewayLink, MqttSerialLink
from .mqtt_dispatcher import (  # noqa: F401
    MQTTDispatcher,
    get_int_payload,
    get_str_payload,
)
from .overheat_rate_limiter import OverheatRateLimiter
from .send_command import LazyCommandExpression, format_command_expression
from .wbdali_error_response import (
    GatewayUnavailable,
    NoPowerOnBus,
    NoResponseFromGateway,
    NoTransmission,
    Overheat,
    TransmissionCancelled,
    UnknownResponseStatus,
    WbGatewayTransmissionError,
)


class GatewayMetaErrorPayload(Enum):
    """Values wb-mqtt-serial publishes to a device's /meta/error topic."""

    OK = ""
    UNREACHABLE = "r"


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


from .gateway_link import (  # noqa: E402  # pylint: disable=wrong-import-position
    WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS,
)

WAIT_DALI_RESPONSE_TIMEOUT_S = 1.5 * WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS / 1000.0
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
# FF25 (proprietary), BF8.
FORWARD_FRAME_BIT_LENGTHS = frozenset((16, 24, 25))
BACKWARD_FRAME_BIT_LENGTH = 8

# Forward-frame sizes `dali.command.from_frame` can decode; FF25 has no decoding.
DECODABLE_FRAME_BIT_LENGTHS = frozenset((16, 24))

UNREADABLE_FRAME_BIT_LENGTH = 16


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
        return cls.from_value(get_int_payload(message))

    @classmethod
    def from_value(cls, raw_value: int) -> BusMonitorSlot:
        """Decode a slot register value; raises `ValueError` if it does not fit the register."""
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
            raw = get_int_payload(message)
        except ValueError as exc:
            self._logger.error(
                "Failed to parse bus monitor payload '%s' from topic '%s': %s",
                message.payload,
                message.topic,
                exc,
            )
            return
        self.handle_raw(raw)

    def handle_raw(self, raw: int) -> None:
        """One ring slot's value, however it reached us — a publication or a register read."""
        try:
            slot = BusMonitorSlot.from_value(raw)
        except ValueError as exc:
            self._logger.error("Bus monitor value 0x%x is not a slot: %s", raw, exc)
            return

        if slot.is_empty:
            self._logger.debug("Empty bus monitor slot")
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


@dataclass
class WaitResponseItem:
    send_item: SendQueueItem
    timeout_handler: asyncio.Handle
    sequence_id: int

    def cancel_timeout(self) -> None:
        self.timeout_handler.cancel()


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


class WBDALIDriver:  # pylint: disable=too-many-instance-attributes
    """One DALI bus of one WB-DALI module.

    The DALI side — queueing, matching answers to frames, decoding them,
    sequences — lives here; the module is reached through a
    :class:`~wb.mqtt_dali.gateway_link.GatewayLink`. With no ``link`` given the
    controller's is built: wb-mqtt-serial in between, over ``mqtt_dispatcher``.
    """

    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        config: WBDALIConfig,
        mqtt_dispatcher: Optional[MQTTDispatcher],
        logger: logging.Logger,
        dev_inst_map: Optional[DeviceInstanceTypeMapper] = None,
        link: Optional[GatewayLink] = None,
    ) -> None:
        self.logger = logger.getChild(f"{config.device_name}_bus{config.bus}")
        self.logger.debug("device=%s, dev_inst_map=%s", config.device_name, dev_inst_map)

        self.config = config
        if self.config.bus not in [1, 2, 3]:
            raise ValueError("Bus number must be 1, 2 or 3")

        self.dev_inst_map = dev_inst_map

        # Register to be called back with bus traffic
        self.bus_traffic = BusTrafficCallbacks(self.config.queue_size)

        self._send_queue: asyncio.Queue[SendQueueItem] = asyncio.Queue(maxsize=self.config.queue_size)

        self._waiting_for_responses: dict[int, WaitResponseItem] = {}

        # Lock to ensure only one sender at a time
        self._send_queue_lock = asyncio.Lock()
        self._queue_sender_task: Optional[asyncio.Task] = None

        self._mqtt_dispatcher = mqtt_dispatcher
        self._overheat_rate_limiter = OverheatRateLimiter()

        if link is None:
            if mqtt_dispatcher is None:
                raise ValueError("WBDALIDriver needs a gateway link or an MQTT dispatcher to build one")
            link = MqttSerialLink(config, mqtt_dispatcher, self.logger)
        self._link = link

        # The start index in the gateway queue of the current batch being sent
        self._batch_start_index = 0
        self._next_queue_index = 0

        self._bus_monitor_frame_handler = BusMonitorFrameHandler(
            self.bus_traffic, self.logger, self.dev_inst_map
        )

        # The index of the next item to send to the gateway, used for bus monitor tracking
        self._send_queue_item_index = 0

        self._gateway_unavailable = False
        self._pending_resync = False

        self._response_timeout = WAIT_DALI_RESPONSE_TIMEOUT_S

    @property
    def response_timeout(self) -> float:
        """Per-command timeout for waiting on a DALI response from the gateway, in seconds.

        Applied when the command is dispatched in `_send_to_gateway`: items
        already in-flight keep the timeout they were scheduled with.
        """
        return self._response_timeout

    @response_timeout.setter
    def response_timeout(self, timeout: float) -> None:
        self._response_timeout = timeout

    @property
    def link(self) -> GatewayLink:
        return self._link

    @property
    def rpc_client_id(self) -> str:
        """The MQTT link's RPC client id; empty for a link that speaks no RPC."""
        return getattr(self._link, "rpc_client_id", "")

    @property
    def rpc_id_counter(self) -> int:
        return getattr(self._link, "rpc_id_counter", 0)

    @property
    def batch_start_index(self) -> int:
        """Get the start index in the gateway queue of the current batch being sent."""
        return self._batch_start_index

    @property
    def gateway_unavailable(self) -> bool:
        """True while wb-mqtt-serial reports the gateway device as unreachable (`r`)."""
        return self._gateway_unavailable

    async def initialize(self) -> None:
        self.logger.debug("Initializing...")
        self._queue_sender_task = asyncio.create_task(self._queue_sender())
        await self._link.resync()
        await self._link.start(self)
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

        for resp_waiter in self._waiting_for_responses.values():
            resp_waiter.cancel_timeout()
            if not resp_waiter.send_item.future.done():
                resp_waiter.send_item.future.set_result(TransmissionCancelled())
        self._waiting_for_responses.clear()

        await self._link.stop()
        self.logger.debug("Deinitialized successfully")

    def set_bus_monitor_enabled(self, enabled: bool) -> None:
        """Tell the link whether the bus monitor is being watched."""
        self._link.set_bus_monitor_enabled(enabled)

    def set_has_control_devices(self, present: bool) -> None:
        """Tell the link whether DALI-2 control devices are configured on this bus."""
        self._link.set_has_control_devices(present)

    async def send_modbus_rpc_no_response(self, function: int, address: int, count: int, msg: str) -> None:
        """Send a Modbus RPC command through the MQTT link (kept for callers that speak RPC)."""
        link = self._link  # the MQTT link; a register link has no RPC to speak
        await link.send_modbus_rpc_no_response(function, address, count, msg)  # type: ignore[attr-defined]

    async def _reset_queue_in_gateway(self) -> None:
        await self._link.resync()

    def _handle_meta_error_message(self, message: aiomqtt.Message) -> None:
        try:
            payload = get_str_payload(message).strip()
        except (AttributeError, UnicodeDecodeError) as exc:
            self.logger.error("Failed to parse /meta/error payload: %s", exc)
            return
        self.on_gateway_error_payload(payload)

    def on_gateway_error_payload(self, payload: str) -> None:
        """wb-mqtt-serial's `/meta/error` for the module: `r` fails pending traffic, `` resyncs."""
        if payload == GatewayMetaErrorPayload.UNREACHABLE.value:
            should_unavailable = True
        elif payload == GatewayMetaErrorPayload.OK.value:
            should_unavailable = False
        else:
            # Any other code (`p`, `w`, ...) is unrelated to gateway availability.
            self.logger.debug("Ignoring /meta/error payload %r", payload)
            return

        if should_unavailable == self._gateway_unavailable:
            return

        if should_unavailable:
            self.logger.warning("Gateway reported unreachable; failing pending DALI traffic")
            self._gateway_unavailable = True
            self._drain_pending_with_gateway_unavailable()
        else:
            self.logger.info("Gateway reported reachable; queue resync deferred to next batch")
            self._reset_queue_state_locally()
            self._pending_resync = True
            self._gateway_unavailable = False

    def _drain_pending_with_gateway_unavailable(self) -> None:
        # Resolve in-flight waiters whose timeout handlers would otherwise fire
        # later and replace our GatewayUnavailable response with NoResponseFromGateway.
        for resp_waiter in list(self._waiting_for_responses.values()):
            resp_waiter.cancel_timeout()
            if not resp_waiter.send_item.future.done():
                response = GatewayUnavailable()
                resp_waiter.send_item.future.set_result(response)
                self.bus_traffic.notify_command(
                    resp_waiter.send_item.command.frame,
                    response,
                    resp_waiter.send_item.source,
                    resp_waiter.sequence_id,
                )
        self._waiting_for_responses.clear()

    def _reset_queue_state_locally(self) -> None:
        self._next_queue_index = 0
        self._batch_start_index = 0
        self._waiting_for_responses.clear()

    def _fail_batch_gateway_unavailable(self, items: list[SendQueueItem]) -> None:
        for item in items:
            if not item.future.done():
                response = GatewayUnavailable()
                item.future.set_result(response)
                self.bus_traffic.notify_command(
                    item.command.frame,
                    response,
                    item.source,
                    self._send_queue_item_index,
                )
                self._send_queue_item_index += 1

    def _handle_reply_message(self, message: aiomqtt.Message) -> None:
        """A reply register publication from wb-mqtt-serial (see the MQTT link)."""
        if message.retain:
            self.logger.debug("Received retained message, ignoring...")
            return  # Ignore retained messages

        try:
            resp: Optional[int] = get_int_payload(message)
        except ValueError as exc:
            self.logger.error(
                "Failed to parse reply payload '%s' from topic '%s': %s",
                message.payload,
                message.topic,
                exc,
            )
            resp = None

        resp_pointer = int(
            str(message.topic)
            .rsplit("/", maxsplit=1)[-1]
            .replace(f"bus_{self.config.bus}_bulk_send_reply_", "")
        )
        self.on_reply(resp_pointer, resp)

    def on_monitor_slot(self, raw: int) -> None:
        """A bus monitor ring slot from the link."""
        self._bus_monitor_frame_handler.handle_raw(raw)

    def on_reply(  # pylint: disable=too-many-return-statements, too-many-branches, too-many-statements
        self, resp_pointer: int, resp: Optional[int]
    ) -> None:
        """The reply register of queue index ``resp_pointer`` holds ``resp``.

        Reply register format:
        [7..0]   - Backward Frame (8 bit)
        [15..8]  - status:
                   0 - no transmission
                   1 - transmission with backward response
                   2 - transmission without response
                   3 - broken response
                   4 - transmission impossible (no power on bus)
                   5 - gateway overheat
        ``None`` is an unreadable publication.
        """
        resp_waiter = self._waiting_for_responses.get(resp_pointer)
        if resp_waiter is None:
            self.logger.warning("Received response for unknown pointer: %d", resp_pointer)
            return
        resp_waiter.cancel_timeout()
        resp_future = resp_waiter.send_item.future
        if resp_future.done():
            self.logger.debug("Response future already done for pointer: %d", resp_pointer)
            return

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

        backward_frame_byte = resp & 0xFF
        status = (resp >> 8) & 0xFF

        if status != 5:
            self._overheat_rate_limiter.on_non_overheat_response()

        if status == 0:
            # No transmission
            self.logger.debug(
                "%s (%d) status 0: No transmission",
                LazyCommandExpression(resp_waiter.send_item.command),
                resp_pointer,
            )
            response = NoTransmission()
            resp_future.set_result(response)
            self.bus_traffic.notify_command(
                resp_waiter.send_item.command.frame,
                response,
                resp_waiter.send_item.source,
                resp_waiter.sequence_id,
            )
            return
        if status == 1:
            # Transmission with backward response
            self.logger.debug(
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
        if status == 2:
            # Transmission without response
            self.logger.debug(
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
        if status == 3:
            # Broken response (framing error)
            self.logger.error(
                "%s (%d) status 3: Broken response, backward_frame=0x%02x",
                LazyCommandExpression(resp_waiter.send_item.command),
                resp_pointer,
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
        if status == 4:
            # Transmission impossible (no power on bus)
            self.logger.error(
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
        if status == 5:
            # Gateway overheat
            self.logger.error(
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

        # Unknown status
        self.logger.error(
            "%s (%d) unknown status %d, backward_frame=0x%02x, full response=0x%04x",
            LazyCommandExpression(resp_waiter.send_item.command),
            resp_pointer,
            status,
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

    async def run_sequence(
        self,
        seq,
        priority: FramePriority = FramePriority.USER_ACTION,
        progress=None,
    ) -> Any:
        """Run a generator-based DALI command sequence.

        All forward frames yielded by the sequence are sent with the given
        ``priority`` (IEC 62386-103:2022 §9.14.1).

        :param seq: A "generator" function to use as a sequence. These are
        available in various places in the python-dali library.
        :param priority: Forward-frame arbitration priority applied to every
        frame emitted by the sequence.
        :param progress: A function to call with progress updates, used by
        some sequences to provide status information. The function must
        accept a single argument. A suitable example is `progress=print` to
        use the built-in `print()` function.
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
                        if progress:
                            progress(cmd)
                    elif isinstance(cmd, list):
                        response = await self._send_commands_internal(
                            cmd, BusTrafficSource.WB, priority, lock_queue=False
                        )
                    else:
                        response = (
                            await self._send_commands_internal(
                                [cmd], BusTrafficSource.WB, priority, lock_queue=False
                            )
                        )[0]
        finally:
            seq.close()

    async def send(
        self,
        cmd: Command,
        source: BusTrafficSource = BusTrafficSource.WB,
        priority: FramePriority = FramePriority.USER_ACTION,
    ) -> Response:
        """Send a single DALI command and optionally wait for a response.
        Args:
            cmd: The DALI command to send.
            source: Source identifier for bus-traffic logging.
            priority: Forward-frame arbitration priority.
        Returns:
            Response from the DALI device when ``cmd.response`` is set,
            otherwise ``Response(None)``. Internal transmission errors are
            returned as ``WbGatewayTransmissionError`` or its subclasses.
        """

        return (await self.send_commands([cmd], source, priority))[0]

    async def send_commands(
        self,
        commands: Sequence[Command],
        source: BusTrafficSource = BusTrafficSource.WB,
        priority: FramePriority = FramePriority.USER_ACTION,
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
        Returns:
            List of responses aligned with ``commands``: response objects
            when the command has ``response`` set, otherwise
            ``Response(None)``. Internal transmission errors are returned
            as ``WbGatewayTransmissionError`` or its subclasses.
        """

        return await self._send_commands_internal(commands, source, priority, lock_queue=True)

    # pylint: disable-next=too-many-return-statements, too-many-branches, too-many-statements
    async def _queue_sender(self) -> None:
        batch: list[SendQueueItem] = []
        timeout = None
        while True:
            item = None
            try:
                resp_waiter = self._waiting_for_responses.get(self._next_queue_index)
                if resp_waiter is not None and not resp_waiter.send_item.future.done():
                    try:
                        await self._send_to_gateway(batch, self._batch_start_index)
                    finally:
                        batch = []
                        self._batch_start_index = self._next_queue_index
                    try:
                        await resp_waiter.send_item.future
                    except asyncio.CancelledError:
                        if not resp_waiter.send_item.future.cancelled():
                            raise

                try:
                    item = await asyncio.wait_for(self._send_queue.get(), timeout)
                except asyncio.TimeoutError:
                    # Not `continue` inside the `finally`: that discards whatever
                    # the send raised — including the cancellation deinitialize()
                    # sends while a batch is still in flight on a slow link, which
                    # left the sender running and deinitialize() waiting forever.
                    try:
                        await self._send_to_gateway(batch, self._batch_start_index)
                    finally:
                        batch = []
                        self._batch_start_index = self._next_queue_index
                        timeout = None
                    continue

                self.logger.debug("Processing queue item: %s", LazyCommandExpression(item.command))
                timeout = WAIT_COMMANDS_FOR_BATCH_TIMEOUT_S

                if item.future.cancelled():
                    self.logger.debug(
                        "Skipping cancelled queue item: %s", LazyCommandExpression(item.command)
                    )
                    continue

                batch.append(item)
                self._next_queue_index += 1

                if self._next_queue_index == self.config.queue_size:
                    try:
                        await self._send_to_gateway(batch, self._batch_start_index)
                    finally:
                        batch = []
                        self._batch_start_index = 0
                        self._next_queue_index = 0

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

    async def _send_to_gateway(self, items: list[SendQueueItem], start_index: int) -> None:
        if len(items) > 0:
            await self._overheat_rate_limiter.wait_before_send()
            if self._gateway_unavailable:
                self._fail_batch_gateway_unavailable(items)
                return
            if self._pending_resync:
                await self._reset_queue_in_gateway()
                self._pending_resync = False
            regs_32bit = []
            for current_index, item in enumerate(items, start_index):

                def timeout_callback(index=current_index):
                    waiter_to_clear = self._waiting_for_responses.get(index)
                    if waiter_to_clear is not None and not waiter_to_clear.send_item.future.done():
                        response = NoResponseFromGateway()
                        waiter_to_clear.send_item.future.set_result(response)
                        self.bus_traffic.notify_command(
                            waiter_to_clear.send_item.command.frame,
                            response,
                            waiter_to_clear.send_item.source,
                            waiter_to_clear.sequence_id,
                        )
                        self.logger.error(
                            "Timeout waiting for response %s for queue index %d",
                            LazyCommandExpression(waiter_to_clear.send_item.command),
                            index,
                        )

                timeout_handler = asyncio.get_running_loop().call_later(
                    self._link.reply_timeout(len(items), self._response_timeout),
                    timeout_callback,
                )
                self._waiting_for_responses[current_index] = WaitResponseItem(
                    item, timeout_handler, self._send_queue_item_index
                )

                result = encode_frame_for_modbus(item.command.frame, item.command.sendtwice, item.priority)
                regs_32bit.append(result)
                self._send_queue_item_index += 1

            await self._link.send_slots(start_index, regs_32bit, self._response_timeout)

    async def _send_commands_internal(
        self,
        commands: Sequence[Command],
        source: BusTrafficSource,
        priority: FramePriority,
        lock_queue: bool,
    ) -> list[Response]:
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug("send: %s", ", ".join(format_command_expression(cmd) for cmd in commands))
        if self._gateway_unavailable:
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
        responses = await self._send_expanded(commands_to_send, priorities, source, lock_queue)
        filtered_responses: list[Response] = []
        i = 0
        for cmd in commands:
            # Skip additional EnableDeviceType commands
            if cmd.devicetype != 0:
                i += 1
            filtered_responses.append(responses[i])
            i += 1

        return filtered_responses

    async def _send_expanded(
        self,
        commands: Sequence[Command],
        priorities: Sequence[FramePriority],
        source: BusTrafficSource,
        lock_queue: bool,
    ) -> list[Response]:
        """Queue every frame and wait for its answer; the wire, as opposed to the memo."""
        response_futures: list[asyncio.Future] = []
        if lock_queue:
            await self._send_queue_lock.acquire()
        try:
            for cmd, frame_priority in zip(commands, priorities):
                fut = asyncio.get_running_loop().create_future()
                response_futures.append(fut)
                await self._send_queue.put(SendQueueItem(fut, cmd, source, frame_priority))
        finally:
            if lock_queue:
                self._send_queue_lock.release()
        return await asyncio.gather(*response_futures)


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
