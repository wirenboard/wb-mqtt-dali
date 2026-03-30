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
from dali.device.helpers import DeviceInstanceTypeMapper
from dali.frame import BackwardFrame, BackwardFrameError, ForwardFrame, Frame
from dali.gear.general import EnableDeviceType
from dali.sequences import progress as seq_progress
from dali.sequences import sleep as seq_sleep

from .bus_traffic import BusTrafficCallbacks, BusTrafficSource
from .mqtt_dispatcher import MQTTDispatcher

ERR_START_BIT = 0x100  # не получен старт бит
ERR_BIT_TIME = 0x200  # неверное время бита
ERR_FRAME_LENGTH = 0x400  # неверная длина фрейма
ERR_STOP_BITS = 0x800  # не получены стоп биты
ERR_TIMEOUT = 0x1000  # таймаут приёма фрейма
ERR_LINE_POWER = 0x2000  # линия не запитана
ERR_LINE_BUSY = 0x4000  # линия занята
ERR_STILL_SENDING = 0x8000

WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS = 1000
WAIT_DALI_RESPONSE_TIMEOUT_S = 1.5 * WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS / 1000.0
WAIT_COMMANDS_FOR_BATCH_TIMEOUT_S = 0.01

MASK = 0xFF


@dataclass
class WBDALIConfig:
    """Configuration for WBDALIDriver."""

    device_name: str = "wb-mdali_1"
    bus: int = 1
    queue_size: int = 10
    queue_start_modbus_address: int = 1920


@dataclass
class SendQueueItem:
    future: asyncio.Future
    command: Command
    source: BusTrafficSource
    timeout_handler: Optional[asyncio.Handle] = None

    def cancel_timeout(self) -> None:
        if self.timeout_handler is not None:
            self.timeout_handler.cancel()
            self.timeout_handler = None


def encode_frame_for_modbus(dali_frame: Frame) -> int:
    """
    Encode a DALI frame into a 32-bit Modbus register value according to
    the WB-MDALI gateway specification.
    """
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
        return result

    raise ValueError(f"Unsupported frame length: {frame_len}")


class WBDALIDriver:
    """
    A driver for WB-MDALI gateway v1.0
    """

    def __init__(
        self,
        config: WBDALIConfig,
        mqtt_dispatcher: MQTTDispatcher,
        logger: logging.Logger,
        dev_inst_map: Optional[DeviceInstanceTypeMapper] = None,
    ) -> None:
        self.logger = logger.getChild("WBDALIDriver")
        self.logger.debug("device=%s, dev_inst_map=%s", config.device_name, dev_inst_map)

        self.config = config
        self.dev_inst_map = dev_inst_map

        # Register to be called back with bus traffic
        self.bus_traffic = BusTrafficCallbacks(config.queue_size)

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
            topic = f"/devices/{self.config.device_name}/controls/channel{self.config.bus}_reply{i}"
            await self._mqtt_dispatcher.subscribe(topic, self._handle_reply_message)

        # Subscribe to FF24 topic
        self.logger.debug("Subscribing to FF24 topic...")
        await self._mqtt_dispatcher.subscribe(
            f"/devices/{self.config.device_name}/controls/channel{self.config.bus}_receive_24bit_forward",
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
            resp_waiter.cancel_timeout()
            if not resp_waiter.future.done():
                resp_waiter.future.set_result(None)
        self._waiting_for_responses.clear()

        # Unsubscribe from all reply topics
        for i in range(self.config.queue_size):
            topic = f"/devices/{self.config.device_name}/controls/channel{self.config.bus}_reply{i}"
            if self._mqtt_dispatcher.is_running:
                await self._mqtt_dispatcher.unsubscribe(topic)

        # Unsubscribe from FF24 topic
        if self._mqtt_dispatcher.is_running:
            await self._mqtt_dispatcher.unsubscribe(
                f"/devices/{self.config.device_name}/controls/channel{self.config.bus}_receive_24bit_forward",
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

        await self.send_modbus_rpc_no_response(
            function=16,
            address=self.config.queue_start_modbus_address,
            count=self.config.queue_size * 2,
            msg="0000fbdf" * self.config.queue_size,
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
        self.bus_traffic.notify_bus_frame(frame, 0)

    async def _handle_reply_message(self, message: mqtt.MQTTMessage) -> None:
        self.logger.debug(
            "Received message: %s %s",
            message.topic,
            message.payload.decode(),
        )

        if message.retain:
            self.logger.debug("Received retained message, ignoring...")
            return  # Ignore retained messages

        resp = int(message.payload.decode())

        # Process the message as needed
        resp_pointer = int(
            str(message.topic).rsplit("/", maxsplit=1)[-1].replace(f"channel{self.config.bus}_reply", "")
        )

        resp_waiter = self._waiting_for_responses.get(resp_pointer)
        if resp_waiter is None:
            self.logger.warning("Received response for unknown pointer: %d", resp_pointer)
            return
        resp_waiter.cancel_timeout()
        resp_future = resp_waiter.future
        if resp_future.done():
            self.logger.debug("Response future already done for pointer: %d", resp_pointer)
            return

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
            response_frame = BackwardFrameError(0)
            resp_future.set_result(response_frame)
            self.bus_traffic.notify_command(resp_waiter.command.frame, response_frame, resp_waiter.source, 0)
            return

        if (resp & ERR_TIMEOUT) != 0:
            self.logger.debug("Timeout waiting for response")
            response_frame = None
            resp_future.set_result(response_frame)
            self.bus_traffic.notify_command(resp_waiter.command.frame, response_frame, resp_waiter.source, 0)
            return

        response_frame = BackwardFrame(resp & ~ERR_STILL_SENDING)
        resp_future.set_result(response_frame)
        self.bus_traffic.notify_command(resp_waiter.command.frame, response_frame, resp_waiter.source, 0)

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
                        response = await self._send_commands_internal(
                            cmd, BusTrafficSource.WB, lock_queue=False
                        )
                    else:
                        response = (
                            await self._send_commands_internal([cmd], BusTrafficSource.WB, lock_queue=False)
                        )[0]
        finally:
            seq.close()

    async def send(self, cmd: Command, source: BusTrafficSource = BusTrafficSource.WB) -> Optional[Response]:
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
        self, commands: Sequence[Command], source: BusTrafficSource = BusTrafficSource.WB
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
                        self.bus_traffic.notify_command(
                            waiter_to_clear.command.frame, None, waiter_to_clear.source, 0
                        )
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
                regs_32bit.append(encode_frame_for_modbus(item.command.frame))

            await self.send_modbus_rpc_no_response(
                function=16,
                address=self.config.queue_start_modbus_address + start_index * 2,
                count=len(regs_32bit) * 2,
                msg="".join([f"{reg:08x}" for reg in regs_32bit]),
            )

    async def _send_commands_internal(
        self, commands: Sequence[Command], source: BusTrafficSource, lock_queue: bool
    ) -> list[Optional[Response]]:
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug("send: %s", ", ".join(str(cmd) for cmd in commands))
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
            # Skip additional EnableDeviceType and second sendtwice commands
            if cmd.devicetype != 0 or cmd.sendtwice:
                i += 1
            if cmd.response is None:
                responses.append(None)
            else:
                responses.append(cmd.response(response_frames[i]))
            i += 1

        return responses
