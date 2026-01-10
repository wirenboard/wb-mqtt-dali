from __future__ import annotations

import asyncio
import json
import logging
import random
import string
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

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

ERR_START_BIT = 0x100  # не получен старт бит
ERR_BIT_TIME = 0x200  # неверное время бита
ERR_FRAME_LENGTH = 0x400  # неверная длина фрейма
ERR_STOP_BITS = 0x800  # не получены стоп биты
ERR_TIMEOUT = 0x1000  # таймаут приёма фрейма
ERR_LINE_POWER = 0x2000  # линия не запитана
ERR_LINE_BUSY = 0x4000  # линия занята
ERR_STILL_SENDING = 0x8000

WAIT_DALI_RESPONSE_TIMEOUT = 1.5  # seconds


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
    barrier_max_concurrent_tasks: int = 3
    barrier_timeout: float = 0.01


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


@dataclass
class ResponseWaiter:
    future: asyncio.Future
    timeout_handler: Optional[asyncio.Handle] = None


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

        self._waiting_for_responses: dict[int, ResponseWaiter] = {}

        # Register to be called back with bus traffic
        self.bus_traffic = BusTrafficCallbacks()

        self.device_queue_size = 10
        self.rpc_id_counter = 0

        self._send_queue = asyncio.Queue(maxsize=self.device_queue_size)
        self._send_queue_lock = asyncio.Lock()
        self._queue_sender_task: Optional[asyncio.Task] = None

        self._mqtt_dispatcher = mqtt_dispatcher

        self._queue_start_modbus_address = 1920

        client_id_suffix = "".join(random.sample(string.ascii_letters + string.digits, 8))
        self._rpc_client_id = f"{mqtt_dispatcher.client_id.replace('/', '_')}-{client_id_suffix}"

    async def initialize(self) -> None:
        self.logger.debug("Initializing...")

        self._queue_sender_task = asyncio.create_task(self._queue_sender())

        await self._reset_queue_in_gateway()

        # Subscribe to all reply topics
        self.logger.debug("Subscribing to reply topics...")
        for i in range(self.device_queue_size):
            topic = f"/devices/{self.config.device_name}/controls/channel{self.config.channel}_reply{i}"
            await self._mqtt_dispatcher.subscribe(topic, self._handle_reply_message)

        # Subscribe to FF24 topic
        self.logger.debug("Subscribing to FF24 topic...")
        await self._mqtt_dispatcher.subscribe(
            f"/devices/{self.config.device_name}/controls/channel{self.config.channel}_receive_24bit_forward",
            self._handle_ff24_message,
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
            if resp_waiter.timeout_handler is not None:
                resp_waiter.timeout_handler.cancel()
        self._waiting_for_responses.clear()

        # Unsubscribe from all reply topics
        for i in range(self.device_queue_size):
            topic = f"/devices/{self.config.device_name}/controls/channel{self.config.channel}_reply{i}"
            if self._mqtt_dispatcher.is_running:
                await self._mqtt_dispatcher.unsubscribe(topic)

        # Unsubscribe from FF24 topic
        if self._mqtt_dispatcher.is_running:
            await self._mqtt_dispatcher.unsubscribe(
                f"/devices/{self.config.device_name}/controls/channel{self.config.channel}_receive_24bit_forward",
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

        self.rpc_id_counter += 1
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
                        # "total_timeout": 1,
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

        await self.send_modbus_rpc_no_response(
            function=16,
            address=self._queue_start_modbus_address,
            count=self.device_queue_size * 2,
            msg="0000fbdf" * self.device_queue_size,
        )

        await self.send_modbus_rpc_no_response(
            function=6,
            address=1960,
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
        self.logger.debug(
            "Received message: %s %s",
            message.topic,
            message.payload.decode(),
        )

        if message.retain:
            self.logger.debug("Received retained message, ignoring...")
            return

        resp = int(message.payload.decode())

        # Process the message as needed
        resp_pointer = int(
            str(message.topic).rsplit("/", maxsplit=1)[-1].replace(f"channel{self.config.channel}_reply", "")
        )

        resp_waiter = self._waiting_for_responses.pop(resp_pointer, None)
        if resp_waiter is None:
            self.logger.warning("Received response for unknown pointer: %d", resp_pointer)
            return
        resp_future = resp_waiter.future
        if resp_waiter.timeout_handler is not None:
            resp_waiter.timeout_handler.cancel()

        # порядок важен, потому что может быть framing error + timeout
        if (
            ((resp & ERR_START_BIT) != 0)
            or ((resp & ERR_BIT_TIME) != 0)
            or ((resp & ERR_FRAME_LENGTH) != 0)
            or ((resp & ERR_STOP_BITS) != 0)
        ):
            self.logger.error(
                "Received error in response: %x (%x)",
                resp,
                resp & ~ERR_STILL_SENDING,
            )
            resp_future.set_result(BackwardFrameError(0))
            return

        if (resp & ERR_TIMEOUT) != 0:
            self.logger.debug("Timeout waiting for response")
            resp_future.set_result(None)
            return

        backward_frame = BackwardFrame(resp & ~ERR_STILL_SENDING)
        resp_future.set_result(backward_frame)
        self.bus_traffic.invoke(backward_frame, "bus")

    async def run_sequence(
        self,
        seq,
        progress=None,
    ) -> Any:
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

        response = None
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
                    else:
                        if cmd.devicetype != 0:
                            # The 'send()' calls here *do* refer to the DALI
                            # transmit method
                            await self._send_commands_unsafe(
                                [EnableDeviceType(cmd.devicetype)],
                            )
                        response = (await self._send_commands_unsafe([cmd]))[0]
        finally:
            seq.close()

    async def send_commands(
        self, commands: list[Command], source: str = "default"
    ) -> list[Optional[Response]]:
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

        async with self._send_queue_lock:
            return await self._send_commands_unsafe(commands, source)

    def _encode_frame_for_modbus(self, dali_frame: Frame) -> int:
        frame_len = len(dali_frame)
        frame_int = dali_frame.as_integer

        if frame_len == 16:
            return frame_int << 16
        if frame_len == 24:
            return (frame_int << 8) | 0x01
        if frame_len == 25:
            first_two_bytes = frame_int >> 8
            last_byte = frame_int & 0xFF
            # insert the 0x01 bit in the middle
            dali_25bit_frame = (first_two_bytes << 1) | 0x00
            dali_25bit_frame = (dali_25bit_frame << 8) | last_byte
            result = (dali_25bit_frame << 7) | 0x02
            self.logger.debug("Sending 25-bit frame, modbus_reg_val=%x", result)
            return result

        raise ValueError(f"Unsupported frame length: {frame_len}")

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

        res = await self.send_commands([cmd], source=source)
        return res[0]

    async def _queue_sender(self) -> None:
        start_index = 0
        current_index = 0
        regs_to_save = []
        timeout = None
        while True:
            # wait for free slot in the queue
            resp_waiter = self._waiting_for_responses.get(current_index)
            if resp_waiter is not None and not resp_waiter.future.done():
                await resp_waiter.future

            try:
                item: SendQueueItem = await asyncio.wait_for(self._send_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._send_data_to_modbus(
                    regs_to_save,
                    self._queue_start_modbus_address + start_index * 2,
                )
                regs_to_save = []
                start_index = current_index
                timeout = None
                continue
            try:
                self.logger.debug("Processing queue item: %s", item)
                modbus_reg_val = self._encode_frame_for_modbus(item.command.frame)
                self.bus_traffic.invoke(item.command.frame, item.source)
                regs_to_save.append(modbus_reg_val)
                response_waiter = ResponseWaiter(item.future)

                def timeout_callback(index=current_index):
                    waiter_to_clear = self._waiting_for_responses.pop(index, None)
                    if waiter_to_clear is not None and not waiter_to_clear.future.done():
                        waiter_to_clear.future.set_result(None)

                response_waiter.timeout_handler = asyncio.get_running_loop().call_later(
                    WAIT_DALI_RESPONSE_TIMEOUT,
                    timeout_callback,
                )
                self._waiting_for_responses[current_index] = response_waiter
                current_index += 1
                if current_index == self.device_queue_size:
                    # We have reached the end of the queue, send the commands
                    await self._send_data_to_modbus(
                        regs_to_save,
                        self._queue_start_modbus_address + start_index * 2,
                    )
                    regs_to_save = []
                    start_index = 0
                    current_index = 0
                timeout = 0.01
            except Exception as e:
                self.logger.error("Error processing queue item: %s", e)
                if not item.future.done():
                    item.future.set_result(None)

    async def _send_data_to_modbus(self, data: list[int], address: int) -> None:
        if len(data) != 0:
            msg = "".join([f"{reg:04x}" for reg in data])
            await self.send_modbus_rpc_no_response(
                function=16,
                address=address,
                count=len(data),
                msg=msg,
            )

    async def _send_commands_unsafe(
        self, commands: list[Command], source: str = "default"
    ) -> list[Optional[Response]]:
        self.logger.debug("send: %s", commands)
        commands_to_send = []
        for cmd in commands:
            if cmd.sendtwice:
                if cmd.response is not None:
                    self.logger.warning(
                        "Command %s has sendtwice=True and a response, this is not supported",
                        cmd,
                    )
                    raise ValueError(f"Command {cmd} with sendtwice=True cannot have a response")

                # A hack to handle sendtwice commands. Remove when proper sendtwice sending is implemented.
                commands_to_send.extend([cmd, cmd])
            else:
                commands_to_send.append(cmd)
        response_futures: list[asyncio.Future] = []
        for cmd in commands_to_send:
            response_futures.append(asyncio.get_running_loop().create_future())
            await self._send_queue.put(SendQueueItem(response_futures[-1], cmd, source))

        response_frames = await asyncio.gather(*response_futures)
        responses: list[Optional[Response]] = []
        i = 0
        for cmd in commands:
            if cmd.response is None:
                responses.append(None)
            else:
                responses.append(cmd.response(response_frames[i]))
            # Skip the second response for sendtwice commands
            i += 2 if cmd.sendtwice else 1

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
    commands = []
    if cmd.devicetype != 0:
        commands.append(EnableDeviceType(cmd.devicetype))
    commands.append(cmd)
    responses = await driver.send_commands(commands)
    res = responses[-1]
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
