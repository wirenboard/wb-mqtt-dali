import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from timeit import default_timer
from typing import Any, Iterable, Optional, Union

import paho.mqtt.client as mqtt
from dali.address import (
    Address,
    DeviceBroadcast,
    DeviceShort,
    GearBroadcast,
    GearGroup,
    InstanceNumber,
)
from dali.command import Command, Response, from_frame
from dali.device.general import StartQuiescentMode, StopQuiescentMode, _Event
from dali.frame import ForwardFrame, Frame
from dali.gear.general import EnableDeviceType

from .asyncio_utils import OneShotTasks
from .bus_traffic import BusTrafficItem, BusTrafficSource
from .commissioning import Commissioning, CommissioningResult
from .common_dali_device import MqttControlBase
from .dali2_controls import publish_dali2_event
from .dali2_device import Dali2Device
from .dali_controls import make_controls
from .dali_device import DaliDevice
from .dali_type8_parameters import ColourType
from .dali_type8_rgbwaf import get_mqtt_controls as rgbwaf_mqtt_controls
from .dali_type8_tc import get_wanted_mqtt_controls as tc_mqtt_controls
from .device_init_scheduler import DeviceInitScheduler
from .device_publisher import (
    ControlInfo,
    DeviceChange,
    DeviceInfo,
    DevicePublisher,
    TranslatedTitle,
)
from .fake_lunatone_iot import run_websocket
from .gtin_db import DaliDatabase
from .mqtt_dispatcher import MQTTDispatcher
from .utils import merge_json_schemas
from .wbdali import WBDALIConfig, WBDALIDriver
from .wbdali_error_response import WbGatewayTransmissionError
from .wbdali_utils import (
    AsyncDeviceInstanceTypeMapper,
    send_commands_with_retry,
    send_with_retry,
)
from .wbmdali import WBDALIConfig as WBDALIDriverOldConfig
from .wbmdali import WBDALIDriver as WBDALIDriverOld


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
class ApplicationControllerConfig:  # pylint: disable=too-many-instance-attributes
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
    IDENTIFY_DEVICE = auto()


@dataclass
class PollingState:
    devices: list = field(default_factory=list)
    last_poll_time: float = 0.0
    poll_turn: bool = True


@dataclass
class ApplicationControllerTask:
    task_type: ApplicationControllerTaskType
    data: Any = field(default_factory=dict)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


@dataclass(frozen=True)
class AggregatedCapabilities:
    has_dt8_rgbwaf: bool = False
    has_dt8_tc: bool = False
    tc_min_mirek: int = 0
    tc_max_mirek: int = 0


def build_virtual_device_controls(
    capabilities: AggregatedCapabilities,
) -> dict[str, MqttControlBase]:
    controls: list[MqttControlBase] = list(make_controls())
    if capabilities.has_dt8_rgbwaf:
        controls.extend(rgbwaf_mqtt_controls(only_setup_controls=True))
    if capabilities.has_dt8_tc:
        controls.extend(tc_mqtt_controls(capabilities.tc_min_mirek, capabilities.tc_max_mirek))
    return {c.control_info.id: c for c in controls}


def aggregate_capabilities(devices: Iterable[DaliDevice]) -> AggregatedCapabilities:
    has_rgbwaf = False
    has_tc = False
    tc_min_values: list[int] = []
    tc_max_values: list[int] = []
    for device in devices:
        if not device.is_initialized:
            continue
        colour_type = device.dt8_colour_type
        if colour_type == ColourType.RGBWAF:
            has_rgbwaf = True
        elif colour_type == ColourType.COLOUR_TEMPERATURE:
            has_tc = True
            limits = device.dt8_tc_limits
            if limits is not None:
                tc_min_values.append(limits.tc_min_mirek)
                tc_max_values.append(limits.tc_max_mirek)
    return AggregatedCapabilities(
        has_dt8_rgbwaf=has_rgbwaf,
        has_dt8_tc=has_tc,
        tc_min_mirek=min(tc_min_values) if tc_min_values else 0,
        tc_max_mirek=max(tc_max_values) if tc_max_values else 0,
    )


class AggregatedVirtualDevice:
    def __init__(
        self,
        mqtt_id: str,
        name: Union[str, TranslatedTitle],
        capabilities: AggregatedCapabilities,
        address: Address,
    ) -> None:
        self.mqtt_id = mqtt_id
        self.name = name
        self.capabilities = capabilities
        self.logger = logging.getLogger()

        self._controls = build_virtual_device_controls(capabilities)
        self._address = address

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
            await send_commands_with_retry(
                driver,
                control.get_setup_commands(self._address, value),
                self.logger,
            )

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger


