from __future__ import annotations

import asyncio
import json
import logging
import random
import string
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional, Sequence, Union

import paho.mqtt.client as mqtt
from dali.address import DeviceBroadcast, DeviceShort, InstanceNumber
from dali.command import Command, Response, from_frame
from dali.device.general import (
    QueryDeviceStatus,
    QueryDeviceStatusResponse,
    QueryInstanceEnabled,
    QueryInstanceType,
    QueryNumberOfInstances,
    StartQuiescentMode,
    StopQuiescentMode,
)
from dali.device.helpers import DeviceInstanceTypeMapper, check_bad_rsp
from dali.frame import BackwardFrame, BackwardFrameError, ForwardFrame, Frame
from dali.gear.general import EnableDeviceType
from dali.sequences import progress as seq_progress
from dali.sequences import sleep as seq_sleep

from .mqtt_dispatcher import MQTTDispatcher

WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS = 1000
WAIT_DALI_RESPONSE_TIMEOUT_S = 1.5 * WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS / 1000.0
WAIT_COMMANDS_FOR_BATCH_TIMEOUT_S = 0.01


@dataclass
class WBDALIConfig:
    """Configuration for WBDALIDriver."""

    device_name: str = "wb-mdali_1"
    channel: int = 1
    modbus_slave_id: int = 1
    modbus_port_path: str = "/dev/ttyRS485-1"
    modbus_baud_rate: int = 115200
    modbus_parity: str = "N"
    modbus_data_bits: int = 8
    modbus_stop_bits: int = 2
    queue_size: int = 16
    queue_start_modbus_address: int = 1400
    queue_bulk_send_pointer_modbus_address: int = 1432
    queue_modbus_channel_offset: int = 1000


class BusTrafficCallbacks:
    """Helper class for callback registration"""

    def __init__(self) -> None:
        self._callbacks = set()

    def register(self, func: Callable[[Frame, str], None]) -> Callable[[], None]:
        def cleanup():
            self._callbacks.discard(func)

        self._callbacks.add(func)
        return cleanup

    def invoke(self, frame: Frame, source: str) -> None:
        for func in self._callbacks:
            func(frame, source)


@dataclass
class SendQueueItem:
    future: asyncio.Future
    command: Command
    source: str
    timeout_handler: Optional[asyncio.Handle] = None

    def cancel_timeout(self) -> None:
        if self.timeout_handler is not None:
            self.timeout_handler.cancel()
            self.timeout_handler = None


def encode_frame_for_modbus(dali_frame: Frame, sendtwice: bool = False, priority: int = 1) -> int:
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
    if not (0 <= priority <= 5):
        raise ValueError(f"Priority must be 0-5, got {priority}")
    result |= (priority & 0x7) << 29

    return result


