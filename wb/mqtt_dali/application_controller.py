import asyncio
from typing import Optional

from dali.address import DeviceBroadcast
from dali.device.general import StartQuiescentMode, StopQuiescentMode

from .bus_lock import BusLock
from .commissioning import Commissioning
from .dali_device import DaliDevice, make_device
from .fake_lunatone_iot import run_websocket
from .mqtt_dispatcher import MQTTDispatcher
from .wbdali import AsyncDeviceInstanceTypeMapper, WBDALIConfig, WBDALIDriver


class ApplicationController:
    def __init__(
        self,
        mqtt_device_id: str,
        bus_index: int,
        devices: list[DaliDevice],
        mqtt_dispatcher: MQTTDispatcher,
        websocket_enabled: bool = False,
        websocket_port: int = 8080,
    ) -> None:
        self.uid = f"{mqtt_device_id}_{bus_index}"
        self.bus_name = f"Bus {bus_index}"
        self.devices = devices

        self._bus_lock = BusLock()
        self._mqtt_dispatcher = mqtt_dispatcher
        self._dev_inst_map = AsyncDeviceInstanceTypeMapper()
        cfg = WBDALIConfig(
            modbus_port_path="/dev/ttyRS485-2",
            device_name=mqtt_device_id,
            modbus_slave_id=2,
        )
        self._dev = WBDALIDriver(cfg, mqtt_dispatcher=self._mqtt_dispatcher, dev_inst_map=self._dev_inst_map)
        self._commissioning_task: Optional[asyncio.Task] = None
        self._websocket_enabled = websocket_enabled
        self._websocket_port = websocket_port
        self._websocket_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        await self._dev.initialize()

        if self._websocket_enabled:
            self._websocket_task = asyncio.create_task(
                self._run_websocket(),
                name=f"websocket-{self.uid}",
            )

    async def stop(self) -> None:
        if self._websocket_task:
            self._websocket_task.cancel()
            try:
                await self._websocket_task
            except asyncio.CancelledError:
                pass
            self._websocket_task = None

        if self._commissioning_task:
            self._commissioning_task.cancel()
            try:
                await self._commissioning_task
            except asyncio.CancelledError:
                pass

        await self._dev.deinitialize()

    async def rescan_bus(self) -> None:
        if not self._commissioning_task or self._commissioning_task.done():
            self._commissioning_task = asyncio.create_task(self._commissioning())
        await self._commissioning_task

    def is_commissioning(self) -> bool:
        return self._commissioning_task is not None and not self._commissioning_task.done()

    async def _commissioning(self):
        async with self._bus_lock:
            await asyncio.sleep(1)
            await self._dev.send(StartQuiescentMode(DeviceBroadcast()))
            try:
                obj = Commissioning(self._dev, [d.address for d in self.devices])
                res = await obj.smart_extend()
            finally:
                await self._dev.send(StopQuiescentMode(DeviceBroadcast()))

            unchanged_devices = [d for d in self.devices if d.address in res.unchanged]
            changed_devices = [make_device(self.uid, d.new) for d in res.changed]
            new_devices = [make_device(self.uid, addr) for addr in res.new]
            self.devices = unchanged_devices + changed_devices + new_devices
            self.devices.sort(key=lambda d: d.address.short)

    async def _run_websocket(self) -> None:
        await run_websocket(
            self._dev,
            asyncio,
            "0.0.0.0",
            self._websocket_port,
        )