ControllableDevice = Union[DaliDevice, Dali2Device, AggregatedVirtualDevice]


async def try_initialize_device(  # pylint: disable=too-many-arguments, R0917
    device: Union[DaliDevice, Dali2Device],
    driver,
    publisher: DevicePublisher,
    scheduler: DeviceInitScheduler,
    control_handler,
    logger: logging.Logger,
    current_time: float,
) -> bool:
    """Try to initialize device. Publish on success or error. Returns True on success."""
    mqtt_id = device.mqtt_id
    is_retry = scheduler.get_retry_count(mqtt_id) > 0
    try:
        await device.initialize(driver)
        if publisher.has_device(mqtt_id):
            await publisher.remove_device(mqtt_id)
        await publish_device(device, publisher, control_handler)
        scheduler.record_success(mqtt_id)
        if is_retry:
            logger.info("Device %s initialized successfully after retry", device.name)
        return True
    except Exception as e:  # pylint: disable=broad-exception-caught
        delay = scheduler.record_failure(mqtt_id, current_time)
        if not is_retry:
            logger.warning("Failed to initialize device %s, retrying in %.0fs: %s", device.name, delay, e)
        if not publisher.has_device(mqtt_id):
            await publish_device(device, publisher, control_handler, error=True)
        return False


async def publish_device(
    device: Union[DaliDevice, Dali2Device],
    publisher: DevicePublisher,
    control_handler,
    error: bool = False,
) -> None:
    if error:
        common_controls = device.get_common_mqtt_controls()
        controls = [c.control_info for c in common_controls]
    else:
        controls = device.get_mqtt_controls()
    device_info = DeviceInfo(device.mqtt_id, device.name, controls)
    await publisher.add_device(device_info)
    await publisher.register_control_handler(device.mqtt_id, "+", control_handler)
    if error:
        for control in common_controls:
            if control.is_readable():
                await publisher.set_control_error(device.mqtt_id, control.control_info.id, "r")


