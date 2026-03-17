import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from timeit import default_timer
from typing import Any, Optional, Union

import paho.mqtt.client as mqtt
from dali.address import DeviceBroadcast
from dali.command import from_frame
from dali.device.general import StartQuiescentMode, StopQuiescentMode, _Event
from dali.frame import ForwardFrame, Frame

from .commissioning import Commissioning, CommissioningResult
from .dali2_controls import publish_dali2_event
from .dali2_device import Dali2Device
from .dali_device import DaliDevice
from .device_publisher import DeviceChange, DeviceInfo, DevicePublisher
from .fake_lunatone_iot import LUNATONE_IOT_EMULATOR_WBDALIDRIVER_SOURCE, run_websocket
from .gtin_db import DaliDatabase
from .mqtt_dispatcher import MQTTDispatcher
from .wbdali import WBDALIConfig, WBDALIDriver
from .wbdali_utils import AsyncDeviceInstanceTypeMapper
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


class OneShotTasks:
    def __init__(self):
        self._tasks = []

    def add(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(self._remove_task)
        return task

    async def stop(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def _remove_task(self, task: asyncio.Task) -> None:
        try:
            self._tasks.remove(task)
        except ValueError:
            # Task might have been removed already (e.g., during stop()).
            pass


class ApplicationControllerTaskType(Enum):
    APPLY_SETTING = auto()
    COMMISSIONING = auto()
    LOAD_INFO = auto()
    EXECUTE_CONTROL = auto()


@dataclass
class ApplicationControllerTask:
    task_type: ApplicationControllerTaskType
    data: Any = field(default_factory=dict)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


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

        self._one_shot_tasks = OneShotTasks()

        self._dali2_devices_by_addr: dict[int, Dali2Device] = {d.address.short: d for d in self.dali2_devices}
        self._devices_by_mqtt_id: dict[str, Union[DaliDevice, Dali2Device]] = {}

        self._gtin_db = gtin_db

        self._tasks_queue: asyncio.Queue[ApplicationControllerTask] = asyncio.Queue()

        self._in_quiescent_mode = False

        self._controls_to_execute: dict[tuple[Union[DaliDevice, Dali2Device], str], str] = {}

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
            if self.dali2_devices:
                await self._dev_inst_map.async_autodiscover(
                    self._dev, [d.address.short for d in self.dali2_devices]
                )
        except Exception as e:
            async with self._state_lock:
                self._state = ApplicationControllerState.UNINITIALIZED
            raise RuntimeError("Failed to initialize WBDALIDriver") from e

        self._update_dali2_devices_instances({d.address.short: d for d in self.dali2_devices})

        await self._device_publisher.initialize()
        for device in self.dali_devices + self.dali2_devices:
            try:
                controls = await device.get_mqtt_controls(self._dev)
            except Exception as e:
                self.logger.error("Failed to get controls for device %s: %s", device.name, e)
                controls = []
            device_info = DeviceInfo(device.mqtt_id, device.name, controls)
            await self._device_publisher.add_device(device_info)
            await self._device_publisher.register_control_handler(
                device.mqtt_id,
                "+",
                self._handle_on_topic,
            )
            self._devices_by_mqtt_id[device.mqtt_id] = device
            device.setLogger(self.logger)

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
            device_info = DeviceInfo(
                new_mqtt_id,
                device.name,
                await device.get_mqtt_controls(self._dev),
            )
            await self._device_publisher.add_device(device_info)
            await self._device_publisher.register_control_handler(
                device.mqtt_id,
                "+",
                self._handle_on_topic,
            )
            await self._device_publisher.remove_device(old_mqtt_id)
            self._devices_by_mqtt_id.pop(old_mqtt_id, None)
            self._devices_by_mqtt_id[new_mqtt_id] = device
            device.setLogger(self.logger)
        else:
            await self._device_publisher.set_device_title(old_mqtt_id, device.name)

        if isinstance(device, Dali2Device) and old_short_address != device.address.short:
            self._dev_inst_map.update_mapping(old_short_address, device.address.short)
            self._dali2_devices_by_addr.pop(old_short_address, None)
            self._dali2_devices_by_addr[device.address.short] = device

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
        for device in created_devices:
            changes.added.append(
                DeviceInfo(device.mqtt_id, device.name, await device.get_mqtt_controls(self._dev))
            )
            device.setLogger(self.logger)

        await self._device_publisher.rebuild(changes)
        for device in created_devices:
            await self._device_publisher.register_control_handler(
                device.mqtt_id,
                "+",
                self._handle_on_topic,
            )

        self.dali_devices = unchanged_devices + created_devices
        self.dali_devices.sort(key=lambda d: d.address.short)
        for removed_id in removed_ids:
            self._devices_by_mqtt_id.pop(removed_id, None)
        self._devices_by_mqtt_id.update({d.mqtt_id: d for d in created_devices})

    async def _update_dali2_devices(self, commissioning_result: CommissioningResult) -> None:
        unchanged_devices = [d for d in self.dali2_devices if d.address in commissioning_result.unchanged]
        changed_devices = [Dali2Device(d.new, self.uid, self._gtin_db) for d in commissioning_result.changed]
        new_devices = [Dali2Device(addr, self.uid, self._gtin_db) for addr in commissioning_result.new]

        created_devices = changed_devices + new_devices
        new_dali2_devices = unchanged_devices + created_devices
        if new_dali2_devices:
            await self._dev_inst_map.async_autodiscover(
                self._dev, [d.address.short for d in new_dali2_devices]
            )
            self._update_dali2_devices_instances({d.address.short: d for d in created_devices})

        removed_short_addresses: set[int] = {d.old_short for d in commissioning_result.changed} | {
            d.short for d in commissioning_result.missing
        }
        removed_ids = [d.mqtt_id for d in self.dali2_devices if d.address.short in removed_short_addresses]
        changes = DeviceChange(removed=removed_ids)
        for device in created_devices:
            changes.added.append(
                DeviceInfo(device.mqtt_id, device.name, await device.get_mqtt_controls(self._dev))
            )
            device.setLogger(self.logger)

        await self._device_publisher.rebuild(changes)
        for device in created_devices:
            await self._device_publisher.register_control_handler(
                device.mqtt_id,
                "+",
                self._handle_on_topic,
            )

        self.dali2_devices = new_dali2_devices
        self.dali2_devices.sort(key=lambda d: d.address.short)
        for removed_id in removed_ids:
            self._devices_by_mqtt_id.pop(removed_id, None)
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
            task = ApplicationControllerTask(
                ApplicationControllerTaskType.EXECUTE_CONTROL, (device, control_id)
            )
            self._tasks_queue.put_nowait(task)
            await task.future
            await self._device_publisher.set_control_error(device_id, control_id, "")
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error("Error executing control %s for device %s: %s", control_id, device_id, e)
            await self._device_publisher.set_control_error(device_id, control_id, "w")

    def _update_dali2_devices_instances(self, dali2_devices_by_addr: dict[int, Dali2Device]) -> None:
        for (addr, inst_num), inst_type in self._dev_inst_map.mapping.items():
            device = dali2_devices_by_addr.get(addr)
            if device is not None:
                self.logger.debug(
                    "Adding instance %d of type %d to DALI 2 device %s", inst_num, inst_type, device.name
                )
                device.add_instance(inst_num, inst_type)

    async def _polling_loop(self) -> None:
        devices = []
        next_poll_time = default_timer()
        queue_timeout = 0.001
        item = None
        while True:
            try:
                try:
                    if item is None:
                        item = await asyncio.wait_for(self._tasks_queue.get(), queue_timeout)
                except asyncio.TimeoutError:
                    current_timer = default_timer()
                    if next_poll_time <= current_timer:
                        if self._in_quiescent_mode:
                            queue_timeout = 1.0
                            continue
                        if not devices:
                            devices = list(self.dali_devices)
                        if devices:
                            await self._poll_device(devices.pop())
                            queue_timeout = 0.001
                        if not devices:
                            next_poll_time = current_timer + self._polling_interval
                            queue_timeout = 1.0
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
                            for _item, payload, device, control_id in controls
                        ],
                        return_exceptions=True,
                    )
                    for (item, payload, device, control_id), result in zip(controls, results):
                        if not item.future.done():
                            if isinstance(result, Exception):
                                item.future.set_exception(result)
                            else:
                                item.future.set_result(result)
                else:
                    try:
                        if item.task_type == ApplicationControllerTaskType.COMMISSIONING:
                            await self._commissioning_task()
                            devices = list(self.dali_devices)
                        elif item.task_type == ApplicationControllerTaskType.LOAD_INFO:
                            device, force_reload = item.data
                            await device.load_info(self._dev, force_reload)
                        elif item.task_type == ApplicationControllerTaskType.APPLY_SETTING:
                            device, new_params = item.data
                            await device.apply_parameters(self._dev, new_params)
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
            lambda: self._one_shot_tasks.add(self._handle_stop_quiescent_mode()),
        )

    async def _handle_stop_quiescent_mode(self) -> None:
        self._in_quiescent_mode = False
        if self._quiescent_mode_timer:
            self._quiescent_mode_timer.cancel()
            self._quiescent_mode_timer = None

    def _handle_bus_traffic_frame(self, frame: Frame, source: str, _frame_counter: Optional[int]) -> None:
        incoming_command = None
        if not frame.error and isinstance(frame, ForwardFrame):
            incoming_command = from_frame(frame, dev_inst_map=self._dev_inst_map)
            if source in ["bus", LUNATONE_IOT_EMULATOR_WBDALIDRIVER_SOURCE]:
                try:
                    if isinstance(incoming_command, StartQuiescentMode):
                        self._one_shot_tasks.add(self._handle_start_quiescent_mode())
                        return
                    if isinstance(incoming_command, StopQuiescentMode):
                        self._one_shot_tasks.add(self._handle_stop_quiescent_mode())
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
                            )
                        )

        if self.bus_monitor_enabled:
            self._one_shot_tasks.add(
                self._mqtt_dispatcher.client.publish(
                    self._bus_monitor_topic,
                    f"{source}: {frame.as_integer:x} {incoming_command}",
                    qos=2,
                    retain=False,
                )
            )
