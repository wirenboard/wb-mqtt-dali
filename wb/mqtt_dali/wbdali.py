from __future__ import annotations

import asyncio
import json
import logging
import random
import string
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Union

import paho.mqtt.client as mqtt
from dali.command import Command, Response, from_frame
from dali.device.general import _Event
from dali.device.helpers import DeviceInstanceTypeMapper
from dali.frame import BackwardFrame, BackwardFrameError, ForwardFrame, Frame
from dali.gear.general import EnableDeviceType
from dali.sequences import progress as seq_progress
from dali.sequences import sleep as seq_sleep

from .bus_traffic import BusTrafficCallbacks, BusTrafficSource
from .mqtt_dispatcher import MQTTDispatcher
from .overheat_rate_limiter import OverheatRateLimiter
from .wbdali_error_response import (
    NoPowerOnBus,
    NoResponseFromGateway,
    NoTransmission,
    Overheat,
    TransmissionCancelled,
    UnknownResponseStatus,
    WbGatewayTransmissionError,
)

# pylint: disable=duplicate-code


WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS = 1000
WAIT_DALI_RESPONSE_TIMEOUT_S = 1.5 * WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS / 1000.0
WAIT_COMMANDS_FOR_BATCH_TIMEOUT_S = 0.01

FRAME_COUNTER_MODULO = 1 << 16


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


class BusMonitorFrameHandler:
    def __init__(
        self,
        bus_traffic: BusTrafficCallbacks,
        logger: logging.Logger,
        dev_inst_map: Optional[DeviceInstanceTypeMapper],
    ) -> None:
        self._last_frame_counter: Optional[int] = None
        self._out_of_order_frame: Optional[int] = None
        self._logger = logger
        self._bus_traffic = bus_traffic
        self._dev_inst_map = dev_inst_map

    async def handle(self, message: mqtt.MQTTMessage) -> None:
        if message.retain:
            return

        try:
            payload_str = message.payload.decode().strip()
            raw_value = int(payload_str, 0)
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            self._logger.error(
                "Failed to parse bus monitor payload '%s' from topic '%s': %s",
                message.payload,
                message.topic,
                exc,
            )
            return

        frame_counter = (raw_value >> 48) & 0xFFFF

        if self._last_frame_counter is None:
            self._last_frame_counter = frame_counter
            await self._bus_traffic_invoke(raw_value)
            return

        delta = self.get_frame_counter_delta(self._last_frame_counter, frame_counter)
        if delta == 2 and self._out_of_order_frame is None:
            # Allow one backward jump without logging a warning, to handle the case
            # when the gateway queue jumps from 4th to 1st item
            # N -> N+2 -> N+1 -> N+3 -> N+4
            self._out_of_order_frame = raw_value
            self._last_frame_counter = frame_counter
            return

        if delta == -1 and self._out_of_order_frame is not None:
            try:
                await self._bus_traffic_invoke(raw_value)
                await self._bus_traffic_invoke(self._out_of_order_frame)
                return
            finally:
                self._out_of_order_frame = None

        if delta != 1:
            self._logger.warning(
                "Bus monitor frame counter jump from %d to %d, possible missed frames",
                self._last_frame_counter,
                frame_counter,
            )

        if self._out_of_order_frame is not None:
            try:
                await self._bus_traffic_invoke(self._out_of_order_frame)
            finally:
                self._out_of_order_frame = None

        self._last_frame_counter = frame_counter
        await self._bus_traffic_invoke(raw_value)

    def get_frame_counter_delta(self, start: int, end: int) -> int:
        # It is ok to have a backward jump to one frame,
        # that can happen when the gateway queue jumps from 4th to 1st item.
        # Jumps for more than one frame are not expected and likely indicate missed frames
        if (start == 0 and end == FRAME_COUNTER_MODULO - 1) or (start - end == 1):
            return -1
        if end >= start:
            return end - start
        return end + FRAME_COUNTER_MODULO - start

    async def _bus_traffic_invoke(self, raw_value: int) -> None:
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
                        self._logger.debug("Event: %s", cmd)
                    else:
                        self._logger.debug("Unexpected FF%d: %s", frame_length, cmd)
                else:
                    self._logger.debug("Unexpected FF%d: %s", frame_length, hex(frame_data))
        self._bus_traffic.notify_bus_frame(frame, frame_counter)


@dataclass
class SendQueueItem:
    future: asyncio.Future[Response]
    command: Command
    source: BusTrafficSource


@dataclass
class WaitResponseItem:
    send_item: SendQueueItem
    timeout_handler: asyncio.Handle
    sequence_id: int

    def cancel_timeout(self) -> None:
        self.timeout_handler.cancel()