class WBDALIDriver:
    def __init__(
        self,
        config: WBDALIConfig,
        mqtt_dispatcher: MQTTDispatcher,
        logger: logging.Logger,
        dev_inst_map: Optional[DeviceInstanceTypeMapper] = None,
    ) -> None:
        self.logger = logger.getChild("WBDALIDriver")
        self.logger.debug("path=%s, dev_inst_map=%s", config.modbus_port_path, dev_inst_map)

        self.config = config
        self.dev_inst_map = dev_inst_map

        # Register to be called back with bus traffic
        self.bus_traffic = BusTrafficCallbacks()

        self._send_queue: asyncio.Queue[SendQueueItem] = asyncio.Queue(maxsize=self.config.queue_size)

        self._waiting_for_responses: dict[int, SendQueueItem] = {}

        # Lock to ensure only one sender at a time
        self._send_queue_lock = asyncio.Lock()
        self._queue_sender_task: Optional[asyncio.Task] = None

        self._mqtt_dispatcher = mqtt_dispatcher

        client_id_suffix = "".join(random.sample(string.ascii_letters + string.digits, 8))
        self._rpc_client_id = f"{mqtt_dispatcher.client_id.replace('/', '_')}-{client_id_suffix}"
        self._rpc_id_counter = 0

        self._batch_start_index = 0
        self._next_queue_index = 0

    @property
    def rpc_client_id(self) -> str:
        return self._rpc_client_id

    @property
    def rpc_id_counter(self) -> int:
        return self._rpc_id_counter

    @property
    def batch_start_index(self) -> int:
        """Get the start index of the current batch being sent to the gateway."""
        return self._batch_start_index

    async def initialize(self) -> None:
        self.logger.debug("Initializing...")

        self._queue_sender_task = asyncio.create_task(self._queue_sender())

        await self._reset_queue_in_gateway()

        # Subscribe to all reply topics
        self.logger.debug("Subscribing to reply topics...")
        for i in range(self.config.queue_size):
            topic = (
                f"/devices/{self.config.device_name}/controls/bus_{self.config.channel}_bulk_send_reply_{i}"
            )
            await self._mqtt_dispatcher.subscribe(topic, self._handle_reply_message)

        # Subscribe to FF24 topic
        # self.logger.debug("Subscribing to FF24 topic...")
        # await self._mqtt_dispatcher.subscribe(
        #     f"/devices/{self.config.device_name}/controls/channel{self.config.channel}_receive_24bit_forward",
        #     self._handle_ff24_message,
        # )

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
            if not resp_waiter.future.done():
                resp_waiter.future.set_result(None)
        self._waiting_for_responses.clear()

        # Unsubscribe from all reply topics
        for i in range(self.config.queue_size):
            topic = (
                f"/devices/{self.config.device_name}/controls/bus_{self.config.channel}_bulk_send_reply_{i}"
            )
            if self._mqtt_dispatcher.is_running:
                await self._mqtt_dispatcher.unsubscribe(topic)

        # Unsubscribe from FF24 topic
        # if self._mqtt_dispatcher.is_running:
        #     await self._mqtt_dispatcher.unsubscribe(
        #         f"/devices/{self.config.device_name}/controls/channel{self.config.channel}_receive_24bit_forward",
        #     )
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
                        "slave_id": self.config.modbus_slave_id,
                        "function": function,
                        "address": address,
                        "count": count,
                        # "response_timeout": 8,
                        "total_timeout": WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS,
                        "frame_timeout": 0,
                        "protocol": "modbus",
                        "format": "HEX",
                        "path": self.config.modbus_port_path,
                        "baud_rate": self.config.modbus_baud_rate,
                        "parity": self.config.modbus_parity,
                        "data_bits": self.config.modbus_data_bits,
                        "stop_bits": self.config.modbus_stop_bits,
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
            + (self.config.channel - 1) * self.config.queue_modbus_channel_offset
        )

        await self.send_modbus_rpc_no_response(
            function=6,
            address=pointer_address,
            count=1,
            msg="0000",
        )

    async def _handle_ff24_message(self, message: mqtt.MQTTMessage) -> None:
        self.logger.debug(
            "Received FF24 MQTT message: %s %s",
            message.topic,
            message.payload.decode(),
        )

        if message.retain:
            return
        frame = ForwardFrame(24, int(message.payload) >> 8)
        cmd = from_frame(frame, dev_inst_map=self.dev_inst_map)
        self.logger.debug("Received FF24: %s", cmd)
        self.bus_traffic.invoke(frame, "bus")

    async def _handle_reply_message(self, message: mqtt.MQTTMessage) -> None:
        """Handle reply message from the DALI bus.

        Message payload format:
        [7..0]   - Backward Frame (8 bit)
        [15..8]  - status:
                   0 - no transmission
                   1 - transmission with backward response
                   2 - transmission without response
                   3 - broken response
                   4 - transmission impossible (no power on bus)
        """
        self.logger.debug(
            "Received message: %s %s",
            message.topic,
            message.payload.decode(),
        )

        if message.retain:
            self.logger.debug("Received retained message, ignoring...")
            return  # Ignore retained messages

        resp = int(message.payload.decode())

        backward_frame_byte = resp & 0xFF
        status = (resp >> 8) & 0xFF

        # Process the message as needed
        resp_pointer = int(
            str(message.topic)
            .rsplit("/", maxsplit=1)[-1]
            .replace(f"bus_{self.config.channel}_bulk_send_reply_", "")
        )

        resp_waiter = self._waiting_for_responses.get(resp_pointer)
        if resp_waiter is None:
            self.logger.warning("Received response for unknown pointer: %d", resp_pointer)
            return
        resp_waiter.cancel_timeout()
        resp_future = resp_waiter.future

        self.logger.debug(
            "Parsed response: pointer=%d, status=%d, backward_frame=0x%02x",
            resp_pointer,
            status,
            backward_frame_byte,
        )

        if status == 0:
            # No transmission
            self.logger.debug("Status 0: No transmission")
            resp_future.set_result(None)
            return
        elif status == 1:
            # Transmission with backward response
            self.logger.debug("Status 1: Transmission with backward response")
            backward_frame = BackwardFrame(backward_frame_byte)
            resp_future.set_result(backward_frame)
            self.bus_traffic.invoke(backward_frame, "bus")
            return
        elif status == 2:
            # Transmission without response
            self.logger.debug("Status 2: Transmission without response")
            resp_future.set_result(None)
            return
        elif status == 3:
            # Broken response (framing error)
            self.logger.error(
                "Status 3: Broken response for pointer %d (backward_frame=0x%02x)",
                resp_pointer,
                backward_frame_byte,
            )
            resp_future.set_result(BackwardFrameError(backward_frame_byte))
            return
        elif status == 4:
            # Transmission impossible (no power on bus)
            self.logger.error(
                "Status 4: Transmission impossible - no power on bus (pointer=%d)",
                resp_pointer,
            )
            resp_future.set_result(BackwardFrameError(0))
            return
        else:
            # Unknown status
            self.logger.error(
                "Unknown status %d for pointer %d (backward_frame=0x%02x)",
                status,
                resp_pointer,
                backward_frame_byte,
            )
            resp_future.set_result(BackwardFrameError(0))
            return

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

        response: Union[Optional[Response], List[Optional[Response]]] = None
        try:
            async with self._send_queue_lock:
                while True:
                    try:
                        # Note that 'send()' here refers to the Python
                        # 'generator' paradigm, not to the DALI driver!
                        cmd = seq.send(response)
                    except StopIteration as r:
                        return r.value
                    response = None
                    logging.debug("got command from sequence: %s", cmd)
                    if isinstance(cmd, seq_sleep):
                        await asyncio.sleep(cmd.delay)
                    elif isinstance(cmd, seq_progress):
                        if progress:
                            progress(cmd)
                    elif isinstance(cmd, list):
                        response = await self._send_commands_internal(cmd, source="default", lock_queue=False)
                    else:
                        response = (
                            await self._send_commands_internal([cmd], source="default", lock_queue=False)
                        )[0]
        finally:
            seq.close()

    async def send(self, cmd: Command, source: str = "default") -> Optional[Response]:
        """
        Send a DALI command to the bus and optionally wait for a response.
        Args:
            cmd (Command): The DALI command to send. Must contain a valid frame and
                optional response handler.
            source (str): The source identifier for logging and tracking purposes.
        Returns:
            Optional[Response]: The response from the DALI device if cmd.response is set,
                otherwise None.
        """

        return (await self.send_commands([cmd], source=source))[0]

    async def send_commands(
        self, commands: Sequence[Command], source: str = "default"
    ) -> List[Optional[Response]]:
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
            list[Optional[Response]]: The list of responses from the DALI devices
                if commands have responses set, otherwise None.
        """

        return await self._send_commands_internal(commands, source, lock_queue=True)

    async def _queue_sender(self) -> None:
        batch: list[SendQueueItem] = []
        timeout = None
        while True:
            item = None
            try:
                resp_waiter = self._waiting_for_responses.get(self._next_queue_index)
                if resp_waiter is not None and not resp_waiter.future.done():
                    try:
                        await self._send_to_gateway(batch, self._batch_start_index)
                    finally:
                        batch = []
                        self._batch_start_index = self._next_queue_index
                    try:
                        await resp_waiter.future
                    except asyncio.CancelledError:
                        if not resp_waiter.future.cancelled():
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

                self.bus_traffic.invoke(item.command.frame, item.source)
                batch.append(item)
                self._next_queue_index += 1

                if self._next_queue_index == self.config.queue_size:
                    try:
                        await self._send_to_gateway(batch, self._batch_start_index)
                    finally:
                        batch = []
                        self._batch_start_index = 0
                        self._next_queue_index = 0

            except Exception as e:
                self.logger.error("Error processing queue item: %s", e)
                if item is not None and not item.future.done():
                    item.future.set_result(None)

    async def _send_to_gateway(self, items: list[SendQueueItem], start_index: int) -> None:
        if len(items) > 0:
            regs_32bit = []
            for current_index, item in enumerate(items, start_index):

                def timeout_callback(index=current_index):
                    waiter_to_clear = self._waiting_for_responses.get(index)
                    if waiter_to_clear is not None and not waiter_to_clear.future.done():
                        waiter_to_clear.future.set_result(None)
                        self.logger.error(
                            "Timeout waiting for response %s for queue index %d",
                            waiter_to_clear.command,
                            index,
                        )

                item.timeout_handler = asyncio.get_running_loop().call_later(
                    WAIT_DALI_RESPONSE_TIMEOUT_S,
                    timeout_callback,
                )
                self._waiting_for_responses[current_index] = item

                result = encode_frame_for_modbus(item.command.frame)
                self.logger.debug(
                    "Encoded frame: len=%d, result=0x%08x",
                    len(item.command.frame),
                    result,
                )
                regs_32bit.append(result)

            msg = "".join([f"{((reg & 0xFFFF) << 16) | ((reg >> 16) & 0xFFFF):08x}" for reg in regs_32bit])
            buffer_address = (
                self.config.queue_start_modbus_address
                + (self.config.channel - 1) * self.config.queue_modbus_channel_offset
                + start_index * 2
            )
            await self.send_modbus_rpc_no_response(
                function=16,
                address=buffer_address,
                count=len(regs_32bit) * 2,
                msg=msg,
            )

    async def _send_commands_internal(
        self, commands: Sequence[Command], source: str, lock_queue: bool
    ) -> list[Optional[Response]]:
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

        response_frames = await asyncio.gather(*response_futures)
        responses: list[Optional[Response]] = []
        i = 0
        for cmd in commands:
            # Skip additional EnableDeviceType commands
            if cmd.devicetype != 0:
                i += 1
            if cmd.response is None:
                responses.append(None)
            else:
                responses.append(cmd.response(response_frames[i]))
            i += 1

        return responses


class AsyncDeviceInstanceTypeMapper(DeviceInstanceTypeMapper):
    """A version of DeviceInstanceTypeMapper taking advantage of
    sending of multiple DALI commands in parallel
    """

    async def async_autodiscover(
        self, driver, addresses: int | tuple[int, int] | Iterable[int] = (0, 63)
    ) -> None:
        """
        An async function to scan a DALI bus for control device instances,
        and query their types. Internally it uses asyncio.gather to wait for
        completion of multiple DALI commands in parallel.
        This information is stored within this AsyncDeviceInstanceTypeMapper,
        for use in decoding "Device/Instance" event messages.

        :param driver: The async DALI driver to use for sending commands.
        :param addresses: Optional specifier of which addresses to scan. Can
        either be a single int, in which case all addresses from zero to that
        value will be scanned; or can be a tuple in the form (start, end), in
        which case all addresses between the provided values will be scanned;
        or finally can be an iterable of ints in which case each address, in
        the iterator will be scanned.

        Needs to be used through an appropriate driver for example:
        ```
        dev_inst_map = AsyncDeviceInstanceTypeMapper()
        await dev_inst_map.async_autodiscover(driver)
        ```
        instead of
        ```
        await driver.run_sequence(dev_inst_map.autodiscover())
        ```

        """
        logging.debug("Starting autodiscover with addresses: %s", addresses)

        if isinstance(addresses, int):
            addresses = list(range(0, addresses))
        elif isinstance(addresses, tuple) and len(addresses) == 2:
            addresses = list(range(addresses[0], addresses[1] + 1))

        # Use quiescent mode to reduce bus contention from input devices
        await driver.send(StartQuiescentMode(DeviceBroadcast()))
        responses = await asyncio.gather(
            *[driver.send(QueryDeviceStatus(device=DeviceShort(addr_int))) for addr_int in addresses],
        )

        queries = []
        logging.debug("QueryDeviceStatus responses: %s", zip(addresses, responses))
        for addr_int, rsp in zip(addresses, responses):
            addr = DeviceShort(addr_int)
            if check_bad_rsp(rsp):
                continue
            if isinstance(rsp, QueryDeviceStatusResponse):
                # Make sure the status is OK
                if rsp.short_address_is_mask or rsp.reset_state:
                    continue
            else:
                # If the response isn't QueryDeviceStatusResponse then
                # something is wrong
                continue

            # Find out how many instances the device has
            queries.append(QueryNumberOfInstances(device=addr))

        responses = await asyncio.gather(*[driver.send(q) for q in queries])
        enabled_queries = []
        type_queries = []
        for query, rsp in zip(queries, responses):
            addr = query.destination

            if check_bad_rsp(rsp):
                continue
            num_inst = rsp.value

            # For each instance, check it is enabled and then query the type
            for inst_int in range(num_inst):
                inst = InstanceNumber(inst_int)

                enabled_queries.append(QueryInstanceEnabled(device=addr, instance=inst))
                type_queries.append(QueryInstanceType(device=addr, instance=inst))

        responses = await asyncio.gather(
            *[driver.send(q) for q in enabled_queries],
            *[driver.send(q) for q in type_queries],
        )

        enabled_responses = responses[: len(enabled_queries)]
        type_responses = responses[len(enabled_queries) :]

        for query, enabled_rsp, type_rsp in zip(enabled_queries, enabled_responses, type_responses):
            addr = query.destination
            inst = query.instance
            if check_bad_rsp(enabled_rsp):
                continue
            if not enabled_rsp.value:
                # Skip if not enabled
                continue

            if check_bad_rsp(type_rsp):
                continue

            logging.debug("message=A²%d I%d type: %s", addr.address, inst.value, type_rsp.value)

            # Add the type to the device/instance map
            self.add_type(
                short_address=addr,
                instance_number=inst,
                instance_type=type_rsp.value,
            )
        await driver.send(StopQuiescentMode(DeviceBroadcast()))


async def query_request(driver: WBDALIDriver, cmd: Command) -> int:
    res = await driver.send(cmd)
    check_query_response(res)
    return res.raw_value.as_integer


def check_query_response(resp: Optional[Response]) -> None:
    if resp is None:
        raise RuntimeError("Got no response")
    raw_value = resp.raw_value
    if raw_value is None:
        raise RuntimeError("Got no response")
    if raw_value.error:
        raise RuntimeError("Framing error")
