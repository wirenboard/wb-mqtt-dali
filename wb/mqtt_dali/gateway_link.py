"""How a `WBDALIDriver` reaches its WB-DALI module: the gateway link.

The driver owns everything about DALI — the send queue and its per-slot
waiters, decoding answers, sequences, bus-traffic callbacks, the memo. What
differs between hosts is only how the module's registers are written and how
their contents come back:

* :class:`MqttSerialLink` — a controller. wb-mqtt-serial owns the port: writes
  go out as ``port/Load`` RPCs, and the reply registers and the bus monitor
  ring arrive as the controls wb-mqtt-serial publishes when it polls them.
* :class:`RegisterLink` — anything that reaches the Modbus registers itself
  (the WASM device editor over WebSerial, the simulator). Writes are register
  writes, and the link polls the reply registers and the ring on its own.

Both hand the driver the same two things — the value of a reply register by
queue slot, and the value of a monitor ring slot — through
:class:`GatewayListener`. Every batch the driver sends is one call to
``write_slots``; the link decides what "slot" means on the wire (the MQTT link
uses the driver's queue slot as the physical slot, the register link rewinds
the module and always writes from slot 0 — measured to be the only discipline
a lost frame cannot stall).

The driver takes a reply only for a slot whose write it has seen acked: a link
returns ``GatewayWriteResult.DONE`` from ``write_slots`` first and delivers the
replies after.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, List, Optional, Protocol

import aiomqtt

from .mqtt_dispatcher import MQTTDispatcher, get_int_payload, get_str_payload
from .mqtt_rpc_client import MQTTRPCClient, MQTTRPCError, MQTTRPCRequestTimeoutError
from .wbdali_registers import (
    FRAME_COUNTER_MODULO,
    MONITOR_REGISTERS_PER_SLOT,
    MONITOR_RING_SIZE,
    TransmissionStatus,
    from_monitor_registers,
    monitor_address,
    queue_pointer_address,
    queue_slot_address,
    reply_address,
    to_registers,
)

# Arbitrary: wb-mqtt-serial gives no bound while its port is busy.
WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS = 5000
# wb-mqtt-serial checks total_timeout only when its port is free, so this does not bound the ack.
PORT_LOAD_ACK_TIMEOUT_S = WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS / 1000.0 + 1.0
# wb-mqtt-serial's error for a request dropped by `total_timeout` before it went to the port.
RPC_REQUEST_TIMEOUT_REASON = "RPC request timeout"

# Register link pacing. Polling a reply register every 5 ms costs nothing
# against a 46 ms bus; the ring is read briskly when someone watches the
# monitor, at an idle pace when DALI-2 devices might speak unprompted, and
# lazily on a bus of plain gear.
REPLY_POLL_INTERVAL_S = 0.005
REPLY_TIME_PER_EXTRA_FRAME_S = 0.2
INITIAL_ROUND_TRIP_S = 0.02
ROUND_TRIP_SMOOTHING = 0.25
MONITOR_POLL_INTERVAL_S = 0.1
MONITOR_IDLE_POLL_INTERVAL_S = 0.25
MONITOR_QUIET_POLL_INTERVAL_S = 1.0

BUS_MONITOR_TOPIC_INDEX_BASE = 1


class GatewayWriteResult(Enum):
    DONE = "done"
    SKIPPED = "skipped"  # wb-mqtt-serial dropped the write before it reached the port
    FAILED = "failed"  # wb-mqtt-serial answered with an error or did not answer in time


class GatewayListener(Protocol):
    """What a link reports back to the driver."""

    def on_reply(self, resp_pointer: int, resp: Optional[int]) -> None:
        """The reply register of queue slot ``resp_pointer`` holds ``resp`` (None: unreadable)."""

    def on_monitor_slot(self, raw: int) -> None:
        """A bus monitor ring slot holds a new value."""

    def on_gateway_error_payload(self, payload: str) -> None:
        """wb-mqtt-serial's ``/meta/error`` for the module changed."""


