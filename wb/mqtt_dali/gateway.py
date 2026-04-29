import asyncio
import json
import logging
import os
import tempfile
from itertools import chain
from typing import Optional, Tuple, Union

from .application_controller import ApplicationController, ApplicationControllerConfig
from .common_dali_device import DaliDeviceAddress
from .dali2_device import Dali2Device
from .dali_device import DaliDevice
from .fake_lunatone_iot import run_websocket
from .gtin_db import DaliDatabase
from .mqtt_dispatcher import MQTTDispatcher
from .mqtt_rpc_client import rpc_call, wait_for_rpc_endpoint
from .mqtt_rpc_server import MQTTRPCServer
from .wbmqtt import remove_topics_by_driver

DEFAULT_POLLING_INTERVAL = 5.0
DEFAULT_WEBSOCKET_PORT = 8080
MIN_WEBSOCKET_PORT = 1
MAX_WEBSOCKET_PORT = 65535


def check_short_address_conflict(
    devices: Union[list[DaliDevice], list[Dali2Device]],
    target_device: Union[DaliDevice, Dali2Device],
    new_short: int,
) -> None:
    """Raise ValueError if new_short is already used by another device on the same bus."""
    if any(device is not target_device and device.address.short == new_short for device in devices):
        raise ValueError(f"Short address {new_short} is already used by another device on this bus")


