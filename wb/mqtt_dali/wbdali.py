from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from itertools import groupby
from operator import itemgetter
from typing import Any, Iterable, Optional

import asyncio_mqtt
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
from dali.driver.base import DALIDriver
from dali.driver.hid import _callback
from dali.frame import BackwardFrame, BackwardFrameError, ForwardFrame, Frame
from dali.gear.general import EnableDeviceType
from dali.sequences import progress as seq_progress
from dali.sequences import sleep as seq_sleep

from wb.mqtt_dali.barrier import Barrier

ERR_START_BIT = 0x100  # не получен старт бит
ERR_BIT_TIME = 0x200  # неверное время бита
ERR_FRAME_LENGTH = 0x400  # неверная длина фрейма
ERR_STOP_BITS = 0x800  # не получены стоп биты
ERR_TIMEOUT = 0x1000  # таймаут приёма фрейма
ERR_LINE_POWER = 0x2000  # линия не запитана
ERR_LINE_BUSY = 0x4000  # линия занята
ERR_STILL_SENDING = 0x8000


@dataclass
class WBDALIConfig:
    """Configuration for WBDALIDriver."""

    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: Optional[str] = None
    mqtt_password: Optional[str] = None
    device_name: str = "wb-mdali_2"
    channel: int = 1
    modbus_slave_id: int = 2
    modbus_port_path: str = "/dev/ttyRS485-1"
    modbus_baud_rate: int = 115200
    modbus_parity: str = "N"
    modbus_data_bits: int = 8
    modbus_stop_bits: int = 2
    reconnect_interval: int = 1
    reconnect_limit: Optional[int] = None
    barrier_max_concurrent_tasks: int = 3
    barrier_timeout: float = 0.01