class GatewayLink(Protocol):
    """The transport half of a WB-DALI bus driver; see the module docstring."""

    @property
    def connected(self) -> bool:
        """Whether writes can reach the module at all (the broker is up, for the MQTT link)."""

    @property
    def session(self) -> int:
        """Bumped on every reconnect: replies and reports from an older session are stale."""

    async def start(self, listener: GatewayListener) -> None:
        """Begin delivering replies and ring slots to ``listener``."""

    async def stop(self) -> None:
        """Stop delivering; safe to call more than once."""

    async def reset_pointer(self) -> GatewayWriteResult:
        """Point the module's queue back at slot 0."""

    async def write_slots(self, first_slot: int, frames: List[int]) -> GatewayWriteResult:
        """Arm ``frames`` (32-bit slot values) as queue slots ``first_slot..``; replies come after."""

    def set_bus_monitor_enabled(self, enabled: bool) -> None:
        """Someone is (not) watching the bus monitor."""

    def set_has_control_devices(self, present: bool) -> None:
        """Whether anything on the bus speaks unprompted (DALI-2 control devices)."""


class RegisterTransport(Protocol):
    """Modbus access to a module by device id — WebSerial, a serial port, the simulator."""

    async def read_input(self, device_id: str, address: int, count: int) -> List[int]:
        """Function 4."""

    async def write_holding(self, device_id: str, address: int, values: List[int]) -> None:
        """Function 6 or 16."""


