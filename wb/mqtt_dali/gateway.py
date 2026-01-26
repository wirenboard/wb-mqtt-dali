import asyncio
import json
from dataclasses import dataclass, field
from typing import Optional, Tuple

from .application_controller import (
    ApplicationController,
    ApplicationControllerConfig,
    WebSocketConfig,
)
from .dali_device import DaliDevice, DaliDeviceAddress
from .mqtt_dispatcher import MQTTDispatcher
from .mqtt_rpc_client import rpc_call
from .mqtt_rpc_server import MQTTRPCServer


@dataclass
class WbDaliGateway:
    uid: str
    buses: list[ApplicationController] = field(default_factory=list)

    async def stop(self) -> None:
        res = await asyncio.gather(
            *[bus.stop() for bus in self.buses],
            return_exceptions=True,
        )
        for r in res:
            if isinstance(r, Exception):
                raise r

    async def start(self) -> None:
        res = await asyncio.gather(
            *[bus.start() for bus in self.buses],
            return_exceptions=True,
        )
        for r in res:
            if isinstance(r, Exception):
                raise r


def bus_from_json(
    gateway_mqtt_device_id: str, bus_index: int, data: dict, mqtt_dispatcher: MQTTDispatcher
) -> ApplicationController:
    devices = []
    uid = f"{gateway_mqtt_device_id}_bus_{bus_index}"
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

    websocket_conf = WebSocketConfig(
        enabled=data.get("websocket_enabled", False),
        port=data.get("websocket_port", 8080),
    )
    ap_conf = ApplicationControllerConfig(gateway_mqtt_device_id, bus_index, devices, websocket_conf)

    res = ApplicationController(ap_conf, mqtt_dispatcher)
    return res


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
    def __init__(self, config: dict, mqtt_dispatcher: MQTTDispatcher, config_path: str) -> None:
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

        self._mqtt_dispatcher = mqtt_dispatcher

        self._config_lock = asyncio.Lock()
        self._config_path = config_path
        self._debug = config.get("debug", False)

    async def start(self) -> None:
        res = await asyncio.gather(
            *[gw.start() for gw in self.wb_dali_gateways],
            return_exceptions=True,
        )
        await self._update_gateways()
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
            self.rescan_bus_rpc_handler,
        )
        await self.rpc_server.add_endpoint(
            "Editor",
            "GetDevice",
            self.get_device_rpc_handler,
        )
        await self.rpc_server.add_endpoint(
            "Editor",
            "SetDevice",
            self.set_device_rpc_handler,
        )
        await self.rpc_server.add_endpoint(
            "Editor",
            "GetBus",
            self.get_bus_rpc_handler,
        )
        await self.rpc_server.add_endpoint(
            "Editor",
            "SetBus",
            self.set_bus_rpc_handler,
        )
        await self.rpc_server.add_endpoint(
            "Editor",
            "GetGateway",
            self.get_gateway_rpc_handler,
        )

    async def stop(self) -> None:
        await self.rpc_server.stop()
        res = await asyncio.gather(
            *[gw.stop() for gw in self.wb_dali_gateways],
            return_exceptions=True,
        )
        for r in res:
            if isinstance(r, Exception):
                raise r

    async def get_list_rpc_handler(self, params: dict):
        await self._update_gateways()
        async with self._config_lock:
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

    async def get_device_rpc_handler(self, params: dict):
        device_id = params.get("deviceId")
        if device_id is None:
            raise ValueError("deviceId parameter is required")
        force_reload = params.get("forceReload", False)
        bus, device = self._get_bus_and_device_by_id(device_id)
        if bus is None or device is None:
            raise ValueError(f"Device {device_id} not found")
        await bus.load_device_info(device, force_reload)
        return {
            "config": device.params,
            "schema": device.schema,
        }

    async def set_device_rpc_handler(self, params: dict):
        device_id = params.get("deviceId")
        if device_id is None:
            raise ValueError("deviceId parameter is required")
        new_params = params.get("config", {})
        bus, device = self._get_bus_and_device_by_id(device_id)
        if bus is None or device is None:
            raise ValueError(f"Device {device_id} not found")
        await bus.apply_parameters(device, new_params)
        return device.params

    async def rescan_bus_rpc_handler(self, params: dict):
        bus_id = params.get("busId")
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                if bus.uid == bus_id:
                    await bus.rescan_bus()
                    await self._save_configuration()
                    return bus_to_json(bus)
        raise ValueError("Bus not found")

    async def get_bus_rpc_handler(self, params: dict):
        bus_id = params.get("busId")
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                if bus.uid == bus_id:
                    return {
                        "config": {
                            "websocket_enabled": bus.websocket_config.enabled,
                            "websocket_port": bus.websocket_config.port,
                        },
                        "schema": {
                            "type": "object",
                            "properties": {
                                "websocket_enabled": {
                                    "title": "Enable WebSocket",
                                    "type": "boolean",
                                },
                                "websocket_port": {
                                    "title": "WebSocket port",
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 65535,
                                },
                            },
                            "translations": {
                                "ru": {
                                    "Enable WebSocket": "Включить WebSocket",
                                    "WebSocket port": "Порт WebSocket'а",
                                }
                            },
                        },
                    }
        raise ValueError(f"Bus {bus_id} not found")

    async def set_bus_rpc_handler(self, params: dict):
        bus_id = params.get("busId")
        new_config = params.get("config", {})
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                if bus.uid == bus_id:
                    new_websocket_config = WebSocketConfig(
                        enabled=new_config.get("websocket_enabled", bus.websocket_config.enabled),
                        port=new_config.get("websocket_port", bus.websocket_config.port),
                    )
                    if new_websocket_config.enabled and self._is_websocket_port_in_use(
                        new_websocket_config.port, bus.uid
                    ):
                        raise ValueError(f"WebSocket port {new_websocket_config.port} is already in use")
                    await bus.setup_websocket(new_websocket_config)
                    await self._save_configuration()
                    return {
                        "websocket_enabled": new_websocket_config.enabled,
                        "websocket_port": new_websocket_config.port,
                    }
        raise ValueError("Bus not found")

    async def get_gateway_rpc_handler(self, params: dict):
        gateway_id = params.get("gatewayId")
        return {"config": {}, "schema": {}}

    def _is_websocket_port_in_use(self, port: int, bus_id: str) -> bool:
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                if bus.uid != bus_id and bus.websocket_config.enabled and bus.websocket_config.port == port:
                    return True
        return False

    def _get_bus_and_device_by_id(
        self, device_id: str
    ) -> Tuple[Optional[ApplicationController], Optional[DaliDevice]]:
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                for device in bus.devices:
                    if device.uid == device_id:
                        return bus, device
        return None, None

    async def _save_configuration(self) -> None:
        async with self._config_lock:
            with open(self._config_path, "w", encoding="utf-8") as f:
                config: dict = {
                    "gateways": [
                        {
                            "device_id": gw.uid,
                            "buses": [
                                {
                                    "websocket_enabled": bus.websocket_config.enabled,
                                    "websocket_port": bus.websocket_config.port,
                                    "devices": [
                                        {
                                            "short": dev.address.short,
                                            "random": dev.address.random,
                                        }
                                        for dev in bus.devices
                                    ],
                                }
                                for bus in gw.buses
                            ],
                        }
                        for gw in self.wb_dali_gateways
                    ]
                }
                if self._debug:
                    config["debug"] = True
                json.dump(config, f, indent=4)

    async def _update_gateways(self) -> None:
        device_ids = set()
        serial_config = await rpc_call("wb-mqtt-serial", "config", "Load", {}, self._mqtt_dispatcher)
        for port in serial_config.get("config", {}).get("ports", []):
            for device in port.get("devices", []):
                if device.get("device_type") == "WB-MDALI":
                    device_id = device.get("id")
                    if device_id is None:
                        device_id = f"wb-mdali_{device.get('slave_id', '')}"
                    device_ids.add(device_id)
        async with self._config_lock:
            new_gateways = []
            for current_gw in self.wb_dali_gateways:
                if current_gw.uid in device_ids:
                    new_gateways.append(current_gw)
                    device_ids.remove(current_gw.uid)
                else:
                    await current_gw.stop()
            for did in device_ids:
                apc_conf = ApplicationControllerConfig(did, 0, [])
                apc = ApplicationController(apc_conf, self._mqtt_dispatcher)
                gw = WbDaliGateway(uid=did, buses=[apc])
                new_gateways.append(gw)
                await gw.start()
            self.wb_dali_gateways = new_gateways
        await self._save_configuration()
