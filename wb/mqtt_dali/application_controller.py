import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from timeit import default_timer
from typing import Optional

from dali.address import DeviceBroadcast
from dali.command import from_frame
from dali.device.general import StartQuiescentMode, StopQuiescentMode
from dali.frame import ForwardFrame, Frame

from .commissioning import Commissioning
from .dali_device import DaliDevice, make_device
from .device_publisher import DeviceChange, DevicePublisher
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
    devices: list[DaliDevice]
    websocket_config: WebSocketConfig = field(default_factory=WebSocketConfig)


class ApplicationController:
    def __init__(
        self,
        config: ApplicationControllerConfig,
        mqtt_dispatcher: MQTTDispatcher,
    ) -> None:
        self.uid = f"{config.mqtt_device_id}_bus_{config.bus_index}"
        self.bus_name = f"Bus {config.bus_index}"
        self.devices = config.devices
        self.websocket_config = config.websocket_config
        self.logger = logging.getLogger(self.uid)

        self._state = ApplicationControllerState.UNINITIALIZED
        self._state_lock = asyncio.Lock()
        self._ready_condition = asyncio.Condition(self._state_lock)

        self._quiescent_mode_timer: Optional[asyncio.TimerHandle] = None
        self._active_task: Optional[asyncio.Task] = None

        self._device_publisher = DevicePublisher(mqtt_dispatcher, self.uid, self.logger)

        self._dev_inst_map = AsyncDeviceInstanceTypeMapper()
        cfg = WBDALIConfig(
            device_name=config.mqtt_device_id,
            channel=config.bus_index + 1,
        )
        self._dev = WBDALIDriver(cfg, mqtt_dispatcher, self.logger, self._dev_inst_map)

        self._websocket_task: Optional[asyncio.Task] = None
        self._websocket_lock = asyncio.Lock()
        self._dev.bus_traffic.register(self._handle_bus_traffic_frame)

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

        for device in self.devices:
            device_info = {
                "id": str(device.address.short),
                "title": device.name,
                "driver": "wb-mqtt-dali",
                "controls": [],
            }
            await self._device_publisher.add_device(device_info)

        await self._device_publisher.initialize()

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

        if self._quiescent_mode_timer:
            self._quiescent_mode_timer.cancel()

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

    async def load_device_info(self, device: DaliDevice, force_reload: bool = False) -> None:
        await self._run_task(
            ApplicationControllerState.GENERIC_TASK, device.load_info(self._dev, force_reload)
        )

    async def apply_parameters(self, device: DaliDevice, new_params: dict) -> None:
        await self._run_task(
            ApplicationControllerState.GENERIC_TASK, device.apply_parameters(self._dev, new_params)
        )

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
            obj = Commissioning(self._dev, [d.address for d in self.devices])
            res = await obj.smart_extend()
        finally:
            await self._dev.send(StopQuiescentMode(DeviceBroadcast()))

        unchanged_devices = [d for d in self.devices if d.address in res.unchanged]
        changed_devices = [make_device(self.uid, d.new) for d in res.changed]
        new_devices = [make_device(self.uid, addr) for addr in res.new]

        old_device_ids = {str(d.address.short) for d in self.devices}

        self.devices = unchanged_devices + changed_devices + new_devices
        self.devices.sort(key=lambda d: d.address.short)

        new_device_ids = {str(d.address.short) for d in self.devices}

        removed_ids = list(old_device_ids - new_device_ids)
        added_devices = [
            {
                "id": str(d.address.short),
                "title": d.name,
                "driver": "wb-mqtt-dali",
                "controls": [],
            }
            for d in new_devices
        ]
        updated_devices = [
            {
                "id": str(d.address.short),
                "title": d.name,
                "driver": "wb-mqtt-dali",
                "controls": [],
            }
            for d in changed_devices
        ]

        changes = DeviceChange(
            added=added_devices,
            removed=removed_ids,
            updated=updated_devices,
        )

        end_time = default_timer()
        self.logger.debug("Commissioning completed in %.2f seconds", end_time - start_time)

        await self._device_publisher.rebuild(changes)

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
            command = from_frame(frame)
            if source in ["bus", LUNATONE_IOT_EMULATOR_WBDALIDRIVER_SOURCE]:
                try:
                    if isinstance(command, StartQuiescentMode):
                        asyncio.create_task(self._handle_start_quiescent_mode())
                    elif isinstance(command, StopQuiescentMode):
                        asyncio.create_task(self._handle_stop_quiescent_mode())
                except Exception:
                    pass  # Ignore errors in bus traffic handling
