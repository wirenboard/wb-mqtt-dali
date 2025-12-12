import asyncio
from dataclasses import dataclass, field

from .application_controller import ApplicationController
from .dali_device import DaliDevice, DaliDeviceAddress
from .mqtt_dispatcher import MQTTDispatcher
from .mqtt_rpc_server import MQTTRPCServer


@dataclass
class WbDaliGateway:
    uid: str
    buses: list[ApplicationController] = field(default_factory=list)


def bus_from_json(
    gateway_mqtt_device_id: str, bus_index: int, data: dict, mqtt_dispatcher: MQTTDispatcher
) -> ApplicationController:
    devices = []
    uid = f"{gateway_mqtt_device_id}_bus{bus_index}"
    for dev_conf in data.get("devices", []):
        device = DaliDevice(
            uid=f'{uid}_{dev_conf["short"]}',
            name=f'Dev {dev_conf["short"]}:{dev_conf["random"]}',
            address=DaliDeviceAddress(
                short=dev_conf["short"],
                random=dev_conf["random"],
            ),
        )
        devices.append(device)

    websocket_enabled = data.get("websocket_enabled", False)
    websocket_port = data.get("websocket_port", 8080)

    return ApplicationController(
        mqtt_device_id=gateway_mqtt_device_id,
        bus_index=bus_index,
        devices=devices,
        mqtt_dispatcher=mqtt_dispatcher,
        websocket_enabled=websocket_enabled,
        websocket_port=websocket_port,
    )


def bus_to_json(bus: ApplicationController) -> dict:
    return {
        "id": bus.uid,
        "name": bus.bus_name,
        "devices": list(
            map(
                lambda dev: {
                    "id": dev.uid,
                    "name": dev.name,
                    "groups": [],
                },
                bus.devices,
            )
        ),
        "groups": [],
        "isCommissioning": bus.is_commissioning(),
    }


class Gateway:
    def __init__(self, config: dict, mqtt_dispatcher: MQTTDispatcher) -> None:
        self.rpc_server = MQTTRPCServer("wb-mqtt-dali", mqtt_dispatcher)
        self.wb_dali_gateways: list[WbDaliGateway] = list(
            map(
                lambda gw_conf: WbDaliGateway(
                    uid=gw_conf["device_id"],
                    buses=list(
                        map(
                            lambda bus: bus_from_json(gw_conf["device_id"], bus[0], bus[1], mqtt_dispatcher),
                            enumerate(gw_conf.get("buses", [])),
                        )
                    ),
                ),
                config.get("gateways", []),
            )
        )

    async def start(self) -> None:
        res = await asyncio.gather(
            *[bus.start() for gw in self.wb_dali_gateways for bus in gw.buses],
            return_exceptions=True,
        )
        for r in res:
            if isinstance(r, Exception):
                raise r
        await self.rpc_server.start()
        await self.rpc_server.add_endpoint(
            "Editor",
            "GetList",
            self.get_list_rpc_handler,
        )
        await self.rpc_server.add_endpoint(
            "Editor",
            "ScanBus",
            self.rescan_bus_handler,
        )

    async def stop(self) -> None:
        await self.rpc_server.stop()
        res = await asyncio.gather(
            *[bus.stop() for gw in self.wb_dali_gateways for bus in gw.buses],
            return_exceptions=True,
        )
        for r in res:
            if isinstance(r, Exception):
                raise r

    async def get_list_rpc_handler(self, params: dict):
        return list(
            map(
                lambda gw: {
                    "id": gw.uid,
                    "name": gw.uid,
                    "buses": list(map(bus_to_json, gw.buses)),
                },
                self.wb_dali_gateways,
            )
        )

    async def rescan_bus_handler(self, params: dict):
        bus_id = params.get("busId")
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                if bus.uid == bus_id:
                    await bus.rescan_bus()
                    return bus_to_json(bus)
        raise ValueError("Bus not found")
