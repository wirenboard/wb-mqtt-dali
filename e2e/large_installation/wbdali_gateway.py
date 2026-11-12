# WB-DALI gateway emulator: the per-bus send queue on the template
# register map, at the driver's queue addresses. A pointer written while a frame executes stays
# where it was written; an overwritten slot keeps the new command.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional

from dali.frame import ForwardFrame

from e2e.large_installation.dali_fakes import DaliBus
from e2e.large_installation.installation_profile import TimingProfile
from e2e.large_installation.template_device import (
    DeviceTemplate,
    RegisterKind,
    TemplateDevice,
)
from wb.mqtt_dali.wbdali import GatewayReplyStatus, WBDALIConfig

DRIVER = WBDALIConfig()
# The driver subscribes to the replies over MQTT and defines no register for them.
REPLIES_REGISTER = 1500
BUS_STATE_DISCRETE = 1000
BUS_POWERED_DISCRETE = 1020
FRAME_BITS_BY_SIZE_CODE = {0: 16, 1: 24, 2: 25}
NO_SEND_PRIORITY = 0


@dataclass(frozen=True)
class SlotFrame:
    raw: int
    frame: ForwardFrame
    sendtwice: bool
    priority: int


def decode_slot(raw: int) -> Optional[SlotFrame]:
    """Inverse of the driver's `encode_frame_for_modbus`; None for an unknown frame size."""
    bits = FRAME_BITS_BY_SIZE_CODE.get((raw >> 25) & 0x7)
    if bits is None:
        return None
    return SlotFrame(
        raw=raw,
        frame=ForwardFrame(bits, raw & ((1 << bits) - 1)),
        sendtwice=bool((raw >> 28) & 1),
        priority=(raw >> 29) & 0x7,
    )


def reply_value(status: GatewayReplyStatus, backward: int = 0) -> int:
    return (status.value << 8) | (backward & 0xFF)


@dataclass(frozen=True)
class Execution:
    """A slot taken for execution, with the queue state it was taken in."""

    slot: int
    raw: int
    frame: Optional[SlotFrame]
    pointer_epoch: int


class GatewayQueue:
    """Slots, pointer and reply registers of one bus.

    `pointer_epoch` changes on every pointer write.
    """

    def __init__(self) -> None:
        self.slots = [0] * DRIVER.queue_size
        self.replies = [0] * DRIVER.queue_size
        self.pointer = 0
        self.pointer_epoch = 0

    def write_words(self, first_word: int, words: list[int]) -> None:
        """Store slot words (low word first); the reply of every touched slot is zeroed."""
        for offset, word in enumerate(words):
            index = first_word + offset
            slot = index // 2
            if index % 2 == 0:
                self.slots[slot] = (self.slots[slot] & 0xFFFF0000) | word
            else:
                self.slots[slot] = (self.slots[slot] & 0x0000FFFF) | (word << 16)
            self.replies[slot] = 0

    def write_pointer(self, value: int) -> None:
        if value == 0:
            self.slots = [0] * DRIVER.queue_size
        self.pointer = value % DRIVER.queue_size
        self.pointer_epoch += 1

    def next_execution(self) -> Optional[Execution]:
        raw = self.slots[self.pointer]
        if raw == 0:
            return None
        return Execution(self.pointer, raw, decode_slot(raw), self.pointer_epoch)

    def complete(self, execution: Execution, reply: int) -> None:
        """Finish an execution; a slot or pointer written meanwhile keeps its new state."""
        self.replies[execution.slot] = reply
        if self.slots[execution.slot] == execution.raw:
            self.slots[execution.slot] = 0
        if execution.pointer_epoch == self.pointer_epoch:
            self.pointer = (execution.slot + 1) % DRIVER.queue_size


