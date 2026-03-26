import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from timeit import default_timer
from typing import Any, Optional, Union

import paho.mqtt.client as mqtt
from dali.address import (
    DeviceBroadcast,
    DeviceShort,
    GearBroadcast,
    GearGroup,
    InstanceNumber,
)
from dali.command import Command, from_frame
from dali.device.general import StartQuiescentMode, StopQuiescentMode, _Event
from dali.frame import ForwardFrame, Frame
from dali.gear.general import EnableDeviceType

from .asyncio_utils import OneShotTasks
from .commissioning import Commissioning, CommissioningResult
from .common_dali_device import MqttControlBase
from .dali2_controls import publish_dali2_event
from .dali2_device import Dali2Device
from .dali_controls import make_controls
from .dali_device import DaliDevice
from .dali_type8_rgbwaf import get_wanted_mqtt_controls as rgbwaf_mqtt_controls
from .dali_type8_tc import get_wanted_mqtt_controls as tc_mqtt_controls
from .device_publisher import (
    ControlInfo,
    DeviceChange,
    DeviceInfo,
    DevicePublisher,
    TranslatedTitle,
)
from .fake_lunatone_iot import LUNATONE_IOT_EMULATOR_WBDALIDRIVER_SOURCE, run_websocket
from .gtin_db import DaliDatabase
from .mqtt_dispatcher import MQTTDispatcher
from .utils import merge_json_schemas
from .wbdali import WBDALIConfig, WBDALIDriver
from .wbdali_utils import AsyncDeviceInstanceTypeMapper
from .wbmdali import WBDALIConfig as WBDALIDriverOldConfig
from .wbmdali import WBDALIDriver as WBDALIDriverOld

MIN_TC_COLOUR = 100000
MAX_TC_COLOUR = 20000000


class ApplicationControllerState(Enum):
    UNINITIALIZED = auto()
    INITIALIZING = auto()
    READY = auto()
    STOPPING = auto()


@dataclass
class WebSocketConfig:
    enabled: bool = False
    port: int = 8080


@dataclass
class ApplicationControllerConfig:
    gateway_mqtt_device_id: str
    # Gateway bus number starting from 1
    bus: int
    dali_devices: list[DaliDevice]
    dali2_devices: list[Dali2Device]
    polling_interval: float
    websocket_config: WebSocketConfig = field(default_factory=WebSocketConfig)
    # Whether to use the old WB-MDALI gateway (True) or the new WB-DALI gateway (False)
    old_gateway: bool = False
    enable_bus_monitor: bool = False


class ApplicationControllerTaskType(Enum):
    APPLY_SETTING = auto()
    APPLY_GROUP_SETTING = auto()
    COMMISSIONING = auto()
    LOAD_INFO = auto()
    EXECUTE_CONTROL = auto()
    APPLY_BUS_SETTING = auto()


@dataclass
class ApplicationControllerTask:
    task_type: ApplicationControllerTaskType
    data: Any = field(default_factory=dict)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


INIT_RETRY_INITIAL_DELAY = 5.0
INIT_RETRY_MULTIPLIER = 2.0
INIT_RETRY_MAX_DELAY = 60.0


@dataclass
class DeviceInitState:
    next_retry_time: float = 0.0
    retry_count: int = 0


class GroupVirtualDevice:
    def __init__(
        self,
        mqtt_id: str,
        name: Union[str, TranslatedTitle],
        group_number: int,
    ) -> None:
        self.mqtt_id = mqtt_id
        self.name = name
        self.group_number = group_number
        self.logger = logging.getLogger()
        self._controls: dict[str, MqttControlBase] = {
            control.control_info.id: control
            for control in [
                *make_controls(),
                *rgbwaf_mqtt_controls(),
                *tc_mqtt_controls(MIN_TC_COLOUR, MAX_TC_COLOUR),
            ]
        }

    async def get_mqtt_controls(self, _driver: Union[WBDALIDriver, WBDALIDriverOld]) -> list[ControlInfo]:
        return [control.control_info for control in self._controls.values()]

    async def execute_control(
        self,
        driver: Union[WBDALIDriver, WBDALIDriverOld],
        control_id: str,
        value: str,
    ) -> None:
        control = self._controls.get(control_id)
        if control is not None and control.is_writable():
            await driver.send_commands(control.get_setup_commands(GearGroup(self.group_number), value))

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger


class BroadcastVirtualDevice:
    def __init__(self, mqtt_id: str, name: Union[str, TranslatedTitle]) -> None:
        self.mqtt_id = mqtt_id
        self.name = name
        self.logger = logging.getLogger()
        self._controls: dict[str, MqttControlBase] = {
            control.control_info.id: control
            for control in [
                *make_controls(),
                *rgbwaf_mqtt_controls(),
                *tc_mqtt_controls(MIN_TC_COLOUR, MAX_TC_COLOUR),
            ]
        }

    def get_mqtt_controls(self) -> list[ControlInfo]:
        return [control.control_info for control in self._controls.values()]

    async def execute_control(
        self,
        driver: Union[WBDALIDriver, WBDALIDriverOld],
        control_id: str,
        value: str,
    ) -> None:
        control = self._controls.get(control_id)
        if control is not None and control.is_writable():
            await driver.send_commands(control.get_setup_commands(GearBroadcast(), value))

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger


ControllableDevice = Union[DaliDevice, Dali2Device, BroadcastVirtualDevice, GroupVirtualDevice]


class ApplicationController:
    def __init__(
        self,
        config: ApplicationControllerConfig,
        mqtt_dispatcher: MQTTDispatcher,
        gtin_db: DaliDatabase,
    ) -> None:
        self.uid = f"{config.gateway_mqtt_device_id}_bus_{config.bus}"
        self.bus_name = f"Bus {config.bus}"
        self.dali_devices = config.dali_devices
        self.dali2_devices = config.dali2_devices
        self.websocket_config = config.websocket_config
        self.logger = logging.getLogger(self.uid)
        self.old_gateway = config.old_gateway

        self._state = ApplicationControllerState.UNINITIALIZED
        self._state_lock = asyncio.Lock()

        self._quiescent_mode_timer: Optional[asyncio.TimerHandle] = None

        self._mqtt_dispatcher = mqtt_dispatcher
        self._device_publisher = DevicePublisher(mqtt_dispatcher, self.logger)

        self._dev_inst_map = AsyncDeviceInstanceTypeMapper()

        self._polling_interval = config.polling_interval
        self._polling_task: Optional[asyncio.Task] = None

        # Special gear commands are preceded by EnableDeviceType
        # Store the type to correctly decode the following command frame
        self._last_bus_traffic_device_type: int = 0

        if config.old_gateway:
            cfg = WBDALIDriverOldConfig(
                device_name=config.gateway_mqtt_device_id,
            )
            self._dev = WBDALIDriverOld(cfg, mqtt_dispatcher, self.logger, self._dev_inst_map)
        else:
            cfg = WBDALIConfig(
                device_name=config.gateway_mqtt_device_id,
                bus=config.bus,
            )
            self._dev = WBDALIDriver(cfg, mqtt_dispatcher, self.logger, self._dev_inst_map)

        self._websocket_task: Optional[asyncio.Task] = None
        self._websocket_lock = asyncio.Lock()

        self._bus_monitor_topic = f"/wb-dali/{self.uid}/bus_monitor"
        self._bus_monitor_enabled = config.enable_bus_monitor
        self._bus_traffic_cleanup = self._dev.bus_traffic.register(self._handle_bus_traffic_frame)

        self._one_shot_tasks = OneShotTasks(self.logger)

        self._dali2_devices_by_addr: dict[int, Dali2Device] = {d.address.short: d for d in self.dali2_devices}
        self._devices_by_mqtt_id: dict[str, ControllableDevice] = {}
        self._broadcast_device = BroadcastVirtualDevice(
            mqtt_id=f"{self.uid}_broadcast",
            name=TranslatedTitle(f"{self.bus_name} Broadcast", f"{self.bus_name} широковещательный"),
        )
        self._group_devices_by_number: dict[int, GroupVirtualDevice] = {}

        self._gtin_db = gtin_db

        self._tasks_queue: asyncio.Queue[ApplicationControllerTask] = asyncio.Queue()

        self._in_quiescent_mode = False
        self._pending_init: dict[str, DeviceInitState] = {}  # mqtt_id -> state

        self._controls_to_execute: dict[tuple[ControllableDevice, str], str] = {}

    @property
    def polling_interval(self) -> float:
        return self._polling_interval

    @property
    def bus_monitor_enabled(self) -> bool:
        return self._bus_monitor_enabled

    def set_polling_interval(self, value: float) -> None:
        self._polling_interval = value

    def set_bus_monitor_enabled(self, enabled: bool) -> None:
        self._bus_monitor_enabled = enabled

    async def start(self) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.UNINITIALIZED:
                raise RuntimeError("ApplicationController must be in UNINITIALIZED state to start")
            self._state = ApplicationControllerState.INITIALIZING

        try:
            await self._dev.initialize()
        except Exception as e:
            async with self._state_lock:
                self._state = ApplicationControllerState.UNINITIALIZED
            raise RuntimeError("Failed to initialize WBDALIDriver") from e

        await self._device_publisher.initialize()
        broadcast_device_info = DeviceInfo(
            self._broadcast_device.mqtt_id,
            self._broadcast_device.name,
            self._broadcast_device.get_mqtt_controls(),
        )
        await self._device_publisher.add_device(broadcast_device_info)
        await self._device_publisher.register_control_handler(
            self._broadcast_device.mqtt_id,
            "+",
            self._handle_on_topic,
        )
        self._devices_by_mqtt_id[self._broadcast_device.mqtt_id] = self._broadcast_device
        self._broadcast_device.set_logger(self.logger)

        for device in self.dali_devices + self.dali2_devices:
            self._devices_by_mqtt_id[device.mqtt_id] = device
            device.set_logger(self.logger)
            self._schedule_device_init(device)

        async with self._state_lock:
            self._state = ApplicationControllerState.READY

        self._polling_task = asyncio.create_task(self._polling_loop())

        async with self._websocket_lock:
            if self.websocket_config.enabled:
                self._run_websocket()

    async def stop(self) -> None:
        async with self._state_lock:
            if self._state in (ApplicationControllerState.UNINITIALIZED, ApplicationControllerState.STOPPING):
                return
            if self._state == ApplicationControllerState.INITIALIZING:
                raise RuntimeError("ApplicationController %s must be initialized to stop" % self.uid)
            self._state = ApplicationControllerState.STOPPING

        self._bus_traffic_cleanup()

        await self._one_shot_tasks.stop()

        if self._quiescent_mode_timer:
            self._quiescent_mode_timer.cancel()

        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

        # Cancel any queued ApplicationControllerTasks so that callers awaiting
        # their futures do not hang during shutdown.
        try:
            while True:
                task = self._tasks_queue.get_nowait()
                task.future.cancel()
        except asyncio.QueueEmpty:
            pass

        self._controls_to_execute.clear()
        self._pending_init.clear()

        async with self._websocket_lock:
            await self._stop_websocket()

        await self._device_publisher.cleanup()
        await self._dev.deinitialize()
        async with self._state_lock:
            self._state = ApplicationControllerState.UNINITIALIZED

    async def rescan_bus(self) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.READY:
                raise RuntimeError("ApplicationController must be initialized")
            task = ApplicationControllerTask(ApplicationControllerTaskType.COMMISSIONING)
            self._tasks_queue.put_nowait(task)
        await task.future

    async def load_device_info(
        self, device: Union[DaliDevice, Dali2Device], force_reload: bool = False
    ) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.READY:
                raise RuntimeError("ApplicationController must be initialized")
            task = ApplicationControllerTask(ApplicationControllerTaskType.LOAD_INFO, (device, force_reload))
            self._tasks_queue.put_nowait(task)
        await task.future

    async def apply_parameters(self, device: Union[DaliDevice, Dali2Device], new_params: dict) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.READY:
                raise RuntimeError("ApplicationController must be initialized")
            task = ApplicationControllerTask(
                ApplicationControllerTaskType.APPLY_SETTING, (device, new_params)
            )
            self._tasks_queue.put_nowait(task)

        old_mqtt_id = device.mqtt_id
        old_short_address = device.address.short
        await task.future

        new_mqtt_id = device.mqtt_id
        if old_mqtt_id != new_mqtt_id:
            await self._device_publisher.remove_device(old_mqtt_id)
            self._devices_by_mqtt_id.pop(old_mqtt_id, None)
            await self._publish_device(device)
            self._devices_by_mqtt_id[new_mqtt_id] = device
            device.set_logger(self.logger)
        else:
            await self._device_publisher.set_device_title(old_mqtt_id, device.name)

        if isinstance(device, Dali2Device) and old_short_address != device.address.short:
            self._dev_inst_map.update_mapping(old_short_address, device.address.short)
            self._dali2_devices_by_addr.pop(old_short_address, None)
            self._dali2_devices_by_addr[device.address.short] = device

        if isinstance(device, DaliDevice):
            await self._refresh_group_virtual_devices()

    async def load_group_info(self, group_index: int) -> dict:
        res = {}
        for device in self.dali_devices:
            if device.is_initialized and group_index in device.groups:
                handlers = device.get_group_parameter_handlers()
                for handler in handlers:
                    merge_json_schemas(res, handler.get_schema(group_and_broadcast=True))
        return res

    async def apply_group_parameters(self, group_index: int, new_params: dict) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.READY:
                raise RuntimeError("ApplicationController must be initialized")
            task = ApplicationControllerTask(
                ApplicationControllerTaskType.APPLY_GROUP_SETTING, (group_index, new_params)
            )
            self._tasks_queue.put_nowait(task)
        await task.future

    async def load_bus_info(self) -> dict:
        res = {}
        for device in self.dali_devices:
            if device.is_initialized:
                handlers = device.get_group_parameter_handlers()
                for handler in handlers:
                    merge_json_schemas(res, handler.get_schema(group_and_broadcast=True))
        return res

    async def apply_bus_parameters(self, new_params: dict) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.READY:
                raise RuntimeError("ApplicationController must be initialized")
            task = ApplicationControllerTask(ApplicationControllerTaskType.APPLY_BUS_SETTING, new_params)
            self._tasks_queue.put_nowait(task)
        await task.future

    async def setup_websocket(self, config: WebSocketConfig) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.READY:
                self.websocket_config = config
                self.logger.debug(
                    "Trying to setup Lunatone IoT Gateway emulator in uninitialized state, just saving config",
                )
                return

            async with self._websocket_lock:
                if self.websocket_config == config:
                    self.logger.debug("Lunatone IoT Gateway emulator config unchanged, no action needed")
                    return

                # Disable websocket
                if not config.enabled:
                    self.logger.info("Stop Lunatone IoT Gateway emulator")
                    await self._stop_websocket()
                    self.websocket_config = config
                    return

                # Port changed, so stop existing websocket first
                if self.websocket_config.port != config.port:
                    self.logger.info("Lunatone IoT Gateway emulator port changed, restarting")
                    await self._stop_websocket()

                self.websocket_config = config
                self._run_websocket()

    async def _apply_group_parameters_task(self, group_index: int, new_params: dict) -> None:
        group_parameter_handlers = []
        group_parameter_types: set[tuple[str, str]] = set()
        for device in self.dali_devices:
            if device.is_initialized and group_index in device.groups:
                handlers = device.get_group_parameter_handlers()
                for handler in handlers:
                    handler_key = (type(handler).__name__, getattr(handler, "property_name", ""))
                    if handler_key not in group_parameter_types:
                        group_parameter_types.add(handler_key)
                        group_parameter_handlers.append(handler)
        for handler in group_parameter_handlers:
            await handler.write(self._dev, GearGroup(group_index), new_params)

    async def _apply_bus_parameters_task(self, new_params: dict) -> None:
        bus_parameter_handlers = []
        bus_parameter_types: set[tuple[str, str]] = set()
        for device in self.dali_devices:
            if device.is_initialized:
                handlers = device.get_group_parameter_handlers()
                for handler in handlers:
                    handler_key = (type(handler).__name__, getattr(handler, "property_name", ""))
                    if handler_key not in bus_parameter_types:
                        bus_parameter_types.add(handler_key)
                        bus_parameter_handlers.append(handler)
        for handler in bus_parameter_handlers:
            await handler.write(self._dev, GearBroadcast(), new_params)

    async def _commissioning_task(self):
        start_time = default_timer()

        await asyncio.sleep(1)
        await self._dev.send(StartQuiescentMode(DeviceBroadcast()))
        try:
            self.logger.debug("Commissioning for DALI 1.0 devices")
            obj = Commissioning(self._dev, [d.address for d in self.dali_devices], dali2=False)
            res_dali = await obj.smart_extend()
            self.logger.debug("Commissioning for DALI 2.0 devices")
            obj = Commissioning(self._dev, [d.address for d in self.dali2_devices], dali2=True)
            res_dali2 = await obj.smart_extend()
        finally:
            await self._dev.send(StopQuiescentMode(DeviceBroadcast()))

        end_time = default_timer()
        self.logger.debug("Commissioning completed in %.2f seconds", end_time - start_time)

        await self._update_dali_devices(res_dali)
        await self._update_dali2_devices(res_dali2)

    async def _update_dali_devices(self, commissioning_result: CommissioningResult) -> None:
        unchanged_devices = [d for d in self.dali_devices if d.address in commissioning_result.unchanged]
        changed_devices = [DaliDevice(d.new, self.uid, self._gtin_db) for d in commissioning_result.changed]
        new_devices = [DaliDevice(addr, self.uid, self._gtin_db) for addr in commissioning_result.new]

        created_devices = changed_devices + new_devices

        removed_short_addresses: set[int] = {d.old_short for d in commissioning_result.changed} | {
            d.short for d in commissioning_result.missing
        }
        removed_ids = [d.mqtt_id for d in self.dali_devices if d.address.short in removed_short_addresses]
        changes = DeviceChange(removed=removed_ids)
        await self._device_publisher.rebuild(changes)

        for removed_id in removed_ids:
            self._devices_by_mqtt_id.pop(removed_id, None)
            self._pending_init.pop(removed_id, None)

        for device in created_devices:
            device.set_logger(self.logger)
            self._schedule_device_init(device)

        self.dali_devices = unchanged_devices + created_devices
        self.dali_devices.sort(key=lambda d: d.address.short)
        self._devices_by_mqtt_id.update({d.mqtt_id: d for d in created_devices})
        await self._refresh_group_virtual_devices()

    async def _update_dali2_devices(self, commissioning_result: CommissioningResult) -> None:
        unchanged_devices = [d for d in self.dali2_devices if d.address in commissioning_result.unchanged]
        changed_devices = [Dali2Device(d.new, self.uid, self._gtin_db) for d in commissioning_result.changed]
        new_devices = [Dali2Device(addr, self.uid, self._gtin_db) for addr in commissioning_result.new]

        created_devices = changed_devices + new_devices
        new_dali2_devices = unchanged_devices + created_devices

        removed_short_addresses: set[int] = {d.old_short for d in commissioning_result.changed} | {
            d.short for d in commissioning_result.missing
        }
        removed_ids = [d.mqtt_id for d in self.dali2_devices if d.address.short in removed_short_addresses]
        changes = DeviceChange(removed=removed_ids)
        await self._device_publisher.rebuild(changes)

        for removed_id in removed_ids:
            self._devices_by_mqtt_id.pop(removed_id, None)
            self._pending_init.pop(removed_id, None)

        for device in created_devices:
            device.set_logger(self.logger)
            self._schedule_device_init(device)

        self.dali2_devices = new_dali2_devices
        self.dali2_devices.sort(key=lambda d: d.address.short)
        self._devices_by_mqtt_id.update({d.mqtt_id: d for d in created_devices})
        self._dali2_devices_by_addr = {d.address.short: d for d in self.dali2_devices}

    async def _handle_on_topic(self, message: mqtt.MQTTMessage) -> None:
        topic_parts = message.topic.split("/")
        if len(topic_parts) < 5:
            self.logger.warning("Received MQTT message with invalid topic format: %s", message.topic)
            return
        device_id = topic_parts[2]
        control_id = topic_parts[4]
        payload = message.payload.decode("utf-8") if getattr(message, "payload", None) else ""

        device = self._devices_by_mqtt_id.get(device_id)
        if device is None:
            return

        key = (device, control_id)
        if key in self._controls_to_execute:
            self._controls_to_execute[key] = payload
            self.logger.debug(
                "Received new command for control %s of device %s while previous command is still pending",
                control_id,
                device_id,
            )
            return
        self._controls_to_execute[key] = payload
        try:
            task = ApplicationControllerTask(ApplicationControllerTaskType.EXECUTE_CONTROL, key)
            self._tasks_queue.put_nowait(task)
            await task.future
            await self._device_publisher.set_control_error(device_id, control_id, "")
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error("Error executing control %s for device %s: %s", control_id, device_id, e)
            await self._device_publisher.set_control_error(device_id, control_id, "w")

    def _get_active_group_numbers(self) -> list[int]:
        active_groups = set()
        for device in self.dali_devices:
            active_groups.update(device.groups)
        return sorted(active_groups)

    def _make_group_virtual_device(self, group_number: int) -> GroupVirtualDevice:
        return GroupVirtualDevice(
            mqtt_id=f"{self.uid}_group_{group_number:02d}",
            name=TranslatedTitle(
                f"{self.bus_name} Group {group_number}",
                f"{self.bus_name} группа {group_number}",
            ),
            group_number=group_number,
        )

    async def _publish_group_virtual_device(self, device: GroupVirtualDevice) -> None:
        device_info = DeviceInfo(
            device.mqtt_id,
            device.name,
            await device.get_mqtt_controls(self._dev),
        )
        await self._device_publisher.add_device(device_info)
        await self._device_publisher.register_control_handler(
            device.mqtt_id,
            "+",
            self._handle_on_topic,
        )
        self._devices_by_mqtt_id[device.mqtt_id] = device
        device.set_logger(self.logger)

    def _schedule_device_init(self, device: Union[DaliDevice, Dali2Device], delay: float = 0.0) -> None:
        if device.mqtt_id not in self._pending_init:
            self._pending_init[device.mqtt_id] = DeviceInitState(
                next_retry_time=default_timer() + delay,
            )

    async def _try_init_pending_device(self, current_time: float) -> None:
        for mqtt_id, state in list(self._pending_init.items()):
            if state.next_retry_time > current_time:
                continue

            device = self._devices_by_mqtt_id.get(mqtt_id)
            if device is None or not isinstance(device, (DaliDevice, Dali2Device)):
                del self._pending_init[mqtt_id]
                continue

            if device.is_initialized:
                if not self._device_publisher.has_device(mqtt_id):
                    await self._publish_device(device)
                del self._pending_init[mqtt_id]
                continue

            try:
                self.logger.info(
                    "Initializing device %s (attempt %d)",
                    device.name,
                    state.retry_count + 1,
                )
                await device.initialize(self._dev)
                if self._device_publisher.has_device(mqtt_id):
                    await self._device_publisher.remove_device(mqtt_id)
                await self._publish_device(device)
                del self._pending_init[mqtt_id]
                self.logger.info("Device %s initialized successfully", device.name)
                if isinstance(device, DaliDevice):
                    await self._refresh_group_virtual_devices()
                elif isinstance(device, Dali2Device):
                    self._update_dali2_device_instance_map(device)
            except Exception as e:  # pylint: disable=broad-exception-caught
                state.retry_count += 1
                delay = min(
                    INIT_RETRY_INITIAL_DELAY * (INIT_RETRY_MULTIPLIER ** (state.retry_count - 1)),
                    INIT_RETRY_MAX_DELAY,
                )
                state.next_retry_time = current_time + delay
                self.logger.warning(
                    "Failed to initialize device %s (attempt %d), next retry in %.0fs: %s",
                    device.name,
                    state.retry_count,
                    delay,
                    e,
                )
                if not self._device_publisher.has_device(mqtt_id):
                    await self._publish_device_with_error(device)
            return  # one device per call

    async def _publish_device(self, device: Union[DaliDevice, Dali2Device]) -> None:
        device_info = DeviceInfo(
            device.mqtt_id,
            device.name,
            device.get_mqtt_controls(),
        )
        await self._device_publisher.add_device(device_info)
        await self._device_publisher.register_control_handler(
            device.mqtt_id,
            "+",
            self._handle_on_topic,
        )

    async def _publish_device_with_error(self, device: Union[DaliDevice, Dali2Device]) -> None:
        common_controls = device.get_common_mqtt_controls()
        controls = [c.control_info for c in common_controls]
        device_info = DeviceInfo(device.mqtt_id, device.name, controls)
        await self._device_publisher.add_device(device_info)
        await self._device_publisher.register_control_handler(
            device.mqtt_id,
            "+",
            self._handle_on_topic,
        )
        for control in common_controls:
            if control.is_readable():
                await self._device_publisher.set_control_error(device.mqtt_id, control.control_info.id, "r")

    def _update_dali2_device_instance_map(self, device: Dali2Device) -> None:
        addr = DeviceShort(device.address.short)
        for inst_num, inst_params in device.instances.items():
            self._dev_inst_map.add_type(
                short_address=addr,
                instance_number=InstanceNumber(inst_num),
                instance_type=inst_params.instance_type,
            )

    async def _refresh_group_virtual_devices(self) -> None:
        active_groups = set(self._get_active_group_numbers())
        existing_groups = set(self._group_devices_by_number)
        self.logger.debug(
            "Refreshing group virtual devices: active=%s existing=%s",
            sorted(active_groups),
            sorted(existing_groups),
        )

        for group_number in sorted(existing_groups - active_groups):
            device = self._group_devices_by_number.pop(group_number)
            self.logger.debug(
                "Removing group virtual device: group=%d mqtt_id=%s",
                group_number,
                device.mqtt_id,
            )
            await self._device_publisher.remove_device(device.mqtt_id)
            self._devices_by_mqtt_id.pop(device.mqtt_id, None)

        for group_number in sorted(active_groups - existing_groups):
            device = self._make_group_virtual_device(group_number)
            self.logger.debug(
                "Adding group virtual device: group=%d mqtt_id=%s",
                group_number,
                device.mqtt_id,
            )
            await self._publish_group_virtual_device(device)
            self._group_devices_by_number[group_number] = device

    async def _polling_loop(self) -> None:
        devices = []
        last_poll_time = default_timer() - self._polling_interval
        queue_timeout = 0.001
        item = None
        while True:
            try:
                try:
                    if item is None:
                        item = await asyncio.wait_for(self._tasks_queue.get(), queue_timeout)
                except asyncio.TimeoutError:
                    current_timer = default_timer()
                    if last_poll_time + self._polling_interval <= current_timer:
                        if self._in_quiescent_mode:
                            queue_timeout = 1.0
                            continue
                        if not devices:
                            devices = [d for d in self.dali_devices if d.is_initialized]
                        if devices:
                            await self._poll_device(devices.pop())
                            queue_timeout = 0.001
                        if not devices:
                            last_poll_time = current_timer
                            queue_timeout = 1.0
                    if self._pending_init and not self._in_quiescent_mode:
                        await self._try_init_pending_device(current_timer)
                    continue

                if self._in_quiescent_mode:
                    if item.task_type == ApplicationControllerTaskType.EXECUTE_CONTROL:
                        self._controls_to_execute.pop(item.data, None)
                    item.future.cancel()
                    item = None
                    continue

                if item.task_type == ApplicationControllerTaskType.EXECUTE_CONTROL:
                    controls = []
                    while (
                        item is not None and item.task_type == ApplicationControllerTaskType.EXECUTE_CONTROL
                    ):
                        payload = self._controls_to_execute.pop(item.data, None)
                        if payload is not None:
                            device, control_id = item.data
                            controls.append((item, payload, device, control_id))
                        else:
                            # Ensure futures are always resolved, even if there is no payload
                            if not item.future.done():
                                item.future.set_exception(
                                    RuntimeError("No payload available for EXECUTE_CONTROL task")
                                )
                        try:
                            item = self._tasks_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            item = None
                    results = await asyncio.gather(
                        *[
                            device.execute_control(self._dev, control_id, payload)
                            for _task, payload, device, control_id in controls
                        ],
                        return_exceptions=True,
                    )
                    for (processed_task, payload, device, control_id), result in zip(controls, results):
                        if not processed_task.future.done():
                            if isinstance(result, Exception):
                                processed_task.future.set_exception(result)
                            else:
                                processed_task.future.set_result(result)
                else:
                    try:
                        if item.task_type == ApplicationControllerTaskType.COMMISSIONING:
                            await self._commissioning_task()
                            devices = [d for d in self.dali_devices if d.is_initialized]
                        elif item.task_type == ApplicationControllerTaskType.LOAD_INFO:
                            device, force_reload = item.data
                            await device.load_info(self._dev, force_reload)
                        elif item.task_type == ApplicationControllerTaskType.APPLY_SETTING:
                            device, new_params = item.data
                            await device.apply_parameters(self._dev, new_params)
                        elif item.task_type == ApplicationControllerTaskType.APPLY_GROUP_SETTING:
                            group_index, new_params = item.data
                            await self._apply_group_parameters_task(group_index, new_params)
                        elif item.task_type == ApplicationControllerTaskType.APPLY_BUS_SETTING:
                            await self._apply_bus_parameters_task(item.data)
                        if not item.future.done():
                            item.future.set_result(None)
                    except Exception as e:
                        if not item.future.done():
                            item.future.set_exception(e)
                    finally:
                        item = None

            except Exception as e:  # pylint: disable=broad-exception-caught
                self.logger.error("Unexpected error in polling loop: %s", e, exc_info=True)
                item = None

    async def _poll_device(self, device: DaliDevice) -> None:
        try:
            responses = await device.poll_controls(self._dev)
        except Exception as e:
            self.logger.exception("Error polling device %s: %s", device.name, str(e))
            return
        tasks = []
        for response in responses:
            if response.error is not None:
                tasks.append(
                    self._device_publisher.set_control_error(device.mqtt_id, response.control_id, "r")
                )
                continue
            if response.title is not None:
                tasks.append(
                    self._device_publisher.set_control_title(
                        device.mqtt_id, response.control_id, response.title
                    )
                )
            if response.value is not None:
                tasks.append(
                    self._device_publisher.set_control_value(
                        device.mqtt_id, response.control_id, response.value
                    )
                )
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    self.logger.error(
                        "Error updating MQTT control for device %s: %s",
                        device.name,
                        result,
                    )

    def _run_websocket(self) -> None:
        self._websocket_task = asyncio.create_task(
            run_websocket(self._dev, "0.0.0.0", self.websocket_config.port, self.logger),
            name=f"websocket-{self.uid}",
        )

    async def _stop_websocket(self) -> None:
        if self._websocket_task is not None:
            self._websocket_task.cancel()
            try:
                await self._websocket_task
            except asyncio.CancelledError:
                # Task cancellation is expected when stopping the websocket; ignore this error.
                pass
            self._websocket_task = None

    async def _handle_start_quiescent_mode(self) -> None:
        self._in_quiescent_mode = True
        if self._quiescent_mode_timer:
            self._quiescent_mode_timer.cancel()
        self._quiescent_mode_timer = asyncio.get_event_loop().call_later(
            60 * 15,  # 15 minutes
            lambda: self._one_shot_tasks.add(
                self._handle_stop_quiescent_mode(), "Stop quiescent mode after timeout"
            ),
        )

    async def _handle_stop_quiescent_mode(self) -> None:
        self._in_quiescent_mode = False
        if self._quiescent_mode_timer:
            self._quiescent_mode_timer.cancel()
            self._quiescent_mode_timer = None

    def _handle_bus_traffic_frame(self, frame: Frame, source: str, frame_counter: Optional[int]) -> None:
        incoming_command = None
        if not frame.error and isinstance(frame, ForwardFrame):
            incoming_command = from_frame(
                frame, dev_inst_map=self._dev_inst_map, devicetype=self._last_bus_traffic_device_type
            )
            if source in ["bus", LUNATONE_IOT_EMULATOR_WBDALIDRIVER_SOURCE]:
                try:
                    if isinstance(incoming_command, StartQuiescentMode):
                        self._one_shot_tasks.add(self._handle_start_quiescent_mode(), "Start quiescent mode")
                        return
                    if isinstance(incoming_command, StopQuiescentMode):
                        self._one_shot_tasks.add(self._handle_stop_quiescent_mode(), "Stop quiescent mode")
                        return
                except Exception:  # pylint: disable=broad-exception-caught
                    pass  # Ignore errors in bus traffic handling

            if (
                isinstance(incoming_command, _Event)
                and incoming_command.instance_number is not None
                and incoming_command.short_address is not None
            ):
                device = self._dali2_devices_by_addr.get(incoming_command.short_address.address)
                if device is not None:
                    instance = device.instances.get(incoming_command.instance_number)
                    if instance is not None:
                        self._one_shot_tasks.add(
                            publish_dali2_event(
                                incoming_command, device.mqtt_id, self._mqtt_dispatcher.client
                            ),
                            "Publish DALI 2 event to MQTT",
                        )

        self._publish_bus_traffic(frame, source, frame_counter, incoming_command)

    def _publish_bus_traffic(
        self, frame: Frame, source: str, frame_counter: Optional[int], decoded_command: Optional[Command]
    ) -> None:
        if decoded_command is not None and isinstance(decoded_command, EnableDeviceType):
            self._last_bus_traffic_device_type = decoded_command.param
        else:
            self._last_bus_traffic_device_type = 0

        if self.bus_monitor_enabled:
            if frame.error:
                command_str = " Error"
            elif decoded_command is not None:
                command_str = f" {decoded_command}"
            else:
                command_str = ""

            frame_length = len(frame)
            if frame_length <= 16:
                frame_value = f"   {frame.as_integer:04x}"
            elif frame_length <= 24:
                frame_value = f" {frame.as_integer:06x}"
            else:
                frame_value = f"{frame.as_integer:07x}"

            frame_type = "FF" if isinstance(frame, ForwardFrame) else "BF"

            if source == "bus":
                msg = f"<<{frame_value} {frame_type}{frame_length}{command_str}"
                if frame_counter is not None:
                    msg = msg + f" (fc: {frame_counter})"
            else:
                msg = f">>{frame_value} {frame_type}{frame_length}{command_str} (src: {source})"

            self._one_shot_tasks.add(
                self._mqtt_dispatcher.client.publish(self._bus_monitor_topic, msg, qos=2, retain=False),
                "Publish DALI bus traffic to MQTT",
            )
