import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from itertools import chain
from typing import Optional, Tuple, Union

from .application_controller import (
    ApplicationController,
    ApplicationControllerConfig,
    WebSocketConfig,
)
from .common_dali_device import DaliDeviceAddress
from .dali2_device import Dali2Device
from .dali_device import DaliDevice
from .gtin_db import DaliDatabase
from .mqtt_dispatcher import MQTTDispatcher
from .mqtt_rpc_client import rpc_call, wait_for_rpc_endpoint
from .mqtt_rpc_server import MQTTRPCServer
from .wbmqtt import remove_topics_by_driver

DEFAULT_POLLING_INTERVAL = 5.0


def check_short_address_conflict(
    devices: Union[list[DaliDevice], list[Dali2Device]],
    target_device: Union[DaliDevice, Dali2Device],
    new_short: int,
) -> None:
    """Raise ValueError if new_short is already used by another device on the same bus."""
    if any(device is not target_device and device.address.short == new_short for device in devices):
        raise ValueError(f"Short address {new_short} is already used by another device on this bus")


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


def check_mqtt_id_conflict(
    gateways: list[WbDaliGateway],
    target_device: Union[DaliDevice, Dali2Device],
    new_mqtt_id: str,
) -> None:
    """Raise ValueError if new_mqtt_id is already used by another device across all gateways."""
    for gw in gateways:
        for bus in gw.buses:
            all_devices = chain(bus.dali_devices, bus.dali2_devices)
            if any(device is not target_device and device.mqtt_id == new_mqtt_id for device in all_devices):
                raise ValueError(f"mqtt_id '{new_mqtt_id}' is already used by another device")


def bus_from_json(
    gateway_mqtt_device_id: str,
    bus_index: int,
    data: dict,
    mqtt_dispatcher: MQTTDispatcher,
    gtin_db: DaliDatabase,
) -> ApplicationController:
    dali_devices: list[DaliDevice] = []
    dali2_devices: list[Dali2Device] = []
    bus_uid = f"{gateway_mqtt_device_id}_bus_{bus_index}"
    for dev_conf in data.get("devices", []):
        if dev_conf.get("dali2", False):
            device = Dali2Device(
                DaliDeviceAddress(dev_conf["short"], dev_conf["random"]),
                bus_uid,
                gtin_db,
                dev_conf.get("mqtt_id"),
                dev_conf.get("name"),
            )
            dali2_devices.append(device)
        else:
            device = DaliDevice(
                DaliDeviceAddress(dev_conf["short"], dev_conf["random"]),
                bus_uid,
                gtin_db,
                dev_conf.get("mqtt_id"),
                dev_conf.get("name"),
            )
            dali_devices.append(device)

    websocket_conf = WebSocketConfig(
        enabled=data.get("websocket_enabled", False),
        port=data.get("websocket_port", 8080),
    )
    polling_interval = data.get("polling_interval", DEFAULT_POLLING_INTERVAL)
    ap_conf = ApplicationControllerConfig(
        gateway_mqtt_device_id,
        bus_index,
        dali_devices,
        dali2_devices,
        polling_interval,
        websocket_conf,
        data.get("old_gateway", False),
        data.get("bus_monitor_enabled", False),
    )

    res = ApplicationController(ap_conf, mqtt_dispatcher, gtin_db)
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
                    "groups": sorted(dev.groups),
                },
                bus.dali_devices + bus.dali2_devices,
            )
        ),
    }