class ApplicationController:  # pylint: disable=too-many-instance-attributes
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
        self._broadcast_device = AggregatedVirtualDevice(
            mqtt_id=f"{self.uid}_broadcast",
            name=TranslatedTitle(f"{self.bus_name} Broadcast", f"{self.bus_name} широковещательный"),
            capabilities=AggregatedCapabilities(),
            address=GearBroadcast(),
        )
        self._group_devices_by_number: dict[int, AggregatedVirtualDevice] = {}

        self._gtin_db = gtin_db

        self._tasks_queue: asyncio.Queue[ApplicationControllerTask] = asyncio.Queue()

        self._in_quiescent_mode = False
        self._init_scheduler = DeviceInitScheduler()

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
        await self._publish_virtual_device(self._broadcast_device)

        current_time = default_timer()
        for device in self.dali_devices + self.dali2_devices:
            self._devices_by_mqtt_id[device.mqtt_id] = device
            device.set_logger(self.logger)
            self._init_scheduler.schedule(device.mqtt_id, current_time)

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
                raise RuntimeError(f"ApplicationController {self.uid} must be initialized to stop")
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
        self._init_scheduler.clear()

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
            await publish_device(device, self._device_publisher, self._handle_on_topic)
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
            await self._refresh_broadcast_device()

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

    async def identify_device(self, device: Union[DaliDevice, Dali2Device]) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.READY:
                raise RuntimeError("ApplicationController must be initialized")
            task = ApplicationControllerTask(ApplicationControllerTaskType.IDENTIFY_DEVICE, device)
            self._tasks_queue.put_nowait(task)
        await task.future

    async def setup_websocket(self, config: WebSocketConfig) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.READY:
                self.websocket_config = config
                self.logger.debug(
                    "Trying to setup Lunatone IoT Gateway emulator in uninitialized state, "
                    "just saving config",
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
        await send_with_retry(self._dev, StartQuiescentMode(DeviceBroadcast()), self.logger)
        try:
            self.logger.debug("Commissioning for DALI 1.0 devices")
            obj = Commissioning(self._dev, [d.address for d in self.dali_devices], dali2=False)
            res_dali = await obj.smart_extend()
            self.logger.debug("Commissioning for DALI 2.0 devices")
            obj = Commissioning(self._dev, [d.address for d in self.dali2_devices], dali2=True)
            res_dali2 = await obj.smart_extend()
        finally:
            await send_with_retry(self._dev, StopQuiescentMode(DeviceBroadcast()), self.logger)

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
            self._init_scheduler.remove(removed_id)

        for device in created_devices:
            device.set_logger(self.logger)
            await self._try_init_new_device(device)

        self.dali_devices = unchanged_devices + created_devices
        self.dali_devices.sort(key=lambda d: d.address.short)
        self._devices_by_mqtt_id.update({d.mqtt_id: d for d in created_devices})
        await self._refresh_group_virtual_devices()
        await self._refresh_broadcast_device()

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
            self._init_scheduler.remove(removed_id)

        for device in created_devices:
            device.set_logger(self.logger)
            await self._try_init_new_device(device)

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

    def _get_group_capabilities(self, group_number: int) -> AggregatedCapabilities:
        return aggregate_capabilities(d for d in self.dali_devices if group_number in d.groups)

    def _get_bus_capabilities(self) -> AggregatedCapabilities:
        return aggregate_capabilities(self.dali_devices)

    def _get_active_group_numbers(self) -> list[int]:
        active_groups = set()
        for device in self.dali_devices:
            active_groups.update(device.groups)
        return sorted(active_groups)

    def _make_group_virtual_device(self, group_number: int) -> AggregatedVirtualDevice:
        return AggregatedVirtualDevice(
            mqtt_id=f"{self.uid}_group_{group_number:02d}",
            name=TranslatedTitle(
                f"{self.bus_name} Group {group_number}",
                f"{self.bus_name} группа {group_number}",
            ),
            capabilities=self._get_group_capabilities(group_number),
            address=GearGroup(group_number),
        )

    async def _publish_virtual_device(self, device: AggregatedVirtualDevice) -> None:
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
        self._devices_by_mqtt_id[device.mqtt_id] = device
        device.set_logger(self.logger)

    async def _try_init_new_device(self, device: Union[DaliDevice, Dali2Device]) -> None:
        current_time = default_timer()
        self._devices_by_mqtt_id[device.mqtt_id] = device
        self._init_scheduler.schedule(device.mqtt_id, current_time)
        await self._do_init_device(device.mqtt_id, current_time)

    async def _do_init_device(self, mqtt_id: str, current_time: float) -> None:
        device = self._devices_by_mqtt_id.get(mqtt_id)
        if device is None or not isinstance(device, (DaliDevice, Dali2Device)):
            self._init_scheduler.remove(mqtt_id)
            return
        if device.is_initialized:
            self._init_scheduler.remove(mqtt_id)
            return
        success = await try_initialize_device(
            device,
            self._dev,
            self._device_publisher,
            self._init_scheduler,
            self._handle_on_topic,
            self.logger,
            current_time,
        )
        if success:
            if isinstance(device, DaliDevice):
                await self._refresh_group_virtual_devices()
                await self._refresh_broadcast_device()
            elif isinstance(device, Dali2Device):
                self._update_dali2_device_instance_map(device)

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
            await self._publish_virtual_device(device)
            self._group_devices_by_number[group_number] = device

        for group_number in sorted(active_groups & existing_groups):
            new_caps = self._get_group_capabilities(group_number)
            old_device = self._group_devices_by_number[group_number]
            if old_device.capabilities != new_caps:
                self.logger.debug(
                    "Rebuilding group virtual device: group=%d capabilities changed",
                    group_number,
                )
                await self._device_publisher.remove_device(old_device.mqtt_id)
                self._devices_by_mqtt_id.pop(old_device.mqtt_id, None)
                new_device = self._make_group_virtual_device(group_number)
                await self._publish_virtual_device(new_device)
                self._group_devices_by_number[group_number] = new_device

    async def _refresh_broadcast_device(self) -> None:
        new_caps = self._get_bus_capabilities()
        if self._broadcast_device.capabilities == new_caps:
            return

        self.logger.debug("Rebuilding broadcast virtual device: capabilities changed")
        old_mqtt_id = self._broadcast_device.mqtt_id

        await self._device_publisher.remove_device(old_mqtt_id)
        self._devices_by_mqtt_id.pop(old_mqtt_id, None)

        self._broadcast_device = AggregatedVirtualDevice(
            mqtt_id=old_mqtt_id,
            name=self._broadcast_device.name,
            capabilities=new_caps,
            address=GearBroadcast(),
        )

        await self._publish_virtual_device(self._broadcast_device)

    async def _poll_step(self, state: "PollingState", current_time: float) -> float:
        """Execute one poll/init step. Returns queue_timeout for next iteration."""
        # First-attempt batch — fast startup, no interleaving needed
        first_attempt_ids = self._init_scheduler.get_first_attempt_ready(current_time)
        if first_attempt_ids:
            for mqtt_id in first_attempt_ids:
                await self._do_init_device(mqtt_id, current_time)
            return 0.001

        # Alternate: poll one device, then retry-init one device
        if state.poll_turn and state.last_poll_time + self._polling_interval <= current_time:
            if not state.devices:
                state.devices = [d for d in self.dali_devices if d.is_initialized]
            if state.devices:
                await self._poll_device(state.devices.pop())
                if not state.devices:
                    state.last_poll_time = current_time
                state.poll_turn = False
                return 0.001
            state.last_poll_time = current_time

        retry_id = self._init_scheduler.get_one_retry_ready(current_time)
        if retry_id:
            await self._do_init_device(retry_id, current_time)
            state.poll_turn = True
            return 0.001

        state.poll_turn = True
        return 1.0

    async def _polling_loop(self) -> None:  # pylint: disable=too-many-branches, too-many-statements
        state = PollingState(last_poll_time=default_timer() - self._polling_interval)
        queue_timeout = 0.001
        item = None
        while True:
            try:
                try:
                    if item is None:
                        item = await asyncio.wait_for(self._tasks_queue.get(), queue_timeout)
                except asyncio.TimeoutError:
                    if self._in_quiescent_mode:
                        queue_timeout = 1.0
                        continue
                    queue_timeout = await self._poll_step(state, default_timer())
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
                            state.devices.clear()
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
                        elif item.task_type == ApplicationControllerTaskType.IDENTIFY_DEVICE:
                            await item.data.identify(self._dev)
                        if not item.future.done():
                            item.future.set_result(None)
                    except Exception as e:  # pylint: disable=broad-exception-caught
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
        except Exception as e:  # pylint: disable=broad-exception-caught
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

    def _handle_bus_traffic_frame(self, item: BusTrafficItem) -> None:
        incoming_command = None
        if not item.request.error and isinstance(item.request, ForwardFrame):
            incoming_command = from_frame(
                item.request, dev_inst_map=self._dev_inst_map, devicetype=self._last_bus_traffic_device_type
            )
            if item.request_source in [BusTrafficSource.BUS, BusTrafficSource.LUNATONE]:
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

        self._publish_bus_traffic(item, incoming_command)

    def _publish_bus_traffic(
        self,
        bus_traffic_item: BusTrafficItem,
        decoded_request_command: Optional[Command],
    ) -> None:
        if decoded_request_command is not None and isinstance(decoded_request_command, EnableDeviceType):
            self._last_bus_traffic_device_type = decoded_request_command.param
        else:
            self._last_bus_traffic_device_type = 0

        if self.bus_monitor_enabled:
            request_msg = format_command(bus_traffic_item.request, decoded_request_command)

            if bus_traffic_item.request_source == BusTrafficSource.BUS:
                request_msg = f"<<{request_msg} (fc: {bus_traffic_item.frame_counter})"
            else:
                if bus_traffic_item.request_source == BusTrafficSource.LUNATONE:
                    request_msg = f">>{request_msg} (from lunatone)"
                else:
                    request_msg = f">>{request_msg}"

            self._one_shot_tasks.add(
                self._mqtt_dispatcher.client.publish(
                    self._bus_monitor_topic, request_msg, qos=2, retain=False
                ),
                "Publish DALI bus traffic to MQTT",
            )

            if bus_traffic_item.response is not None and (
                isinstance(bus_traffic_item.response, WbGatewayTransmissionError)
                or bus_traffic_item.response.raw_value is not None
            ):
                response_msg = f"<<{format_response(bus_traffic_item.response)}"

                self._one_shot_tasks.add(
                    self._mqtt_dispatcher.client.publish(
                        self._bus_monitor_topic, response_msg, qos=2, retain=False
                    ),
                    "Publish DALI bus traffic to MQTT",
                )


def format_frame(frame: Frame) -> str:
    frame_length = len(frame)
    if frame_length <= 16:
        frame_value = f"   {frame.as_integer:04x}"
    elif frame_length <= 24:
        frame_value = f" {frame.as_integer:06x}"
    else:
        frame_value = f"{frame.as_integer:07x}"

    frame_type = "FF" if isinstance(frame, ForwardFrame) else "BF"
    return f"{frame_value} {frame_type}{frame_length:<2}"


def format_command(frame: Frame, decoded_command: Optional[Command]) -> str:
    if frame.error:
        return f"{format_frame(frame)} Error"
    if decoded_command is not None:
        return f"{format_frame(frame)} {decoded_command}"
    return format_frame(frame)


def format_response(response: Response) -> str:
    if isinstance(response, WbGatewayTransmissionError):
        return str(response)
    if (
        type(response) is Response  # pylint: disable=C0123
        and response.raw_value is not None
        and response.raw_value.error is not True
    ):
        return f"{format_frame(response.raw_value)} {response.raw_value.as_integer}"
    return f"{format_frame(response.raw_value)} {response}"
