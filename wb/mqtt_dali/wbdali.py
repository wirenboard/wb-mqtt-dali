from __future__ import annotations

import asyncio
import json
import logging
import random
import string
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


WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS = 1000
WAIT_DALI_RESPONSE_TIMEOUT_S = 1.5 * WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS / 1000.0
WAIT_COMMANDS_FOR_BATCH_TIMEOUT_S = 0.01

FRAME_COUNTER_MODULO = 1 << 16


# Maximum number of out-of-order frames `BusMonitorFrameHandler` holds while
# waiting for the gap to close. Bounded by `ring_size - 1` of the gateway's
# 4-slot bus_monitor ring: once the 4th ahead-of-expected frame arrives, the
# slot that would have held the missing earlier frame has been overwritten,
# so it is a real gap rather than an in-flight reorder.
BUS_MONITOR_REORDER_WINDOW = 3


def get_int_payload(message: aiomqtt.Message) -> int:
    if message.payload is None:
        raise ValueError("payload is empty")
    if isinstance(message.payload, (bytes, bytearray)):
        return int(message.payload.decode().strip(), 0)
    if isinstance(message.payload, memoryview):
        return int(message.payload.tobytes().decode().strip(), 0)
    if isinstance(message.payload, str):
        return int(message.payload.strip(), 0)
    return int(message.payload)


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


