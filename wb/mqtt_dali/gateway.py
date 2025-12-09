import asyncio
from dataclasses import dataclass, field

from .application_controller import ApplicationController, DaliDevice, DaliDeviceAddress
from .mqtt_dispatcher import MQTTDispatcher
from .mqtt_rpc_server import MQTTRPCServer


@dataclass
class WbDaliGateway:
    uid: str
    buses: list[ApplicationController] = field(default_factory=list)


def bus_from_json(
    gateway_uid: str, bus_index: int, data: dict, mqtt_dispatcher: MQTTDispatcher
) -> ApplicationController:
    devices = []
    uid = f"{gateway_uid}_bus{bus_index}"
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
    return ApplicationController(
        uid=uid,
        bus_name=f"Bus {bus_index}",
        devices=devices,
        mqtt_dispatcher=mqtt_dispatcher,
    )


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
        await self.rpc_server.start()
        await self.rpc_server.add_endpoint(
            "Editor",
            "GetList",
            self.get_list_rpc_handler,
        )
        res = await asyncio.gather(
            *[bus.start() for gw in self.wb_dali_gateways for bus in gw.buses],
            return_exceptions=True,
        )
        for r in res:
            if isinstance(r, Exception):
                raise r

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
                    "buses": list(
                        map(
                            lambda bus: {
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
                            },
                            gw.buses,
                        )
                    ),
                },
                self.wb_dali_gateways,
            )
        )