class WbDaliGateway:

    def __init__(
        self,
        uid: str,
        buses: list[ApplicationController],
        websocket_enabled: bool = False,
        websocket_port: int = DEFAULT_WEBSOCKET_PORT,
    ) -> None:
        self.uid = uid
        self.buses = buses
        self.websocket_enabled = websocket_enabled
        self.websocket_port = websocket_port

        self._websocket_task: Optional[asyncio.Task] = None
        self._websocket_lock = asyncio.Lock()

    async def stop(self) -> None:
        await self._stop_websocket()
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
        async with self._websocket_lock:
            if self.websocket_enabled:
                self._start_websocket_task()

    async def apply_websocket_config(self, enabled: bool, port: int) -> None:
        async with self._websocket_lock:
            old_enabled = self.websocket_enabled
            old_port = self.websocket_port

            if enabled == old_enabled and port == old_port:
                return

            if not enabled:
                await self._stop_websocket_locked()
                self.websocket_enabled = False
                self.websocket_port = port
                if old_enabled:
                    for bus in self.buses:
                        bus.release_quiescent_mode()
                return

            if old_enabled and old_port != port:
                await self._stop_websocket_locked()

            self.websocket_enabled = True
            self.websocket_port = port

            if self._websocket_task is None:
                self._start_websocket_task()

    async def _stop_websocket(self) -> None:
        async with self._websocket_lock:
            await self._stop_websocket_locked()

    async def _stop_websocket_locked(self) -> None:
        if self._websocket_task is not None:
            self._websocket_task.cancel()
            try:
                await self._websocket_task
            except asyncio.CancelledError:
                # Task cancellation is expected when stopping the websocket; ignore this error.
                pass
            self._websocket_task = None

    def _start_websocket_task(self) -> None:
        drivers = [bus.driver for bus in self.buses]
        logger = logging.getLogger(self.uid)
        self._websocket_task = asyncio.create_task(
            run_websocket(drivers, self.uid, "0.0.0.0", self.websocket_port, logger),
            name=f"websocket-{self.uid}",
        )


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

    polling_interval = data.get("polling_interval", DEFAULT_POLLING_INTERVAL)
    ap_conf = ApplicationControllerConfig(
        gateway_mqtt_device_id,
        bus_index,
        dali_devices,
        dali2_devices,
        polling_interval,
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
    # pylint: disable=too-many-locals, too-many-branches
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
                    websocket_enabled=bool(gw_conf.get("websocket_enabled", False)),
                    websocket_port=int(gw_conf.get("websocket_port", DEFAULT_WEBSOCKET_PORT)),
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
            "SetGateway",
            self.set_gateway_rpc_handler,
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
        await self.rpc_server.add_endpoint(
            "Editor",
            "IdentifyDevice",
            self.identify_device_rpc_handler,
        )
        await self.rpc_server.add_endpoint(
            "Editor",
            "ResetDeviceSettings",
            self.reset_device_settings_rpc_handler,
        )
        await self.rpc_server.add_endpoint(
            "Editor",
            "ResetDevice",
            self.reset_device_rpc_handler,
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

    async def get_list_rpc_handler(self, _params: dict):
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
                    try:
                        schema = await bus.load_bus_info()
                    except Exception as e:  # pylint: disable=broad-exception-caught
                        logging.error("Failed to load bus info for bus %s: %s", bus_id, e)
                        schema = {
                            "type": "object",
                            "properties": {},
                        }
                    result = {
                        "config": {
                            "polling_interval": bus.polling_interval,
                            "bus_monitor_enabled": bus.bus_monitor_enabled,
                        },
                        "schema": schema,
                    }
                    return result
        raise ValueError(f"Bus {bus_id} not found")

    async def set_bus_rpc_handler(self, params: dict):
        bus_id = params.get("busId")
        new_config = dict(params.get("config", {}))
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                if bus.uid == bus_id:
                    bus.set_polling_interval(new_config.get("polling_interval", bus.polling_interval))
                    bus.set_bus_monitor_enabled(
                        new_config.get("bus_monitor_enabled", bus.bus_monitor_enabled)
                    )
                    for key in ["polling_interval", "bus_monitor_enabled"]:
                        new_config.pop(key, None)
                    if new_config:
                        await bus.apply_bus_parameters(new_config)
                    await self._save_configuration()
                    return {
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

    async def identify_device_rpc_handler(self, params: dict):
        device_id = params.get("deviceId")
        if device_id is None:
            raise ValueError("deviceId parameter is required")
        bus, device = self._get_bus_and_device_by_id(device_id)
        await bus.identify_device(device)
        return {}

    async def reset_device_settings_rpc_handler(self, params: dict):
        device_id = params.get("deviceId")
        if device_id is None:
            raise ValueError("deviceId parameter is required")
        bus, device = self._get_bus_and_device_by_id(device_id)
        await bus.reset_device_settings(device)
        return {}

    async def reset_device_rpc_handler(self, params: dict):
        device_id = params.get("deviceId")
        if device_id is None:
            raise ValueError("deviceId parameter is required")
        bus, device = self._get_bus_and_device_by_id(device_id)
        await bus.reset_device(device)
        await self._save_configuration()
        return {}

    async def get_gateway_rpc_handler(self, params: dict):
        gateway_id = params.get("gatewayId")
        if gateway_id is None:
            raise ValueError("gatewayId parameter is required")
        gw = self._find_gateway(gateway_id)
        return {
            "config": {
                "websocket_enabled": gw.websocket_enabled,
                "websocket_port": gw.websocket_port,
            },
            "schema": {},
        }

    async def set_gateway_rpc_handler(self, params: dict):
        gateway_id = params.get("gatewayId")
        if gateway_id is None:
            raise ValueError("gatewayId parameter is required")
        new_config = params.get("config", {}) or {}

        async with self._config_lock:
            gw = self._find_gateway(gateway_id)

            new_enabled = bool(new_config.get("websocket_enabled", gw.websocket_enabled))
            new_port = int(new_config.get("websocket_port", gw.websocket_port))

            if not MIN_WEBSOCKET_PORT <= new_port <= MAX_WEBSOCKET_PORT:
                raise ValueError(
                    f"WebSocket port {new_port} is out of range "
                    f"[{MIN_WEBSOCKET_PORT}..{MAX_WEBSOCKET_PORT}]"
                )

            if new_enabled and self._is_gateway_websocket_port_in_use(new_port, gw.uid):
                raise ValueError(f"WebSocket port {new_port} is already in use")

            await gw.apply_websocket_config(new_enabled, new_port)
            save_configuration(self._config_path, self._debug, self.wb_dali_gateways)
            return {
                "websocket_enabled": gw.websocket_enabled,
                "websocket_port": gw.websocket_port,
            }

    def _find_gateway(self, gateway_id: str) -> WbDaliGateway:
        for gw in self.wb_dali_gateways:
            if gw.uid == gateway_id:
                return gw
        raise ValueError(f"Gateway {gateway_id} not found")

    def _is_gateway_websocket_port_in_use(self, port: int, gateway_id: str) -> bool:
        for gw in self.wb_dali_gateways:
            if gw.uid != gateway_id and gw.websocket_enabled and gw.websocket_port == port:
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
    ) -> Tuple[ApplicationController, Union[DaliDevice, Dali2Device]]:
        for gw in self.wb_dali_gateways:
            for bus in gw.buses:
                for device in bus.dali_devices:
                    if device.uid == device_id:
                        return bus, device
                for device in bus.dali2_devices:
                    if device.uid == device_id:
                        return bus, device
        raise ValueError(f"Device {device_id} not found")

    async def _save_configuration(self) -> None:
        async with self._config_lock:
            save_configuration(self._config_path, self._debug, self.wb_dali_gateways)

    async def _update_gateways(self) -> None:
        device_ids = set()
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
                    if device.get("device_type") in ["WB-DALI", "WB-MDALI"]:
                        if device_id is None:
                            device_id = f"wb-dali_{device.get('slave_id', '')}"
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
                buses = []
                bus_count = 3
                for bus_index in range(1, bus_count + 1):
                    apc_conf = ApplicationControllerConfig(did, bus_index, [], [], DEFAULT_POLLING_INTERVAL)
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
                        "websocket_enabled": gw.websocket_enabled,
                        "websocket_port": gw.websocket_port,
                        "buses": [
                            {
                                "polling_interval": bus.polling_interval,
                                "devices": [
                                    get_dict_for_device_config(dev)
                                    for dev in bus.dali_devices + bus.dali2_devices
                                ],
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