class BusMonitorFrameHandler:  # pylint: disable=too-few-public-methods
    """Decode and reorder sporadic-frame bus_monitor publications.

    wb-mqtt-serial reads the gateway's 4-slot bus_monitor ring and publishes
    each frame on `bus_<N>_monitor_sporadic_frame_{1..4}`. Reads do not always
    happen in counter order — a frame written to the ring later can be read
    (and published) earlier than its predecessor. We keep an ordered buffer
    of up to `BUS_MONITOR_REORDER_WINDOW` frames whose `frame_counter` is
    ahead of what we expect next, and dispatch them as soon as the gap
    closes. Frames are dispatched to callbacks in strict counter order. A
    warning is emitted when a frame's counter jumps forward beyond the
    reorder window (= a real gap on the wire). A frame whose counter falls
    behind the expected one (already overtaken in the dispatch stream) is
    treated as a gateway anomaly (republished frame or oversized
    wb-mqtt-serial reorder) — it is dropped with a warning rather than
    spliced out of order into the dispatch stream.
    """

    def __init__(
        self,
        bus_traffic: BusTrafficCallbacks,
        logger: logging.Logger,
        dev_inst_map: Optional[DeviceInstanceTypeMapper],
    ) -> None:
        self._next_expected_fc: Optional[int] = None
        self._buffer: dict[int, int] = {}
        self._logger = logger
        self._bus_traffic = bus_traffic
        self._dev_inst_map = dev_inst_map

    def handle(self, message: aiomqtt.Message) -> None:
        if message.retain:
            return

        try:
            raw_value = get_int_payload(message)
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            self._logger.error(
                "Failed to parse bus monitor payload '%s' from topic '%s': %s",
                message.payload,
                message.topic,
                exc,
            )
            return

        fc = (raw_value >> 48) & 0xFFFF

        if self._next_expected_fc is None:
            self._next_expected_fc = (fc + 1) % FRAME_COUNTER_MODULO
            self._bus_traffic_invoke(raw_value)
            return

        # Modular forward distance from expected to received: 0 = right on
        # time; small positive = ahead by a few slots (out-of-order ahead);
        # large positive (near FRAME_COUNTER_MODULO) = arrived behind expected
        # (modular wrap), i.e. a late frame we have already given up on.
        distance = (fc - self._next_expected_fc) % FRAME_COUNTER_MODULO

        if distance == 0:
            self._bus_traffic_invoke(raw_value)
            self._next_expected_fc = self._drain_buffer((fc + 1) % FRAME_COUNTER_MODULO)
            return

        if distance <= BUS_MONITOR_REORDER_WINDOW:
            # Future frame within the reorder window
            self._buffer[fc] = raw_value
            return

        if distance < FRAME_COUNTER_MODULO // 2:
            # Forward jump beyond the reorder window — earlier frames are gone
            advanced_expected = self._flush_buffer_after_gap(self._next_expected_fc)
            if fc != advanced_expected:
                self._logger.warning(
                    "Bus monitor frame counter jump from %d to %d, %d frame(s) missed",
                    (advanced_expected - 1) % FRAME_COUNTER_MODULO,
                    fc,
                    (fc - advanced_expected) % FRAME_COUNTER_MODULO,
                )
            self._bus_traffic_invoke(raw_value)
            self._next_expected_fc = (fc + 1) % FRAME_COUNTER_MODULO
            return

        # Backward jump beyond the reorder window — the gateway misbehaved
        self._logger.warning(
            "Bus monitor frame counter went backwards: fc=%d, expected=%d — dropping",
            fc,
            self._next_expected_fc,
        )

    # --- Private ---

    def _drain_buffer(self, expected: int) -> int:
        while expected in self._buffer:
            raw = self._buffer.pop(expected)
            self._bus_traffic_invoke(raw)
            expected = (expected + 1) % FRAME_COUNTER_MODULO
        return expected

    def _flush_buffer_after_gap(self, expected: int) -> int:
        """Forward jump beyond the window — concede the gap, dispatch all
        buffered frames in counter order, and return the new expected counter
        past them.
        """
        if not self._buffer:
            return expected
        sorted_items = sorted(
            self._buffer.items(),
            key=lambda kv: (kv[0] - expected) % FRAME_COUNTER_MODULO,
        )
        first_fc = sorted_items[0][0]
        missed = (first_fc - expected) % FRAME_COUNTER_MODULO
        if missed > 0:
            self._logger.warning(
                "Bus monitor frame counter jump from %d to %d, %d frame(s) missed",
                (expected - 1) % FRAME_COUNTER_MODULO,
                first_fc,
                missed,
            )
        for _, buf_raw in sorted_items:
            self._bus_traffic_invoke(buf_raw)
        last_fc = sorted_items[-1][0]
        self._buffer.clear()
        return (last_fc + 1) % FRAME_COUNTER_MODULO

    def _bus_traffic_invoke(self, raw_value: int) -> None:
        frame_length = (raw_value >> 32) & 0xFF
        frame_mask = (1 << frame_length) - 1
        frame_data = raw_value & frame_mask
        is_backward = bool((raw_value >> 40) & 0x1)
        is_broken = bool((raw_value >> 41) & 0x1)
        frame_counter = (raw_value >> 48) & 0xFFFF

        if is_broken:
            if is_backward:
                frame = BackwardFrameError(frame_data)
                self._logger.debug("Unexpected broken BF: %s", hex(frame_data))
            else:
                frame = ForwardFrame(frame_length, frame_data)
                frame._error = True  # pylint: disable=protected-access
                self._logger.debug("Unexpected broken FF%d: %s", frame_length, hex(frame_data))
        else:
            if is_backward:
                frame = BackwardFrame(frame_data)
                self._logger.debug("Unexpected BF: %s", hex(frame_data))
            else:
                frame = ForwardFrame(frame_length, frame_data)
                if frame_length in (16, 24):
                    cmd = from_frame(frame, dev_inst_map=self._dev_inst_map)
                    if isinstance(cmd, _Event):
                        self._logger.debug("Event: %s", LazyCommandExpression(cmd))
                    else:
                        self._logger.debug("Unexpected FF%d: %s", frame_length, LazyCommandExpression(cmd))
                else:
                    self._logger.debug("Unexpected FF%d: %s", frame_length, hex(frame_data))
        self._bus_traffic.notify_bus_frame(frame, frame_counter)


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
    def __init__(
        self,
        config: WBDALIConfig,
        mqtt_dispatcher: MQTTDispatcher,
        logger: logging.Logger,
        dev_inst_map: Optional[DeviceInstanceTypeMapper] = None,
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

        client_id_suffix = "".join(random.sample(string.ascii_letters + string.digits, 8))
        self._rpc_client_id = f"{mqtt_dispatcher.client_id.replace('/', '_')}-{client_id_suffix}"
        self._rpc_id_counter = 0

        # The start index in the gateway queue of the current batch being sent
        self._batch_start_index = 0
        self._next_queue_index = 0

        self._bus_monitor_frame_handler = BusMonitorFrameHandler(
            self.bus_traffic, self.logger, self.dev_inst_map
        )

        # The index of the next item to send to the gateway, used for bus monitor tracking
        self._send_queue_item_index = 0

        self._meta_error_topic = f"/devices/{self.config.device_name}/meta/error"
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
    def rpc_client_id(self) -> str:
        return self._rpc_client_id

    @property
    def rpc_id_counter(self) -> int:
        return self._rpc_id_counter

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

        await self._reset_queue_in_gateway()

        # Subscribe to all reply topics
        self.logger.debug("Subscribing to reply topics...")
        for i in range(self.config.queue_size):
            topic = f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_{i}"
            await self._mqtt_dispatcher.subscribe(topic, self._handle_reply_message)

        # Subscribe to FF24 topic
        self.logger.debug("Subscribing to FF24 topic...")
        for i in range(1, 5):
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

        for resp_waiter in self._waiting_for_responses.values():
            resp_waiter.cancel_timeout()
            if not resp_waiter.send_item.future.done():
                resp_waiter.send_item.future.set_result(TransmissionCancelled())
        self._waiting_for_responses.clear()

        # Unsubscribe from all reply topics
        for i in range(self.config.queue_size):
            topic = f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_{i}"
            if self._mqtt_dispatcher.is_running:
                await self._mqtt_dispatcher.unsubscribe(topic)

        # Unsubscribe from FF24 topic
        if self._mqtt_dispatcher.is_running:
            for i in range(1, 5):
                await self._mqtt_dispatcher.unsubscribe(
                    f"/devices/{self.config.device_name}/controls/"
                    f"bus_{self.config.bus}_monitor_sporadic_frame_{i}",
                )
            await self._mqtt_dispatcher.unsubscribe(self._meta_error_topic)
        self.logger.debug("Deinitialized successfully")

    async def send_modbus_rpc_no_response(self, function: int, address: int, count: int, msg: str) -> None:
        """Send a Modbus RPC command without expecting a response."""
        self.logger.debug(
            "Sending Modbus RPC command: function=%d, address=%d, count=%d, msg=%s",
            function,
            address,
            count,
            msg,
        )

        self._rpc_id_counter += 1
        await self._mqtt_dispatcher.client.publish(
            f"/rpc/v1/wb-mqtt-serial/port/Load/{self._rpc_client_id}",
            json.dumps(
                {
                    "params": {
                        "device_id": self.config.device_name,
                        "function": function,
                        "address": address,
                        "count": count,
                        # "response_timeout": 8,
                        "total_timeout": WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS,
                        "frame_timeout": 0,
                        "format": "HEX",
                        "msg": msg,
                    },
                    "id": self.rpc_id_counter,
                }
            ),
        )

    async def _reset_queue_in_gateway(self) -> None:
        self.logger.debug("Resetting message queue in gateway")

        pointer_address = (
            self.config.queue_bulk_send_pointer_modbus_address
            + (self.config.bus - 1) * self.config.queue_modbus_bus_offset
        )

        await self.send_modbus_rpc_no_response(
            function=6,
            address=pointer_address,
            count=1,
            msg="0000",
        )

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

    def _handle_reply_message(  # pylint: disable=too-many-return-statements, too-many-branches, too-many-statements
        self, message: aiomqtt.Message
    ) -> None:
        """Handle reply message from the DALI bus.

        Message payload format:
        [7..0]   - Backward Frame (8 bit)
        [15..8]  - status:
                   0 - no transmission
                   1 - transmission with backward response
                   2 - transmission without response
                   3 - broken response
                   4 - transmission impossible (no power on bus)
                   5 - gateway overheat
        """
        if message.retain:
            self.logger.debug("Received retained message, ignoring...")
            return  # Ignore retained messages

        try:
            resp = get_int_payload(message)
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
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
                    self._response_timeout,
                    timeout_callback,
                )
                self._waiting_for_responses[current_index] = WaitResponseItem(
                    item, timeout_handler, self._send_queue_item_index
                )

                result = encode_frame_for_modbus(item.command.frame, item.command.sendtwice, item.priority)
                regs_32bit.append(result)
                self._send_queue_item_index += 1

            msg = "".join([f"{((reg & 0xFFFF) << 16) | ((reg >> 16) & 0xFFFF):08x}" for reg in regs_32bit])
            buffer_address = (
                self.config.queue_start_modbus_address
                + (self.config.bus - 1) * self.config.queue_modbus_bus_offset
                + start_index * 2
            )
            await self.send_modbus_rpc_no_response(
                function=16,
                address=buffer_address,
                count=len(regs_32bit) * 2,
                msg=msg,
            )

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
        response_futures: list[asyncio.Future] = []
        if lock_queue:
            await self._send_queue_lock.acquire()
        try:
            for cmd, frame_priority in zip(commands_to_send, priorities):
                fut = asyncio.get_running_loop().create_future()
                response_futures.append(fut)
                await self._send_queue.put(SendQueueItem(fut, cmd, source, frame_priority))
        finally:
            if lock_queue:
                self._send_queue_lock.release()

        responses = await asyncio.gather(*response_futures)
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
