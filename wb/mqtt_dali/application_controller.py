from dataclasses import dataclass, field

from .mqtt_dispatcher import MQTTDispatcher
from .wbdali import AsyncDeviceInstanceTypeMapper, WBDALIConfig, WBDALIDriver


@dataclass
class DaliDeviceAddress:
    short: int
    random: int


@dataclass
class DaliDevice:
    uid: str
    name: str
    address: DaliDeviceAddress
    groups: list[str] = field(default_factory=list)


class ApplicationController:
    def __init__(
        self,
        uid: str,
        bus_name: str,
        enable_lunaton: bool,
        devices: list[DaliDevice],
        mqtt_dispatcher: MQTTDispatcher,
    ) -> None:
        self.uid = uid
        self.bus_name = bus_name
        self.enable_lunaton = enable_lunaton
        self.devices = devices
        self.dev_inst_map = AsyncDeviceInstanceTypeMapper()
        self.mqtt_dispatcher = mqtt_dispatcher
        cfg = WBDALIConfig(
            modbus_port_path="/dev/ttyRS485-2",
            device_name=self.uid,
            modbus_slave_id=2,
        )
        self.dev = WBDALIDriver(cfg, mqtt_dispatcher=self.mqtt_dispatcher, dev_inst_map=self.dev_inst_map)

    async def start(self) -> None:
        await self.dev.initialize()

    async def stop(self) -> None:
        await self.dev.deinitialize()