class Gateway:
    def __init__(
        self,
        config: dict,
        mqtt_dispatcher: MQTTDispatcher,
        config_path: str,
        gtin_db: DaliDatabase,
    ) -> None:
        self.rpc_server = MQTTRPCServer("wb-mqtt-dali", mqtt_dispatcher)
        self.wb_dali_gateways: list[WbDaliGateway] = list(
            map(
                lambda gw_conf: WbDaliGateway(
                    uid=gw_conf["device_id"],
                    buses=list(
                        map(
                            lambda bus: bus_from_json(
                                gw_conf["device_id"], bus[0], bus[1], mqtt_dispatcher, gtin_db
                            ),
                            enumerate(gw_conf.get("buses", []), 1),
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

        self._gtin_db = gtin_db

    async def start(self) -> None:
        try:
            await remove_topics_by_driver(self._mqtt_dispatcher, "wb-mqtt-dali")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logging.debug("Failed to remove old MQTT topics for wb-mqtt-dali: %s", e)

        try:
            await wait_for_rpc_endpoint(
                "wb-mqtt-serial",
                "config",
                "Load",
                self._mqtt_dispatcher,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logging.warning("wb-mqtt-serial RPC endpoint is not available: %s", e)
            raise RuntimeError("Required RPC endpoint wb-mqtt-serial/config/Load is not available") from e

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
        await self.rpc_server.add_endpoint(
            "Editor",
            "GetGroup",
            self.get_group_rpc_handler,
        )
        await self.rpc_server.add_endpoint(
            "Editor",
            "SetGroup",
            self.set_group_rpc_handler,
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
        new_short = new_params.get("short_address")
        if new_short is not None and new_short != device.address.short:
            if isinstance(device, Dali2Device):
                check_short_address_conflict(bus.dali2_devices, device, new_short)
            else:
                check_short_address_conflict(bus.dali_devices, device, new_short)
        new_mqtt_id = new_params.get("mqtt_id")
        if new_mqtt_id is not None and new_mqtt_id != device.mqtt_id:
            check_mqtt_id_conflict(self.wb_dali_gateways, device, new_mqtt_id)
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
                            "polling_interval": bus.polling_interval,
                            "bus_monitor_enabled": bus.bus_monitor_enabled,
                        },
                        "schema": {
                            "type": "object",
                            "properties": {
                                "polling_interval": {
                                    "type": "number",
                                    "title": "Polling Interval",
                                    "default": 5,
                                    "propertyOrder": 1,
                                },
                                "websocket_enabled": {
                                    "title": "Lunatone DALI-2 IoT Gateway emulator",
                                    "type": "boolean",
                                    "description": "websocket_description",
                                    "format": "switch",
                                    "default": False,
                                    "propertyOrder": 2,
                                },
                                "websocket_port": {
                                    "title": "WebSocket port",
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 65535,
                                    "propertyOrder": 3,
                                },
                                "bus_monitor_enabled": {
                                    "type": "boolean",
                                    "title": "Bus Monitor",
                                    "default": False,
                                    "format": "switch",
                                    "propertyOrder": 4,
                                },
                            },
                            "translations": {
                                "ru": {
                                    "Lunatone DALI-2 IoT Gateway emulator": "Эмуляция шлюза Lunatone DALI-2 IoT",
                                    "WebSocket port": "Порт WebSocket'а",
                                    "Polling Interval": "Интервал опроса",
                                    "websocket_description": "Включение этой опции запустит WebSocket-сервер, который эмулирует Lunatone DALI-2 IoT Gateway. "
                                    "К нему можно подключить Lunatone DALI Cockpit для управления устройствами. "
                                    "В DALI Cockpit надо выбрать Network в качестве интерфейса шины, "
                                    "указать DALI-2 Display/DALI-2 IoT/DALI-2 WLAN, ввести адрес контроллера и порт, заданный ниже.",
                                    "Bus Monitor": "Монитор шины",
                                },
                                "en": {
                                    "websocket_description": "Enabling this option will start a WebSocket-server that emulates the Lunatone DALI-2 IoT Gateway. "
                                    "You can connect the Lunatone DALI Cockpit to it for device management. "
                                    "In DALI Cockpit, select Network as the bus interface, "
                                    "specify DALI-2 Display/DALI-2 IoT/DALI-2 WLAN, and enter the controller address and the port specified above.",
                                },
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
                    bus.set_polling_interval(new_config.get("polling_interval", bus.polling_interval))
                    bus.set_bus_monitor_enabled(
                        new_config.get("bus_monitor_enabled", bus.bus_monitor_enabled)
                    )
                    await self._save_configuration()
                    return {
                        "websocket_enabled": new_websocket_config.enabled,
                        "websocket_port": new_websocket_config.port,
                        "polling_interval": bus.polling_interval,
                        "bus_monitor_enabled": bus.bus_monitor_enabled,
                    }
        raise ValueError("Bus not found")

    async def get_group_rpc_handler(self, params: dict):
        group_id = params.get("groupId")
        if group_id is None:
            raise ValueError("groupId parameter is required")
        bus, group_index = self._get_bus_and_group_index_by_id(group_id)
        if bus is None or group_index is None:
            raise ValueError(f"Group {group_id} not found")
        return await bus.load_group_info(group_index)

    async def set_group_rpc_handler(self, params: dict):
        group_id = params.get("groupId")
        if group_id is None:
            raise ValueError("groupId parameter is required")
        new_params = params.get("config", {})
        bus, group_index = self._get_bus_and_group_index_by_id(group_id)
        if bus is None or group_index is None:
            raise ValueError(f"Group {group_id} not found")
        await bus.apply_group_parameters(group_index, new_params)
        return {}

    async def get_gateway_rpc_handler(self, params: dict):
        return {"config": {}, "schema": {}}

    def _is_websocket_port_in_use(self, port: int, bus_id: str) -> bool:
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                if bus.uid != bus_id and bus.websocket_config.enabled and bus.websocket_config.port == port:
                    return True
        return False

    def _get_bus_and_group_index_by_id(
        self, group_id: str
    ) -> Tuple[Optional[ApplicationController], Optional[int]]:
        bus_uid, sep, index_str = group_id.rpartition("_g")
        if not sep:
            return None, None
        try:
            group_index = int(index_str)
        except ValueError:
            return None, None
        if not 0 <= group_index <= 15:
            return None, None
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                if bus.uid == bus_uid:
                    return bus, group_index
        return None, None

    def _get_bus_and_device_by_id(
        self, device_id: str
    ) -> Tuple[Optional[ApplicationController], Optional[Union[DaliDevice, Dali2Device]]]:
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                for device in bus.dali_devices:
                    if device.uid == device_id:
                        return bus, device
                for device in bus.dali2_devices:
                    if device.uid == device_id:
                        return bus, device
        return None, None

    async def _save_configuration(self) -> None:
        async with self._config_lock:
            save_configuration(self._config_path, self._debug, self.wb_dali_gateways)

    async def _update_gateways(self) -> None:
        device_ids = set()
        # device_id to whether it's an old WB-MDALI (True) or actual WB-DALI device (False)
        old_gateway: dict[str, bool] = {}
        try:
            serial_config = await rpc_call("wb-mqtt-serial", "config", "Load", {}, self._mqtt_dispatcher)
        except Exception:  # pylint: disable=broad-exception-caught
            logging.debug("Failed to load wb-mqtt-serial configuration")
            return
        for port in serial_config.get("config", {}).get("ports", []):
            if port.get("enabled", True):
                for device in port.get("devices", []):
                    if not device.get("enabled", True):
                        continue
                    device_id = device.get("id")
                    if device.get("device_type") == "WB-DALI":
                        if device_id is None:
                            device_id = f"wb-dali_{device.get('slave_id', '')}"
                        device_ids.add(device_id)
                        old_gateway[device_id] = False
                    elif device.get("device_type") == "WB-MDALI":
                        if device_id is None:
                            device_id = f"wb-mdali_{device.get('slave_id', '')}"
                        device_ids.add(device_id)
                        old_gateway[device_id] = True
        async with self._config_lock:
            new_gateways = []
            for current_gw in self.wb_dali_gateways:
                if current_gw.uid in device_ids:
                    new_gateways.append(current_gw)
                    device_ids.remove(current_gw.uid)
                else:
                    await current_gw.stop()
            for did in device_ids:
                buses = []
                bus_count = 3
                is_old_gateway = old_gateway.get(did, False)
                if is_old_gateway:
                    # old gateway supports only one bus
                    bus_count = 1
                for bus_index in range(1, bus_count + 1):
                    apc_conf = ApplicationControllerConfig(
                        did, bus_index, [], [], DEFAULT_POLLING_INTERVAL, old_gateway=is_old_gateway
                    )
                    apc = ApplicationController(apc_conf, self._mqtt_dispatcher, self._gtin_db)
                    buses.append(apc)
                gw = WbDaliGateway(uid=did, buses=buses)
                new_gateways.append(gw)
                await gw.start()
            self.wb_dali_gateways = new_gateways
        await self._save_configuration()


def get_dict_for_device_config(device: Union[DaliDevice, Dali2Device]) -> dict:
    res: dict = {
        "short": device.address.short,
        "random": device.address.random,
    }
    if isinstance(device, Dali2Device):
        res["dali2"] = True
    if device.has_custom_mqtt_id:
        res["mqtt_id"] = device.mqtt_id
    if device.has_custom_name:
        res["name"] = device.name
    return res


def save_configuration(config_path: str, debug: bool, gateways: list[WbDaliGateway]) -> None:
    real_config_path = os.path.realpath(config_path)
    config_dir = os.path.dirname(real_config_path)
    config_path = real_config_path
    temp_fd, temp_path = tempfile.mkstemp(
        prefix="wb-mqtt-dali",
        suffix=".cfg.tmp",
        dir=config_dir,
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_f:
            config: dict = {
                "gateways": [
                    {
                        "device_id": gw.uid,
                        "buses": [
                            {
                                "websocket_enabled": bus.websocket_config.enabled,
                                "websocket_port": bus.websocket_config.port,
                                "polling_interval": bus.polling_interval,
                                "devices": [
                                    get_dict_for_device_config(dev)
                                    for dev in bus.dali_devices + bus.dali2_devices
                                ],
                                "old_gateway": bus.old_gateway,
                                "bus_monitor_enabled": bus.bus_monitor_enabled,
                            }
                            for bus in gw.buses
                        ],
                    }
                    for gw in gateways
                ]
            }
            if debug:
                config["debug"] = True
            json.dump(config, temp_f, indent=4)
        os.replace(temp_path, config_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
