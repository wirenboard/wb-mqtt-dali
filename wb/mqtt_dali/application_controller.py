import asyncio
from typing import Optional

from dali.address import DeviceBroadcast
from dali.device.general import StartQuiescentMode, StopQuiescentMode

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
        self.dev_inst_map = AsyncDeviceInstanceTypeMapper()
        self.mqtt_dispatcher = mqtt_dispatcher
        cfg = WBDALIConfig(
            modbus_port_path="/dev/ttyRS485-2",
            device_name=mqtt_device_id,
            modbus_slave_id=2,
        )
        self.dev = WBDALIDriver(cfg, mqtt_dispatcher=self.mqtt_dispatcher, dev_inst_map=self.dev_inst_map)
        self._commissioning_task: Optional[asyncio.Task] = None
        self._websocket_enabled = websocket_enabled
        self._websocket_port = websocket_port
        self._websocket_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        await self.dev.initialize()

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

        await self.dev.deinitialize()
        if self._commissioning_task:
            self._commissioning_task.cancel()
            try:
                await self._commissioning_task
            except asyncio.CancelledError:
                pass

    async def rescan_bus(self) -> None:
        if not self._commissioning_task or self._commissioning_task.done():
            self._commissioning_task = asyncio.create_task(self._commissioning())
        await self._commissioning_task

    def is_commissioning(self) -> bool:
        return self._commissioning_task is not None and not self._commissioning_task.done()

    async def _commissioning(self):
        await asyncio.sleep(1)
        await self.dev.send(StartQuiescentMode(DeviceBroadcast()))
        try:
            obj = Commissioning(self.dev, [d.address for d in self.devices])
            res = await obj.smart_extend()
        finally:
            await self.dev.send(StopQuiescentMode(DeviceBroadcast()))

        unchanged_devices = [d for d in self.devices if d.address in res.unchanged]
        changed_devices = [make_device(self.uid, d.new) for d in res.changed]
        new_devices = [make_device(self.uid, addr) for addr in res.new]
        self.devices = unchanged_devices + changed_devices + new_devices
        self.devices.sort(key=lambda d: d.address.short)

    async def _run_websocket(self) -> None:
        try:
            await run_websocket(
                self.dev,
                asyncio,
                "0.0.0.0",
                self._websocket_port,
            )
        except asyncio.CancelledError:
            raise