class GatewayBus:
    def __init__(
        self,
        index: int,
        dali_bus: DaliBus,
        timing: TimingProfile,
        on_completed: Callable[[GatewayBus, int], None],
    ) -> None:
        self.index = index
        self.queue = GatewayQueue()
        self.dali_bus = dali_bus
        self._timing = timing
        self._on_completed = on_completed
        self._wakeup = asyncio.Event()
        self._idle_since = 0.0

    @property
    def register_offset(self) -> int:
        return self.index * DRIVER.queue_modbus_bus_offset

    def wake(self) -> None:
        self._wakeup.set()

    async def run(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while (execution := self.queue.next_execution()) is not None:
                await self._execute(execution)

    def execute_now(self, frame: Optional[SlotFrame]) -> int:
        """Run a slot on the fake bus without timing; returns the reply register value."""
        if frame is None or frame.priority == NO_SEND_PRIORITY:
            return reply_value(GatewayReplyStatus.NO_TRANSMISSION)
        reply = self.dali_bus.transact(frame.frame)
        if reply.backward is None:
            return reply_value(GatewayReplyStatus.NO_BACKWARD_FRAME)
        if reply.collision:
            return reply_value(GatewayReplyStatus.BROKEN_BACKWARD_FRAME, reply.backward)
        return reply_value(GatewayReplyStatus.BACKWARD_FRAME, reply.backward)

    # --- Private ---

    async def _execute(self, execution: Execution) -> None:
        frame = execution.frame
        sent = frame is not None and frame.priority != NO_SEND_PRIORITY
        if sent:
            await _sleep_until(self._idle_since + self._timing.settling_s(frame.priority))
            forward_s = self._forward_s(frame)
            if frame.sendtwice:
                forward_s = 2 * forward_s + self._timing.settling_s(frame.priority)
            await asyncio.sleep(forward_s)
        reply = self.execute_now(frame)
        if sent:
            if reply >> 8 == GatewayReplyStatus.NO_BACKWARD_FRAME.value:
                await asyncio.sleep(self._timing.delay_s(self._timing.no_backward_wait_ms))
            else:
                await asyncio.sleep(
                    self._timing.delay_s(self._timing.backward_delay_ms + self._timing.backward_ms)
                )
        self._idle_since = asyncio.get_running_loop().time()
        self.queue.complete(execution, reply)
        self._on_completed(self, execution.slot)

    def _forward_s(self, frame: SlotFrame) -> float:
        return self._timing.delay_s(
            self._timing.forward_16_ms if len(frame.frame) == 16 else self._timing.forward_24_ms
        )


class EmulatedWbDali(TemplateDevice):
    def __init__(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        self,
        slave_id: int,
        template: DeviceTemplate,
        timing: TimingProfile,
        dali_buses: list[DaliBus],
        fw_version: str,
    ) -> None:
        super().__init__(slave_id, template, 0.0, fw_version)
        self.buses = [
            GatewayBus(index, bus, timing, self._on_slot_completed) for index, bus in enumerate(dali_buses)
        ]
        for bus in self.buses:
            base = bus.register_offset
            self.add_registers(
                RegisterKind.HOLDING,
                range(
                    DRIVER.queue_start_modbus_address + base,
                    DRIVER.queue_bulk_send_pointer_modbus_address + base + 1,
                ),
            )
            self.add_registers(
                RegisterKind.INPUT,
                range(REPLIES_REGISTER + base, REPLIES_REGISTER + base + DRIVER.queue_size),
            )
            for discrete in (BUS_STATE_DISCRETE + bus.index, BUS_POWERED_DISCRETE + bus.index):
                self.add_registers(RegisterKind.DISCRETE, range(discrete, discrete + 1))
                self.set_silently(RegisterKind.DISCRETE, discrete, 1)

    async def run(self) -> None:
        await asyncio.gather(*(bus.run() for bus in self.buses))

    # --- Hooks for subclasses ---

    def _on_write(self, kind: RegisterKind, values: list[tuple[int, int]]) -> None:
        if kind != RegisterKind.HOLDING:
            return
        for bus in self.buses:
            base = bus.register_offset
            words = [
                (a - DRIVER.queue_start_modbus_address - base, v)
                for a, v in values
                if 0 <= a - DRIVER.queue_start_modbus_address - base < 2 * DRIVER.queue_size
            ]
            pointer_values = [
                v for a, v in values if a == DRIVER.queue_bulk_send_pointer_modbus_address + base
            ]
            if not words and not pointer_values:
                continue
            if words:
                bus.queue.write_words(words[0][0], [v for _, v in words])
            for value in pointer_values:
                bus.queue.write_pointer(value)
            self._sync_registers(bus)
            bus.wake()

    # --- Private ---

    def _sync_registers(self, bus: GatewayBus) -> None:
        """Mirror the queue into the register map; nothing here raises an event."""
        for slot in range(DRIVER.queue_size):
            self._mirror_slot(bus, slot)
            self.set_silently(
                RegisterKind.INPUT, REPLIES_REGISTER + bus.register_offset + slot, bus.queue.replies[slot]
            )
        self.set_silently(
            RegisterKind.HOLDING,
            DRIVER.queue_bulk_send_pointer_modbus_address + bus.register_offset,
            bus.queue.pointer,
        )

    def _mirror_slot(self, bus: GatewayBus, slot: int) -> None:
        address = DRIVER.queue_start_modbus_address + bus.register_offset + 2 * slot
        self.set_silently(RegisterKind.HOLDING, address, bus.queue.slots[slot] & 0xFFFF)
        self.set_silently(RegisterKind.HOLDING, address + 1, bus.queue.slots[slot] >> 16)

    def _on_slot_completed(self, bus: GatewayBus, slot: int) -> None:
        self._mirror_slot(bus, slot)
        self.set(RegisterKind.INPUT, REPLIES_REGISTER + bus.register_offset + slot, bus.queue.replies[slot])
        self.set(
            RegisterKind.HOLDING,
            DRIVER.queue_bulk_send_pointer_modbus_address + bus.register_offset,
            bus.queue.pointer,
        )


async def _sleep_until(deadline: float) -> None:
    delay = deadline - asyncio.get_running_loop().time()
    if delay > 0:
        await asyncio.sleep(delay)
