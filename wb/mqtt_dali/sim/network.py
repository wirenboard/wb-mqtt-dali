"""A Modbus network of simulated WB-DALI modules.

The bottom of the stack in simulation mode: it implements the same
`RegisterTransport` the driver uses against real hardware, so nothing above it
can tell the difference.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List

from .dali_bus import SimulatedDaliBus
from .gateway import VirtualWbDaliGateway

logger = logging.getLogger("wb.mqtt_dali.sim.network")

# How long the simulator may run without yielding, when no bus time is being
# simulated. One animation frame: long enough that the yields are rare, short
# enough that the page still repaints during a scan.
MAX_UNINTERRUPTED_S = 0.016


class SimulatedModbusNetwork:
    """Modbus registers backed by simulated WB-DALI modules.

    :param frame_delay_s: wall-clock cost charged per DALI frame. Zero keeps the
        UI instant; a realistic value (~0.035 s for a query round trip) makes the
        driver's timeouts behave as they do on real hardware, at the price of a
        slow bus scan.
    """

    def __init__(self, frame_delay_s: float = 0.0) -> None:
        self.gateways: Dict[str, VirtualWbDaliGateway] = {}
        self.frame_delay_s = frame_delay_s
        # A real module has one RS-485 link and serves requests in order.
        self._locks: Dict[str, asyncio.Lock] = {}
        self._yielded_at = 0.0
        self._event_tasks: List[asyncio.Task] = []

    def add_module(self, device_id: str, buses: Dict[int, SimulatedDaliBus]) -> VirtualWbDaliGateway:
        """Put a WB-DALI module with the given buses on the network under a Modbus device id."""
        gateway = VirtualWbDaliGateway(buses)
        self.gateways[device_id] = gateway
        return gateway

    def press_button(self, device_id: str, bus: int, device_index: int, instance: int = 0) -> bool:
        """Press a simulated wall switch.

        The frame goes into the gateway's bus monitor ring and nowhere else: a
        control device is a bus master in its own right, so nothing answers it
        and no reply register records it.
        """
        gateway = self.gateways.get(device_id)
        if gateway is None or bus not in gateway.buses:
            return False
        devices = gateway.buses[bus].dali_bus.devices
        if device_index >= len(devices):
            return False
        frame = devices[device_index].press(instance)
        if frame is None:
            return False
        gateway.record_bus_frame(bus, len(frame), frame.as_integer)
        return True

    def schedule_button_presses(self, device_id: str, bus: int, device_index: int, interval_s: float) -> None:
        """Have a simulated switch press itself, so there is traffic to watch."""

        async def press_forever() -> None:
            while True:
                await asyncio.sleep(interval_s)
                self.press_button(device_id, bus, device_index)

        self._event_tasks.append(asyncio.create_task(press_forever(), name=f"dali-switch-{device_id}-{bus}"))

    def stop(self) -> None:
        """Cancel every self-pressing switch."""
        for task in self._event_tasks:
            task.cancel()
        self._event_tasks.clear()

    # -- RegisterTransport ------------------------------------------------

    async def read_input(self, device_id: str, address: int, count: int) -> List[int]:
        """Modbus function 4 against one module, charged one frame of bus time."""
        gateway = self._require(device_id)
        async with self._lock_for(device_id):
            await self._charge_bus_time(1)
            return gateway.read_input(address, count)

    async def write_holding(self, device_id: str, address: int, values: List[int]) -> None:
        """Modbus function 6/16 against one module, charged one frame per slot written."""
        gateway = self._require(device_id)
        async with self._lock_for(device_id):
            # A queue slot is two registers, so the register count is the frame
            # count. Charge the bus time before transmitting, so an answer never
            # appears earlier than the frame that produced it could have.
            await self._charge_bus_time(max(1, len(values) // 2))
            gateway.write_holding(address, values)

    def _require(self, device_id: str) -> VirtualWbDaliGateway:
        gateway = self.gateways.get(device_id)
        if gateway is None:
            raise ValueError(f"No device {device_id!r} on the bus")
        if not gateway.reachable:
            raise TimeoutError(f"Device {device_id!r} did not answer")
        return gateway

    def _lock_for(self, device_id: str) -> asyncio.Lock:
        lock = self._locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[device_id] = lock
        return lock

    async def _charge_bus_time(self, frames: int) -> None:
        if self.frame_delay_s and frames > 0:
            await asyncio.sleep(self.frame_delay_s * frames)
            return

        # With no simulated bus time there is still the event loop to think
        # about: a bus scan is tens of thousands of register operations, and a
        # long enough run of them without yielding freezes the page.
        #
        # Yielding on *every* operation is worse, though. Under Pyodide each
        # yield is a `setTimeout`, which browsers clamp to about 4 ms once
        # nested — so a scan that takes seconds in a worker took minutes on the
        # main thread, which is where the offline build has to run. Yielding by
        # elapsed time instead bounds how long the page can be blocked without
        # paying a clamped timer per register access.
        now = asyncio.get_running_loop().time()
        if now - self._yielded_at >= MAX_UNINTERRUPTED_S:
            self._yielded_at = now
            await asyncio.sleep(0)