class WBDALIDriver(DALIDriver):
    """``DALIDriver`` implementation for Hasseb DALI USB device."""

    device_found = None
    logger = logging.getLogger("WBDALIDriver")
    sn = 0
    send_message = None
    _pending = None
    _response_message = None

    async def send_modbus_rpc_no_response(self, function: int, address: int, count: int, msg: str) -> None:
        """Send a Modbus RPC command without expecting a response."""
        self.logger.debug(
            "Sending Modbus RPC command: function=%d, address=%d, count=%d, msg=%s",
            function,
            address,
            count,
            msg,
        )

        # FIXME: I don't know the bette way
        await asyncio.wait_for(self.mqtt_client._connected, timeout=5)  # pylint: disable=W0212
        self.rpc_id_counter += 1
        await self.mqtt_client.publish(
            "/rpc/v1/wb-mqtt-serial/port/Load/dali-no-response",
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

    async def reset_queue(self) -> None:
        self.logger.debug("Resetting message queue")
        self.next_pointer = 0

        await self.send_modbus_rpc_no_response(
            function=16,
            address=1920,
            count=self.device_queue_size * 2,
            msg="0000fbdf" * self.device_queue_size,
        )

        await self.send_modbus_rpc_no_response(
            function=6,
            address=1960,
            count=1,
            msg="0000",
        )

        self.responses = {}

    async def get_next_pointer(self):
        """Get the next pointer for the message queue."""
        async with self.next_pointer_lock:
            pointer = self.next_pointer
            if (pointer in self.responses) and asyncio.isfuture(self.responses[pointer]):
                if not self.responses[pointer].done():
                    self.logger.debug("Pointer %d is still waiting for a response", pointer)
                await self.responses[pointer]

            self.responses[pointer] = asyncio.get_event_loop().create_future()

            self.next_pointer = (self.next_pointer + 1) % self.device_queue_size
            self.cmd_counter += 1

            msgs = []
            for i in range(self.device_queue_size):
                if i not in self.responses:
                    val = 0
                else:
                    val = self.responses[i].done()
                msgs.append(f"{i}={val}")

            self.logger.debug("Next pointer: %d, responses: %s", pointer, " ".join(msgs))

            return pointer, self.responses[pointer]

    async def _incoming_ff_task(self) -> None:
        self.logger.debug("Incoming FF task running...")
        async with self._create_mqtt_client() as mqtt_client:
            self.logger.debug("Connected to MQTT broker")
            await mqtt_client.subscribe(
                f"/devices/{self.config.device_name}/controls/channel{self.config.channel}_receive_24bit_forward"
            )
            async for message in mqtt_client.unfiltered_messages:
                self.logger.debug(
                    "Received FF24 MQTT message: %s %s",
                    message.topic,
                    message.payload.decode(),
                )

                if message.retain:
                    continue
                frame = ForwardFrame(24, int(message.payload) >> 8)
                cmd = from_frame(frame, dev_inst_map=self.dev_inst_map)
                self.logger.debug("Received FF24: %s", cmd)
                self.bus_traffic._invoke(cmd, None, False)  # pylint: disable=W0212

        # Subscribe to reply topics

    async def _read_task(self) -> None:
        self.logger.debug("Read task running...")
        async with self.mqtt_client:
            self.logger.debug("Connected to MQTT broker")

            # Subscribe to reply topics
            for i in range(self.device_queue_size):
                await self.mqtt_client.subscribe(
                    f"/devices/{self.config.device_name}/controls/channel{self.config.channel}_reply{i}"
                )
            self.connected.set()

            # Listen for messages
            async for message in self.mqtt_client.unfiltered_messages:
                self.logger.debug("Received message: %s %s", message.topic, message.payload.decode())

                if message.retain:
                    self.logger.debug("Received retained message, ignoring...")
                    continue  # Ignore retained messages

                resp = int(message.payload.decode())

                # Process the message as needed
                resp_pointer = int(
                    str(message.topic)
                    .rsplit("/", maxsplit=1)[-1]
                    .replace(f"channel{self.config.channel}_reply", "")
                )

                if resp_pointer not in self.responses:
                    self.logger.warning("Received response for unknown pointer: %d", resp_pointer)
                    continue
                resp_future = self.responses[resp_pointer]

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
                    continue

                if (resp & ERR_TIMEOUT) != 0:
                    self.logger.debug("Timeout waiting for response")
                    resp_future.set_result(None)
                    continue

                resp_future.set_result(BackwardFrame(resp & ~ERR_STILL_SENDING))

    def _create_mqtt_client(self) -> asyncio_mqtt.Client:
        """Create and configure MQTT client."""
        client_kwargs = {
            "hostname": self.config.mqtt_host,
            "port": self.config.mqtt_port,
        }

        if self.config.mqtt_username:
            client_kwargs["username"] = self.config.mqtt_username
        if self.config.mqtt_password:
            client_kwargs["password"] = self.config.mqtt_password

        return asyncio_mqtt.Client(**client_kwargs)

    def __init__(
        self,
        config: Optional[WBDALIConfig] = None,
        dev_inst_map: Optional[DeviceInstanceTypeMapper] = None,
    ):
        self.config = config or WBDALIConfig()
        self.dev_inst_map = dev_inst_map
        self.logger.debug(
            "path=%s, reconnect_interval=%d, reconnect_limit=%d, dev_inst_map=%s",
            config.modbus_port_path,
            config.reconnect_interval,
            config.reconnect_limit,
            dev_inst_map,
        )

        self.responses = {}

        self._log = logging.getLogger()
        self._reconnect_count = 0
        self._reconnect_task = None

        self._f = None

        self._current_command_frame = None
        self._last_reply = None
        self._not_waiting_for_reply = asyncio.Event()
        self._not_waiting_for_reply.set()

        # Should the send() method raise an exception if there is a
        # problem communicating with the underlying device, or should
        # it catch the exception and keep trying?  Set this attribute
        # as required.
        self.exceptions_on_send = True

        # Acquire this lock to perform a series of commands as a
        # transaction.  While you hold the lock, you must call send()
        # with keyword argument in_transaction=True
        self.transaction_lock = asyncio.Lock()

        # Register to be called back with "connected", "disconnected"
        # or "failed" as appropriate ("failed" means the reconnect
        # limit has been reached; no more connections will be
        # attempted unless you call connect() explicitly.)
        self.connection_status_callback = _callback(self)

        # Register to be called back with bus traffic; three arguments are passed:
        # command, response, config_command_error

        # config_command_error is true if the config command has a response, or
        # if the command was not sent twice within the required time limit
        self.bus_traffic = _callback(self)

        # This event will be set when we are connected to the device
        # and cleared when the connection is lost
        self.connected = asyncio.Event()

        # firmware_version and serial may be populated on some
        # devices, and will read as None on devices that don't support
        # reading them.  They are only valid after self.connected is
        # set.
        self.firmware_version = None
        self.serial = None

        self.device_queue_size = 10
        self.next_pointer = 0
        self.next_pointer_lock = asyncio.Lock()
        self.mqtt_client = self._create_mqtt_client()
        self.rpc_id_counter = 0
        self.cmd_counter = 0
        self.send_barrier = Barrier(
            self.config.barrier_max_concurrent_tasks,
            default_timeout=self.config.barrier_timeout,
        )

    async def run_sequence(
        self,
        seq,
        progress=None,
    ) -> Any:
        """
        Run a command sequence as a transaction. Implements the same API as
        the 'hid' drivers.

        :param seq: A "generator" function to use as a sequence. These are
        available in various places in the python-dali library.
        :param progress: A function to call with progress updates, used by
        some sequences to provide status information. The function must
        accept a single argument. A suitable example is `progress=print` to
        use the built-in `print()` function.
        :return: Depends on the sequence being used
        """

        async with self.transaction_lock:
            response = None
            try:
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
                            await self.send(
                                EnableDeviceType(cmd.devicetype),
                            )
                        response = await self.send(cmd)
            finally:
                seq.close()

    def wait_for_response(self) -> None:
        self.logger.debug("wait_for_response()")

    def construct(self, command) -> None:
        self.logger.debug("construct(command=%s)", command)

    def extract(self, data) -> None:
        self.logger.debug("extract(data=%s)", data)

    async def _add_cmd_to_send_buffer(self, pointer: int, reg_value: int, timeout: int = None) -> None:
        """Use barrier primitive to write multiple commands via single Modbus request"""
        self.logger.debug("waiting at the barrier, pointer=%d", pointer)
        payload = (pointer, reg_value)
        position, payloads = await self.send_barrier.wait(payload, timeout=timeout)
        self.logger.debug("barrier position: %s, pointer=%d, payloads=%s", position, pointer, payloads)

        if position == 0:
            # We are the first task to reach the barrier, we will send the commands
            reg_val_at_pointer = {}
            for p, c in payloads:
                reg_val_at_pointer[p] = c
            pointers = list(sorted(reg_val_at_pointer.keys()))

            # magic, credit: https://docs.python.org/2.6/library/itertools.html#examples
            # [1, 4,5,6, 10, 15,16,17,18, 22, 25,26,27,28] => [1], [4,5,6], [10], [15,16,17,18], [22], [25,26,27,28]
            for _, g in groupby(enumerate(pointers), lambda ix: ix[0] - ix[1]):
                conseq_range = list((map(itemgetter(1), g)))
                start_pointer = conseq_range[0]
                count = len(conseq_range)
                msg = "".join([f"{reg_val_at_pointer[p]:08x}" for p in conseq_range])

                await self.send_modbus_rpc_no_response(
                    function=16,
                    address=1920 + start_pointer * 2,
                    count=count * 2,
                    msg=msg,
                )

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

    async def send(self, cmd: Command) -> Optional[Response]:
        self.logger.debug("send(command=%s)", cmd)
        response = None
        # await self.connected.wait()
        # await asyncio.sleep(0.001)
        # if not self._not_waiting_for_reply.is_set():
        #     self.logger.warning("another send() is still in progress, waiting for it to complete")
        # await self._not_waiting_for_reply.wait()

        if (cmd.sendtwice) and (cmd.response is not None):
            self.logger.warning(
                "Command %s has sendtwice=True and a response, this is not supported",
                cmd,
            )
            raise ValueError("Command with sendtwice=True cannot have a response")

        if cmd.sendtwice:
            next_pointers = await asyncio.gather(self.get_next_pointer(), self.get_next_pointer())
        else:
            next_pointers = [
                await self.get_next_pointer(),
            ]
        # if cmd.bits
        for i, (pointer, future) in enumerate(next_pointers):
            self.logger.debug("Sending command: %s %d/%d", cmd, i + 1, len(next_pointers))
            modbus_reg_val = self._encode_frame_for_modbus(cmd.frame)
            await self._add_cmd_to_send_buffer(pointer, modbus_reg_val)

            if cmd.response:
                resp_frame = await future
                response = cmd.response(resp_frame)

        self.bus_traffic._invoke(cmd, response, False)  # pylint: disable=W0212
        return response

    def receive(self) -> None:
        self.logger.debug("receive()")

    def readFirmwareVersion(self) -> None:
        self.logger.debug("readFirmwareVersion()")

    def enableSniffing(self) -> None:
        self.logger.debug("enableSniffing()")

    def disableSniffing(self):
        self.logger.debug("disableSniffing()")

    async def connect(self) -> bool:
        """Attempt to connect to the device.

        Attempts to open the device.  If this fails, schedules a
        reconnection attempt.

        Returns True if opening the device file succeded immediately,
        False otherwise.  NB you must still await connected.wait()
        before using the device, because there may be further`
        initialisation for the driver to perform.

        If your application is (for example) a command-line script
        that wants to report failure as early as possible, you could
        do so if this returns False.
        """
        if self._f:
            return True
        self._log.debug("trying to connect to %s...", self.config.modbus_port_path)

        self._reconnect_count = 0
        # self.connected.set()
        self._log.debug(" opened")
        # asyncio.get_running_loop().add_reader(self._f, self._reader)

        async with self.mqtt_client:
            await self.reset_queue()

        asyncio.create_task(self._read_task())
        asyncio.create_task(self._incoming_ff_task())

        self.connection_status_callback._invoke("connected")  # pylint: disable=W0212
        return True

    async def _reconnect(self) -> None:
        self._reconnect_count += 1
        if self.config.reconnect_limit is not None and self._reconnect_count > self.config.reconnect_limit:
            # We have failed.
            self._log.debug("connection limit reached")
            self._reconnect_count = 0
            self._reconnect_task = None
            return
        await asyncio.sleep(self.config.reconnect_interval)
        self._reconnect_task = None
        self.connect()

    def disconnect(self, reconnect: bool = False) -> None:
        self._log.debug("disconnecting")
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._f:
            asyncio.get_running_loop().remove_reader(self._f)
            os.close(self._f)
        self._f = None
        self.connected.clear()
        self.connection_status_callback._invoke("disconnected")  # pylint: disable=W0212
        if reconnect:
            self._reconnect_task = asyncio.ensure_future(self._reconnect())


class AsyncDeviceInstanceTypeMapper(DeviceInstanceTypeMapper):
    """A version of DeviceInstanceTypeMapper taking advantage of sending of multiple DALI commands in parallel"""

    async def async_autodiscover(
        self, driver, addresses: int | tuple[int, int] | Iterable[int] = (0, 63)
    ) -> None:
        """
        An async function to scan a DALI bus for control device instances,
        and query their types. Internaly it uses asyncio.gather to wait for
        completion of mulitple DALI commands in parallel.
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