def encode_frame_for_modbus(dali_frame: Frame, sendtwice: bool = False, priority: int = 4) -> int:
    """Encode DALI frame for Modbus transmission.

    Format:
    [24..0]   - frame data, up to 25 bits, right-aligned
    [27..25]  - frame size: 0=FF16, 1=FF24, 2=FF25
    [28]      - send twice flag
    [31..29]  - priority: 0=no send, 1-5=priority level

    Args:
        dali_frame: DALI frame to encode
        sendtwice: Whether to send the frame twice
        priority: Send priority (0=no send, 1-5=priority level)

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

    # Bits [31..29] - priority (0-5)
    if priority < 0 or priority > 5:
        raise ValueError(f"Priority must be 0-5, got {priority}")
    result |= (priority & 0x7) << 29

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

    async def _handle_reply_message(  # pylint: disable=too-many-return-statements, too-many-branches, too-many-statements
        self, message: mqtt.MQTTMessage
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
        resp = int(message.payload.decode())

        if message.retain:
            self.logger.debug("Received retained message, ignoring...")
            return  # Ignore retained messages

        backward_frame_byte = resp & 0xFF
        status = (resp >> 8) & 0xFF

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

        if status != 5:
            self._overheat_rate_limiter.on_non_overheat_response()

        if status == 0:
            # No transmission
            self.logger.debug(
                "%s (%d) status 0: No transmission", resp_waiter.send_item.command, resp_pointer
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
                resp_waiter.send_item.command,
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
                "%s (%d) status 2: Transmission without response", resp_waiter.send_item.command, resp_pointer
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
                resp_waiter.send_item.command,
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
                resp_waiter.send_item.command,
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
                resp_waiter.send_item.command,
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
            resp_waiter.send_item.command,
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

    async def run_sequence(self, seq, progress=None) -> Any:
        """
        Run a command sequence.
        Implements the same API as the 'hid' drivers.

        :param seq: A "generator" function to use as a sequence. These are
        available in various places in the python-dali library.
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
                    logging.debug("got command from sequence: %s", cmd)
                    if isinstance(cmd, seq_sleep):
                        await asyncio.sleep(cmd.delay)
                    elif isinstance(cmd, seq_progress):
                        if progress:
                            progress(cmd)
                    elif isinstance(cmd, list):
                        response = await self._send_commands_internal(
                            cmd, BusTrafficSource.WB, lock_queue=False
                        )
                    else:
                        response = (
                            await self._send_commands_internal([cmd], BusTrafficSource.WB, lock_queue=False)
                        )[0]
        finally:
            seq.close()

    async def send(self, cmd: Command, source: BusTrafficSource = BusTrafficSource.WB) -> Response:
        """
        Send a DALI command to the bus and optionally wait for a response.
        Args:
            cmd (Command): The DALI command to send. Must contain a valid frame and
                optional response handler.
            source (str): The source identifier for logging and tracking purposes.
        Returns:
            Response: The response from the DALI device if cmd.response is set,
                otherwise Response(None).
                Internal transmission errors are returned as WbGatewayTransmissionError or its subclasses.
        """

        return (await self.send_commands([cmd], source=source))[0]

    async def send_commands(
        self, commands: Sequence[Command], source: BusTrafficSource = BusTrafficSource.WB
    ) -> List[Response]:
        """
        Send a sequence of DALI commands to the bus and optionally wait for responses.
        Send order is preserved, but commands are sent in batches
        and can't be interleaved with other send() calls.
        If sending multiple commands is desired, but order is not important,
        consider using asyncio.gather with individual send() calls instead.
        Args:
            commands (list[Command]): The list of DALI commands to send.
            source (str): The source identifier for logging and tracking purposes.
        Returns:
            list[Response]: The list of responses from the DALI devices
                if commands have responses set, otherwise Response(None).
                Internal transmission errors are returned as WbGatewayTransmissionError or its subclasses.
        """

        return await self._send_commands_internal(commands, source, lock_queue=True)

    async def _queue_sender(
        self,
    ) -> None:  # pylint: disable=too-many-return-statements, too-many-branches, too-many-statements
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

                self.logger.debug("Processing queue item: %s", str(item.command))
                timeout = WAIT_COMMANDS_FOR_BATCH_TIMEOUT_S

                if item.future.cancelled():
                    self.logger.debug("Skipping cancelled queue item: %s", str(item.command))
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
                self.logger.error("Error processing queue item: %s", e)
                if item is not None and not item.future.done():
                    item.future.set_result(WbGatewayTransmissionError())

    async def _send_to_gateway(self, items: list[SendQueueItem], start_index: int) -> None:
        if len(items) > 0:
            await self._overheat_rate_limiter.wait_before_send()
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
                            waiter_to_clear.send_item.command,
                            index,
                        )

                timeout_handler = asyncio.get_running_loop().call_later(
                    WAIT_DALI_RESPONSE_TIMEOUT_S,
                    timeout_callback,
                )
                self._waiting_for_responses[current_index] = WaitResponseItem(
                    item, timeout_handler, self._send_queue_item_index
                )

                result = encode_frame_for_modbus(item.command.frame, item.command.sendtwice)
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
        self, commands: Sequence[Command], source: BusTrafficSource, lock_queue: bool
    ) -> list[Response]:
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug("send: %s", ", ".join(str(cmd) for cmd in commands))
        commands_to_send = []
        for cmd in commands:
            if cmd.devicetype != 0:
                commands_to_send.append(EnableDeviceType(cmd.devicetype))
            commands_to_send.append(cmd)
        response_futures: list[asyncio.Future] = []
        if lock_queue:
            await self._send_queue_lock.acquire()
        try:
            for cmd in commands_to_send:
                fut = asyncio.get_running_loop().create_future()
                response_futures.append(fut)
                await self._send_queue.put(SendQueueItem(fut, cmd, source))
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
