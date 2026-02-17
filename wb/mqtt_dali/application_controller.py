import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from timeit import default_timer
from typing import Optional, Sequence, Union

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
from .wbdali import AsyncDeviceInstanceTypeMapper, WBDALIConfig, WBDALIDriver


class ApplicationControllerState(Enum):
    UNINITIALIZED = auto()
    INITIALIZING = auto()
    READY = auto()
    STOPPING = auto()
    COMMISSIONING = auto()
    IN_QUIESCENT_MODE = auto()
    GENERIC_TASK = auto()


@dataclass
class WebSocketConfig:
    enabled: bool = False
    port: int = 8080


@dataclass
class ApplicationControllerConfig:
    gateway_mqtt_device_id: str
    bus_index: int
    dali_devices: list[DaliDevice]
    dali2_devices: list[Dali2Device]
    polling_interval: float
    websocket_config: WebSocketConfig = field(default_factory=WebSocketConfig)


class ApplicationController:
    def __init__(
        self,
        config: ApplicationControllerConfig,
        mqtt_dispatcher: MQTTDispatcher,
        gtin_db: DaliDatabase,
    ) -> None:
        self.uid = f"{config.gateway_mqtt_device_id}_bus_{config.bus_index}"
        self.bus_name = f"Bus {config.bus_index}"
        self.dali_devices = config.dali_devices
        self.dali2_devices = config.dali2_devices
        self.websocket_config = config.websocket_config
        self.logger = logging.getLogger(self.uid)

        self._state = ApplicationControllerState.UNINITIALIZED
        self._state_lock = asyncio.Lock()
        self._ready_condition = asyncio.Condition(self._state_lock)

        self._quiescent_mode_timer: Optional[asyncio.TimerHandle] = None
        self._active_task: Optional[asyncio.Task] = None
        self._active_task_description: str = ""

        self._mqtt_dispatcher = mqtt_dispatcher
        self._device_publisher = DevicePublisher(mqtt_dispatcher, self.logger)

        self._dev_inst_map = AsyncDeviceInstanceTypeMapper()
        cfg = WBDALIConfig(
            device_name=config.gateway_mqtt_device_id,
            channel=config.bus_index + 1,
        )

        self._polling_interval = config.polling_interval
        self._polling_task: Optional[asyncio.Task] = None
        self._reschedule_polling_task = True
        self._dev = WBDALIDriver(cfg, mqtt_dispatcher, self.logger, self._dev_inst_map)

        self._websocket_task: Optional[asyncio.Task] = None
        self._websocket_lock = asyncio.Lock()
        self._bus_traffic_cleanup = self._dev.bus_traffic.register(self._handle_bus_traffic_frame)

        self._dali2_devices_by_addr: dict[int, Dali2Device] = {d.address.short: d for d in self.dali2_devices}
        self._devices_by_mqtt_id: dict[str, Union[DaliDevice, Dali2Device]] = {}

        self._gtin_db = gtin_db

    @property
    def polling_interval(self) -> float:
        return self._polling_interval

    def set_polling_interval(self, value: float) -> None:
        self._polling_interval = value

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
            device_info = DeviceInfo(device.mqtt_id, device.name, await device.get_mqtt_controls(self._dev))
            await self._device_publisher.add_device(device_info)

        self._polling_task = asyncio.create_task(self._polling_loop())

        async with self._websocket_lock:
            if self.websocket_config.enabled:
                self._run_websocket()
        await self._notify_ready()

    async def stop(self) -> None:
        async with self._state_lock:
            if self._state in (
                ApplicationControllerState.UNINITIALIZED,
                ApplicationControllerState.INITIALIZING,
                ApplicationControllerState.STOPPING,
            ):
                raise RuntimeError("ApplicationController must be initialized to stop")
            self._state = ApplicationControllerState.STOPPING

        self._bus_traffic_cleanup()

        if self._quiescent_mode_timer:
            self._quiescent_mode_timer.cancel()

        if self._polling_task:
            self._reschedule_polling_task = False
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

        async with self._websocket_lock:
            await self._stop_websocket()

        await self._device_publisher.cleanup()

        if self._active_task:
            if self._active_task.cancel():
                self.logger.debug("Cancelling active task: %s", self._active_task_description)
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
            self._active_task = None

        await self._dev.deinitialize()
        async with self._state_lock:
            self._state = ApplicationControllerState.UNINITIALIZED

    async def rescan_bus(self) -> None:
        await self._run_task(
            ApplicationControllerState.COMMISSIONING, self._commissioning_task(), "commissioning", 5.0
        )

    def is_commissioning(self) -> bool:
        return self._state == ApplicationControllerState.COMMISSIONING

    async def load_device_info(
        self, device: Union[DaliDevice, Dali2Device], force_reload: bool = False
    ) -> None:
        await self._run_task(
            ApplicationControllerState.GENERIC_TASK,
            device.load_info(self._dev, force_reload),
            f"loading device {device.name} info",
            5.0,
        )

    async def apply_parameters(self, device: Union[DaliDevice, Dali2Device], new_params: dict) -> None:
        old_mqtt_id = device.mqtt_id
        old_short_address = device.address.short
        await self._run_task(
            ApplicationControllerState.GENERIC_TASK,
            device.apply_parameters(self._dev, new_params),
            f"applying parameters to device {device.name}",
            5.0,
        )

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
        else:
            await self._device_publisher.set_device_title(old_mqtt_id, device.name)

        if isinstance(device, Dali2Device) and old_short_address != device.address.short:
            self._dev_inst_map.update_mapping(old_short_address, device.address.short)
            self._dali2_devices_by_addr.pop(old_short_address, None)
            self._dali2_devices_by_addr[device.address.short] = device

    async def setup_websocket(self, config: WebSocketConfig) -> None:
        async with self._state_lock:
            if self._state in (
                ApplicationControllerState.UNINITIALIZED,
                ApplicationControllerState.INITIALIZING,
                ApplicationControllerState.STOPPING,
            ):
                self.websocket_config = config
                self.logger.debug(
                    "Trying to setup Lunatone IoT Gateway emulator in uninitialized state %s, just saving config",
                    self._state,
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

    async def _run_task(
        self, new_state: ApplicationControllerState, task, task_description: str, timeout: float = 1.0
    ) -> None:
        try:
            async with self._ready_condition:
                await asyncio.wait_for(
                    self._ready_condition.wait_for(lambda: self._state == ApplicationControllerState.READY),
                    timeout=timeout,
                )
                self._state = new_state
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"Bus is occupied by {self._active_task_description}") from e
        self._active_task = asyncio.create_task(task)
        self._active_task_description = task_description
        try:
            await self._active_task
        finally:
            await self._notify_ready()

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

        removed_short_addresses: set[int] = {d.old_short for d in commissioning_result.changed} | {
            d.short for d in commissioning_result.missing
        }
        removed_ids = [d.mqtt_id for d in self.dali_devices if d.address.short in removed_short_addresses]

        created_devices = changed_devices + new_devices

        self.dali_devices = unchanged_devices + created_devices
        self.dali_devices.sort(key=lambda d: d.address.short)

        for removed_id in removed_ids:
            self._devices_by_mqtt_id.pop(removed_id, None)
        self._devices_by_mqtt_id.update({d.mqtt_id: d for d in created_devices})

        changes = DeviceChange(removed=removed_ids)
        for device in created_devices:
            changes.added.append(
                DeviceInfo(device.mqtt_id, device.name, await device.get_mqtt_controls(self._dev))
            )

        await self._device_publisher.rebuild(changes)
        for device in created_devices:
            await self._device_publisher.register_control_handler(
                device.mqtt_id,
                "+",
                self._handle_on_topic,
            )

    async def _update_dali2_devices(self, commissioning_result: CommissioningResult) -> None:
        unchanged_devices = [d for d in self.dali2_devices if d.address in commissioning_result.unchanged]
        changed_devices = [Dali2Device(d.new, self.uid, self._gtin_db) for d in commissioning_result.changed]
        new_devices = [Dali2Device(addr, self.uid, self._gtin_db) for addr in commissioning_result.new]

        removed_short_addresses: set[int] = {d.old_short for d in commissioning_result.changed} | {
            d.short for d in commissioning_result.missing
        }
        removed_ids = [d.mqtt_id for d in self.dali2_devices if d.address.short in removed_short_addresses]

        created_devices = changed_devices + new_devices

        self.dali2_devices = unchanged_devices + created_devices
        self.dali2_devices.sort(key=lambda d: d.address.short)
        self._dali2_devices_by_addr = {d.address.short: d for d in self.dali2_devices}

        for removed_id in removed_ids:
            self._devices_by_mqtt_id.pop(removed_id, None)
        self._devices_by_mqtt_id.update({d.mqtt_id: d for d in created_devices})

        if self.dali2_devices:
            await self._dev_inst_map.async_autodiscover(
                self._dev, [d.address.short for d in self.dali2_devices]
            )
            self._update_dali2_devices_instances({d.address.short: d for d in created_devices})

        changes = DeviceChange(removed=removed_ids)
        for device in created_devices:
            changes.added.append(
                DeviceInfo(device.mqtt_id, device.name, await device.get_mqtt_controls(self._dev))
            )

        await self._device_publisher.rebuild(changes)
        for device in created_devices:
            await self._device_publisher.register_control_handler(
                device.mqtt_id,
                "+",
                self._handle_on_topic,
            )

    async def _handle_on_topic(self, message: mqtt.MQTTMessage) -> None:
        device_id = message.topic.split("/")[2]
        control_id = message.topic.split("/")[4]
        payload = message.payload.decode("utf-8") if getattr(message, "payload", None) else ""

        device = self._devices_by_mqtt_id.get(device_id)
        if device is None:
            return
        if self._active_task:
            if self._active_task_description == "polling":
                self._active_task.cancel("command sent")
        try:
            await self._run_task(
                ApplicationControllerState.GENERIC_TASK,
                device.execute_control(self._dev, control_id, payload),
                "sending command",
                3.0,
            )
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
        try:
            await asyncio.sleep(self._polling_interval)

            devices = tuple(self.dali_devices)
            if devices:
                try:
                    await self._run_task(
                        ApplicationControllerState.GENERIC_TASK,
                        self._poll_devices(devices),
                        "polling",
                    )
                except RuntimeError as e:
                    self.logger.debug("Skipping polling cycle: %s", e)

        except asyncio.CancelledError:
            self.logger.info("Polling loop cancelled")
            if not self._reschedule_polling_task:
                raise
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error("Unexpected error in polling loop: %s", e, exc_info=True)
            await asyncio.sleep(1)
        finally:
            if self._reschedule_polling_task and self._state not in (
                ApplicationControllerState.STOPPING,
                ApplicationControllerState.UNINITIALIZED,
            ):
                self._polling_task = asyncio.create_task(self._polling_loop())

    async def _poll_devices(self, devices: Sequence[DaliDevice]) -> None:
        queries = [device.poll_controls(self._dev) for device in devices]
        responses = await asyncio.gather(*queries, return_exceptions=True)
        tasks = []
        for device_responses, device in zip(responses, devices):
            if isinstance(device_responses, BaseException):
                self.logger.warning("Error polling device %s", device.name)
                continue
            for response in device_responses:
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
            await asyncio.gather(*tasks)

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
        async with self._state_lock:
            if self._state not in [
                ApplicationControllerState.READY,
                ApplicationControllerState.IN_QUIESCENT_MODE,
                ApplicationControllerState.GENERIC_TASK,
                ApplicationControllerState.COMMISSIONING,
            ]:
                return
            self._state = ApplicationControllerState.IN_QUIESCENT_MODE
        if self._active_task:
            self._active_task.cancel()
        if self._quiescent_mode_timer:
            self._quiescent_mode_timer.cancel()
        self._quiescent_mode_timer = asyncio.get_event_loop().call_later(
            60 * 15,  # 15 minutes
            lambda: asyncio.create_task(self._handle_stop_quiescent_mode()),
        )

    async def _handle_stop_quiescent_mode(self) -> None:
        async with self._state_lock:
            if self._state == ApplicationControllerState.IN_QUIESCENT_MODE:
                self._state = ApplicationControllerState.READY
                if self._quiescent_mode_timer:
                    self._quiescent_mode_timer.cancel()
                    self._quiescent_mode_timer = None
                self._ready_condition.notify()

    async def _notify_ready(self) -> None:
        async with self._state_lock:
            if self._state not in [
                ApplicationControllerState.STOPPING,
                ApplicationControllerState.IN_QUIESCENT_MODE,
            ]:
                self._state = ApplicationControllerState.READY
                self._active_task_description = ""
                self._ready_condition.notify()

    def _handle_bus_traffic_frame(self, frame: Frame, source: str) -> None:
        if isinstance(frame, ForwardFrame):
            incoming_command = from_frame(frame, dev_inst_map=self._dev_inst_map)
            if source in ["bus", LUNATONE_IOT_EMULATOR_WBDALIDRIVER_SOURCE]:
                try:
                    if isinstance(incoming_command, StartQuiescentMode):
                        asyncio.create_task(self._handle_start_quiescent_mode())
                        return
                    if isinstance(incoming_command, StopQuiescentMode):
                        asyncio.create_task(self._handle_stop_quiescent_mode())
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
                        asyncio.create_task(
                            publish_dali2_event(
                                incoming_command, device.mqtt_id, self._mqtt_dispatcher.client
                            )
                        )
