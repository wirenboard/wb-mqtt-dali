import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from timeit import default_timer
from typing import Optional, Sequence, Union

from dali.address import DeviceBroadcast
from dali.command import from_frame
from dali.device.general import StartQuiescentMode, StopQuiescentMode
from dali.frame import ForwardFrame, Frame

from .commissioning import Commissioning, CommissioningResult
from .common_dali2_controls import get_dali2_controls, publish_dali2_event
from .common_gear_controls import (
    build_polling_queries,
    get_common_controls,
    publish_polling_results,
    register_common_handlers,
)
from .dali_2_device import Dali2Device, make_dali2_device
from .dali_device import DaliDevice, make_dali_device
from .device_publisher import DeviceChange, DeviceInfo, DevicePublisher
from .fake_lunatone_iot import LUNATONE_IOT_EMULATOR_WBDALIDRIVER_SOURCE, run_websocket
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
    mqtt_device_id: str
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
    ) -> None:
        self.uid = f"{config.mqtt_device_id}_bus_{config.bus_index}"
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

        self._mqtt_dispatcher = mqtt_dispatcher
        self._device_publisher = DevicePublisher(mqtt_dispatcher, self.logger)

        self._dev_inst_map = AsyncDeviceInstanceTypeMapper()
        cfg = WBDALIConfig(
            device_name=config.mqtt_device_id,
            channel=config.bus_index + 1,
        )

        self._polling_interval = config.polling_interval
        self._polling_task: Optional[asyncio.Task] = None
        self._dev = WBDALIDriver(cfg, mqtt_dispatcher, self.logger, self._dev_inst_map)

        self._websocket_task: Optional[asyncio.Task] = None
        self._websocket_lock = asyncio.Lock()
        self._bus_traffic_cleanup = self._dev.bus_traffic.register(self._handle_bus_traffic_frame)

    @property
    def polling_interval(self) -> float:
        return self._polling_interval

    async def start(self) -> None:
        async with self._state_lock:
            if self._state != ApplicationControllerState.UNINITIALIZED:
                raise RuntimeError("ApplicationController must be in UNINITIALIZED state to start")
            self._state = ApplicationControllerState.INITIALIZING

        try:
            await self._dev.initialize()
            await self._dev_inst_map.async_autodiscover(
                self._dev, [d.address.short for d in self.dali2_devices]
            )
        except Exception as e:
            async with self._state_lock:
                self._state = ApplicationControllerState.UNINITIALIZED
            raise RuntimeError("Failed to initialize WBDALIDriver") from e

        for device in self.dali_devices:
            device_info = DeviceInfo(device.uid, device.name, get_common_controls())
            await self._device_publisher.add_device(device_info)
            await register_common_handlers(device, self, self._device_publisher)

        self._update_dali2_devices_instances({d.address.short: d for d in self.dali2_devices})
        for device in self.dali2_devices:
            device_info = DeviceInfo(device.uid, device.name, get_dali2_controls(device))
            await self._device_publisher.add_device(device_info)

        await self._device_publisher.initialize()

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
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
            self._active_task = None

        await self._dev.deinitialize()
        async with self._state_lock:
            self._state = ApplicationControllerState.UNINITIALIZED

    async def rescan_bus(self) -> None:
        await self._run_task(ApplicationControllerState.COMMISSIONING, self._commissioning_task())

    def is_commissioning(self) -> bool:
        return self._state == ApplicationControllerState.COMMISSIONING

    async def load_device_info(
        self, device: Union[DaliDevice, Dali2Device], force_reload: bool = False
    ) -> None:
        await self._run_task(
            ApplicationControllerState.GENERIC_TASK, device.load_info(self._dev, force_reload)
        )

    async def apply_parameters(self, device: Union[DaliDevice, Dali2Device], new_params: dict) -> None:
        await self._run_task(
            ApplicationControllerState.GENERIC_TASK, device.apply_parameters(self._dev, new_params)
        )

    async def send_command(self, command):
        return await self._run_task(ApplicationControllerState.GENERIC_TASK, self._dev.send(command))

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

    async def _run_task(self, new_state: ApplicationControllerState, task) -> None:
        try:
            async with self._ready_condition:
                await asyncio.wait_for(
                    self._ready_condition.wait_for(lambda: self._state == ApplicationControllerState.READY),
                    timeout=1.0,
                )
                self._state = new_state
        except asyncio.TimeoutError as e:
            raise RuntimeError("Bus is busy") from e
        self._active_task = asyncio.create_task(task)
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
        changed_devices = [make_dali_device(self.uid, d.new) for d in commissioning_result.changed]
        new_devices = [make_dali_device(self.uid, addr) for addr in commissioning_result.new]

        old_device_ids = {d.uid for d in self.dali_devices}

        self.dali_devices = unchanged_devices + changed_devices + new_devices
        self.dali_devices.sort(key=lambda d: d.address.short)

        new_device_ids = {d.uid for d in self.dali_devices}
        removed_ids = list(old_device_ids - new_device_ids)
        added_devices = [DeviceInfo(d.uid, d.name, get_common_controls()) for d in new_devices]
        updated_devices = [DeviceInfo(d.uid, d.name, get_common_controls()) for d in changed_devices]

        changes = DeviceChange(
            added=added_devices,
            removed=removed_ids,
            updated=updated_devices,
        )

        await self._device_publisher.rebuild(changes)

        for device in new_devices + changed_devices:
            await register_common_handlers(device, self, self._device_publisher)

    async def _update_dali2_devices(self, commissioning_result: CommissioningResult) -> None:
        unchanged_devices = [d for d in self.dali2_devices if d.address in commissioning_result.unchanged]
        changed_devices = [make_dali2_device(self.uid, d.new) for d in commissioning_result.changed]
        new_devices = [make_dali2_device(self.uid, addr) for addr in commissioning_result.new]

        old_device_ids = {d.uid for d in self.dali2_devices}
        self.dali2_devices = unchanged_devices + changed_devices + new_devices
        self.dali2_devices.sort(key=lambda d: d.address.short)

        await self._dev_inst_map.async_autodiscover(self._dev, [d.address.short for d in self.dali2_devices])
        self._update_dali2_devices_instances({d.address.short: d for d in changed_devices + new_devices})

        new_device_ids = {d.uid for d in self.dali2_devices}
        removed_ids = list(old_device_ids - new_device_ids)
        added_devices = [DeviceInfo(d.uid, d.name, get_dali2_controls(d)) for d in new_devices]
        updated_devices = [DeviceInfo(d.uid, d.name, get_dali2_controls(d)) for d in changed_devices]

        changes = DeviceChange(
            added=added_devices,
            removed=removed_ids,
            updated=updated_devices,
        )

        await self._device_publisher.rebuild(changes)

    def _update_dali2_devices_instances(self, dali2_devices_by_addr: dict[int, Dali2Device]) -> None:
        for (addr, inst_num), inst_type in self._dev_inst_map.mapping.items():
            device = dali2_devices_by_addr.get(addr)
            if device:
                self.logger.debug(
                    "Adding instance %d of type %d to DALI 2 device %s", inst_num, inst_type, device.uid
                )
                device.add_instance(inst_num, inst_type)

    async def _polling_loop(self) -> None:
        reschedule = True
        try:
            await asyncio.sleep(self._polling_interval)

            devices = tuple(self.dali_devices)
            if devices:
                try:
                    await self._run_task(
                        ApplicationControllerState.GENERIC_TASK,
                        self._poll_devices(devices),
                    )
                except RuntimeError as e:
                    self.logger.debug("Skipping polling cycle: %s", e)

        except asyncio.CancelledError:
            reschedule = False
            self.logger.info("Polling loop cancelled")
            raise
        except Exception as e:
            self.logger.error("Unexpected error in polling loop: %s", e, exc_info=True)
            await asyncio.sleep(1)
        finally:
            if reschedule and self._state not in (
                ApplicationControllerState.STOPPING,
                ApplicationControllerState.UNINITIALIZED,
            ):
                self._polling_task = asyncio.create_task(self._polling_loop())

    async def _poll_devices(self, devices: Sequence[DaliDevice]) -> None:
        queries = build_polling_queries(devices)
        if not queries:
            return
        batch_failed = False
        try:
            responses = await self._dev.send_commands(queries, source="polling")
        except asyncio.TimeoutError:
            self.logger.warning("Batch poll timeout")
            responses = [None] * len(queries)
            batch_failed = True
        except Exception as e:
            self.logger.error("Batch poll failed: %s", e)
            responses = [None] * len(queries)
            batch_failed = True

        if not batch_failed:
            for device, response in zip(devices, responses):
                if response is None:
                    self.logger.warning("No response during polling for device %s", device.name)

        await publish_polling_results(
            devices,
            responses,
            self._device_publisher,
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
                self._ready_condition.notify()

    def _handle_bus_traffic_frame(self, frame: Frame, source: str) -> None:
        if isinstance(frame, ForwardFrame):
            command = from_frame(frame, dev_inst_map=self._dev_inst_map)
            if source in ["bus", LUNATONE_IOT_EMULATOR_WBDALIDRIVER_SOURCE]:
                try:
                    if isinstance(command, StartQuiescentMode):
                        asyncio.create_task(self._handle_start_quiescent_mode())
                        return
                    if isinstance(command, StopQuiescentMode):
                        asyncio.create_task(self._handle_stop_quiescent_mode())
                        return
                except Exception:
                    pass  # Ignore errors in bus traffic handling
            asyncio.create_task(
                publish_dali2_event(command, self.dali2_devices, self._mqtt_dispatcher.client)
            )