class MqttSerialLink:
    """The controller link: wb-mqtt-serial in between (see module docstring)."""

    def __init__(
        self,
        config,
        mqtt_dispatcher: MQTTDispatcher,
        rpc_client: MQTTRPCClient,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.logger = logger
        self._mqtt_dispatcher = mqtt_dispatcher
        self._rpc_client = rpc_client
        self._listener: Optional[GatewayListener] = None
        self._meta_error_topic = f"/devices/{self.config.device_name}/meta/error"

    @property
    def connected(self) -> bool:
        return self._mqtt_dispatcher.connected

    @property
    def session(self) -> int:
        return self._mqtt_dispatcher.session

    def _reply_topic(self, index: int) -> str:
        return f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_{index}"

    def _monitor_topic(self, slot: int) -> str:
        return (
            f"/devices/{self.config.device_name}/controls/"
            f"bus_{self.config.bus}_monitor_sporadic_frame_{slot}"
        )

    async def start(self, listener: GatewayListener) -> None:
        self._listener = listener
        await self._rpc_client.start(self._mqtt_dispatcher)
        self.logger.debug("Subscribing to reply topics...")
        for i in range(self.config.queue_size):
            await self._mqtt_dispatcher.subscribe(self._reply_topic(i), self._on_reply_message)
        self.logger.debug("Subscribing to FF24 topic...")
        for i in range(BUS_MONITOR_TOPIC_INDEX_BASE, MONITOR_RING_SIZE + BUS_MONITOR_TOPIC_INDEX_BASE):
            await self._mqtt_dispatcher.subscribe(self._monitor_topic(i), self._on_monitor_message)
        await self._mqtt_dispatcher.subscribe(self._meta_error_topic, self._on_meta_error_message)

    async def stop(self) -> None:
        for i in range(self.config.queue_size):
            await self._mqtt_dispatcher.unsubscribe(self._reply_topic(i))
        for i in range(BUS_MONITOR_TOPIC_INDEX_BASE, MONITOR_RING_SIZE + BUS_MONITOR_TOPIC_INDEX_BASE):
            await self._mqtt_dispatcher.unsubscribe(self._monitor_topic(i))
        await self._mqtt_dispatcher.unsubscribe(self._meta_error_topic)
        await self._rpc_client.stop()

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

    async def reset_pointer(self) -> GatewayWriteResult:
        pointer_address = (
            self.config.queue_bulk_send_pointer_modbus_address
            + (self.config.bus - 1) * self.config.queue_modbus_bus_offset
        )
        # ensure: wb-mqtt-serial writes the gateway setup first.
        return await self.send_modbus_rpc(
            function=6, address=pointer_address, count=1, msg="0000", ensure_configured_device=True
        )

    async def write_slots(self, first_slot: int, frames: List[int]) -> GatewayWriteResult:
        msg = "".join([f"{((reg & 0xFFFF) << 16) | ((reg >> 16) & 0xFFFF):08x}" for reg in frames])
        buffer_address = (
            self.config.queue_start_modbus_address
            + (self.config.bus - 1) * self.config.queue_modbus_bus_offset
            + first_slot * 2
        )
        return await self.send_modbus_rpc(function=16, address=buffer_address, count=len(frames) * 2, msg=msg)

    def set_bus_monitor_enabled(self, enabled: bool) -> None:
        """wb-mqtt-serial streams the ring regardless; nothing to pace here."""

    def set_has_control_devices(self, present: bool) -> None:
        """See set_bus_monitor_enabled."""

    # -- what wb-mqtt-serial publishes -----------------------------------------

    def _on_reply_message(self, message: aiomqtt.Message) -> None:
        if message.retain:
            self.logger.debug("Received retained message, ignoring...")
            return
        if not message.payload:
            # wb-mqtt-serial clears every reply topic when it starts.
            self.logger.debug("Dropping empty reply on topic '%s'", message.topic)
            return
        try:
            value: Optional[int] = get_int_payload(message)
        except ValueError as exc:
            self.logger.error(
                "Failed to parse reply payload '%s' from topic '%s': %s", message.payload, message.topic, exc
            )
            value = None
        index = int(
            str(message.topic)
            .rsplit("/", maxsplit=1)[-1]
            .replace(f"bus_{self.config.bus}_bulk_send_reply_", "")
        )
        if self._listener is not None:
            self._listener.on_reply(index, value)

    def _on_monitor_message(self, message: aiomqtt.Message) -> None:
        if message.retain or not message.payload:
            return
        try:
            raw = get_int_payload(message)
        except ValueError as exc:
            self.logger.error(
                "Failed to parse bus monitor payload '%s' from topic '%s': %s",
                message.payload,
                message.topic,
                exc,
            )
            return
        if self._listener is not None:
            self._listener.on_monitor_slot(raw)

    def _on_meta_error_message(self, message: aiomqtt.Message) -> None:
        try:
            payload = get_str_payload(message).strip()
        except (AttributeError, UnicodeDecodeError) as exc:
            self.logger.error("Failed to parse /meta/error payload: %s", exc)
            return
        if self._listener is not None:
            self._listener.on_gateway_error_payload(payload)


class RegisterLink:  # pylint: disable=too-many-instance-attributes
    """The direct link: this process reads and writes the module's registers.

    A batch is one exchange: rewind the pointer, write the frames into slots
    ``0..n-1`` in one register write, poll the last slot's reply register until
    the module reports a transmission, then read every reply register. The
    replies are handed over under the driver's slot numbers once ``write_slots``
    has returned, as wb-mqtt-serial's would arrive after its ack. The
    module transmits armed slots strictly from its pointer and clears a slot's
    reply the moment the slot is written, which is what makes the last slot's
    status mean "the whole batch went out" and a non-zero reply mean "this
    frame's answer". Nothing else clears a reply register, so a slot that was
    never written is never read.

    The bus monitor ring is polled at a pace chosen by whether someone watches
    the monitor and whether DALI-2 devices are on the bus; the ring is
    baselined at start so frames left from before this session never replay as
    fresh traffic (on a controller the same frames arrive as retained
    publications, which the MQTT link drops).
    """

    def __init__(self, config, transport: RegisterTransport, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._transport = transport
        self._listener: Optional[GatewayListener] = None
        # One register exchange at a time: the ring poll must not interleave
        # with a batch's write-poll-read.
        self._lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task] = None
        self._monitor_seen: List[Optional[int]] = [None] * MONITOR_RING_SIZE
        self._closed = False
        self._monitor_on = False
        # Assume event sources exist until the daemon says otherwise: the
        # cautious default costs a few register reads, the other loses events.
        self._has_control_devices = True
        self._monitor_interval = self._pick_monitor_interval()
        self._round_trip_s = INITIAL_ROUND_TRIP_S

    @property
    def device_id(self) -> str:
        return self.config.device_name

    @property
    def connected(self) -> bool:
        return True

    @property
    def session(self) -> int:
        return 0

    @property
    def round_trip_s(self) -> float:
        """A smoothed measure of one register exchange, waiting for the port included."""
        return self._round_trip_s

    async def _read(self, address: int, count: int) -> List[int]:
        started = asyncio.get_running_loop().time()
        try:
            return await self._transport.read_input(self.device_id, address, count)
        finally:
            self._observe_round_trip(asyncio.get_running_loop().time() - started)

    async def _write(self, address: int, values: List[int]) -> None:
        started = asyncio.get_running_loop().time()
        try:
            await self._transport.write_holding(self.device_id, address, values)
        finally:
            self._observe_round_trip(asyncio.get_running_loop().time() - started)

    def _observe_round_trip(self, elapsed: float) -> None:
        self._round_trip_s += ROUND_TRIP_SMOOTHING * (elapsed - self._round_trip_s)

    async def start(self, listener: GatewayListener) -> None:
        self._listener = listener
        try:
            async with self._lock:
                registers = await self._read(
                    monitor_address(self.config.bus, 0), MONITOR_RING_SIZE * MONITOR_REGISTERS_PER_SLOT
                )
            self._monitor_seen = [
                from_monitor_registers(
                    registers[slot * MONITOR_REGISTERS_PER_SLOT : (slot + 1) * MONITOR_REGISTERS_PER_SLOT]
                )
                or None
                for slot in range(MONITOR_RING_SIZE)
            ]
        except Exception as error:  # pylint: disable=broad-exception-caught
            self.logger.warning("Baselining the bus monitor failed: %s", error)
            self._monitor_seen = [None] * MONITOR_RING_SIZE
        self._start_bus_monitor()

    async def stop(self) -> None:
        # A monitor toggle arriving after shutdown must not resurrect the
        # polling task against a stopped transport; see _start_bus_monitor.
        self._closed = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None

    async def reset_pointer(self) -> GatewayWriteResult:
        """Nothing to do: every batch rewinds the pointer before its write."""
        return GatewayWriteResult.DONE

    async def _write_pointer(self, slot: int) -> None:
        await self._write(queue_pointer_address(self.config.bus), [slot])

    def _poll_deadline(self, frames: int) -> float:
        # Each frame ahead of the polled slot needs its own bus time (46 ms
        # answered, more for send-twice) before the module can even reach it.
        return self.config.response_timeout_s + REPLY_TIME_PER_EXTRA_FRAME_S * (frames - 1)

    async def write_slots(self, first_slot: int, frames: List[int]) -> GatewayWriteResult:
        registers: List[int] = []
        for frame in frames:
            registers.extend(to_registers(frame))
        bus = self.config.bus
        values: List[int] = []
        async with self._lock:
            try:
                # Rewind first: the module only ever transmits the slot its
                # pointer is on, so this is what guarantees the frames go out.
                await self._write_pointer(0)
                await self._write(queue_slot_address(bus, 0), registers)
            except Exception as error:  # pylint: disable=broad-exception-caught
                self.logger.error("Writing a batch of %d frames failed: %s", len(frames), error)
                return GatewayWriteResult.FAILED
            try:
                if await self._poll_reply(len(frames) - 1, self._poll_deadline(len(frames))) is None:
                    self.logger.error("No reply for a batch of %d frames", len(frames))
                # Earlier slots may have been consumed before a stall; report
                # what actually happened, slot by slot.
                values = await self._read(reply_address(bus, 0), len(frames))
            except Exception as error:  # pylint: disable=broad-exception-caught
                # Written, so the frames may have gone out: the driver's reply clock decides.
                self.logger.error("Reading the replies of a batch failed: %s", error)
        replies = [
            (first_slot + offset, value)
            for offset, value in enumerate(values)
            if (value >> 8) != TransmissionStatus.NO_TRANSMISSION
        ]
        if replies:
            asyncio.get_running_loop().call_soon(self._deliver_replies, replies)
        return GatewayWriteResult.DONE

    def _deliver_replies(self, replies: List[tuple]) -> None:
        if self._listener is None or self._closed:
            return
        for slot, value in replies:
            self._listener.on_reply(slot, value)

    async def _poll_reply(self, slot: int, deadline_s: float) -> Optional[int]:
        address = reply_address(self.config.bus, slot)
        deadline = asyncio.get_running_loop().time() + deadline_s
        while True:
            value = (await self._read(address, 1))[0]
            if value >> 8 != TransmissionStatus.NO_TRANSMISSION:
                return value
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(REPLY_POLL_INTERVAL_S)

    # -- bus monitor ------------------------------------------------------------

    def set_bus_monitor_enabled(self, enabled: bool) -> None:
        """Quicken or relax the ring polling — never stop it.

        The operator's toggle decides how promptly foreign frames show up in
        the view; the daemon needs them regardless, because a DALI-2 sensor's
        readings arrive as event frames and nothing else updates them.
        """
        self._monitor_on = enabled
        self._monitor_interval = self._pick_monitor_interval()
        self._start_bus_monitor()

    def set_has_control_devices(self, present: bool) -> None:
        self._has_control_devices = present
        self._monitor_interval = self._pick_monitor_interval()

    def _pick_monitor_interval(self) -> float:
        if self._monitor_on:
            return MONITOR_POLL_INTERVAL_S
        return MONITOR_IDLE_POLL_INTERVAL_S if self._has_control_devices else MONITOR_QUIET_POLL_INTERVAL_S

    def _start_bus_monitor(self) -> None:
        if self._closed:
            return
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(
                self._poll_bus_monitor(), name=f"dali-monitor-{self.device_id}-{self.config.bus}"
            )

    async def _poll_bus_monitor(self) -> None:
        base = monitor_address(self.config.bus, 0)
        count = MONITOR_RING_SIZE * MONITOR_REGISTERS_PER_SLOT
        while True:
            await asyncio.sleep(self._monitor_interval)
            try:
                async with self._lock:
                    registers = await self._read(base, count)
            except Exception as error:  # pylint: disable=broad-exception-caught
                self.logger.warning("Reading the bus monitor failed: %s", error)
                continue
            # A slot keeps its value until the ring wraps onto it again, so the
            # previous read is what tells a new frame from one already reported.
            fresh: List[int] = []
            for slot in range(MONITOR_RING_SIZE):
                raw = from_monitor_registers(
                    registers[slot * MONITOR_REGISTERS_PER_SLOT : (slot + 1) * MONITOR_REGISTERS_PER_SLOT]
                )
                if raw in (0, self._monitor_seen[slot]):
                    continue
                self._monitor_seen[slot] = raw
                fresh.append(raw)
            if self._listener is None:
                continue
            # One poll can find several new frames, and the ring's slot order
            # is not their order on the bus: wb-mqtt-serial publishes slots one
            # change at a time, so the handler expects them by frame counter.
            for raw in sorted(fresh, key=_frame_counter_order(fresh)):
                self._listener.on_monitor_slot(raw)


def _frame_counter_order(raws: List[int]):
    """A sort key by frame counter that survives the counter wrapping inside one poll."""
    counters = [(raw >> 48) & 0xFFFF for raw in raws]
    wrapped = counters and max(counters) - min(counters) > FRAME_COUNTER_MODULO // 2

    def key(raw: int) -> int:
        counter = (raw >> 48) & 0xFFFF
        if wrapped and counter < FRAME_COUNTER_MODULO // 2:
            counter += FRAME_COUNTER_MODULO
        return counter

    return key
