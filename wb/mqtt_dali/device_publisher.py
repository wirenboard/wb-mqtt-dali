import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

import paho.mqtt.client as mqtt

from .mqtt_dispatcher import MQTTDispatcher
from .wbmqtt import ControlMeta, Device, remove_topics_by_driver


@dataclass
class ControlInfo:  # pylint: disable=R0903
    # MQTT control ID
    # Must be unique per device
    # Control topic /devices/{device_mqtt_name}/controls/{id}
    id: str
    title: Optional[str] = None
    type: str = "value"
    value: Optional[str] = None
    read_only: bool = False
    order: Optional[int] = None


@dataclass
class DeviceInfo:  # pylint: disable=R0903
    # MQTT device ID
    # Must be unique
    # Device topic /devices/{id}
    id: str
    title: Optional[str] = None
    controls: List[ControlInfo] = field(default_factory=list)


@dataclass
class DeviceChange:
    # Newly added devices
    added: List[DeviceInfo] = field(default_factory=list)

    # List of device IDs matching DeviceInfo.id
    removed: List[str] = field(default_factory=list)

    # Updated devices
    # id must match existing device IDs
    updated: List[DeviceInfo] = field(default_factory=list)


class ControlHandler:  # pylint: disable=R0903
    def __init__(self, device_id: str, control_id: str, callback: Callable):
        self.device_id = device_id
        self.control_id = control_id
        self.callback = callback


class DevicePublisher:
    def __init__(
        self,
        mqtt_dispatcher: MQTTDispatcher,
        logger: logging.Logger,
    ):
        self._mqtt_dispatcher = mqtt_dispatcher
        self._devices: Dict[str, Device] = {}
        self._control_handlers: Dict[str, ControlHandler] = {}
        self._initialized = False
        self._lock = asyncio.Lock()
        self._on_topic_running_handlers: Set[asyncio.Task] = set()
        self.logger = logger.getChild("DevicePublisher")

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return

            await remove_topics_by_driver(self._mqtt_dispatcher, "wb-mqtt-dali")

            if self._devices:
                await asyncio.gather(*[device.initialize() for device in self._devices.values()])

            self._initialized = True
            self.logger.info("Initialized successfully")

    async def cleanup(self) -> None:
        async with self._lock:
            self.logger.info("Cleaning up all devices")

            if self._control_handlers:
                await asyncio.gather(
                    *[
                        self._mqtt_dispatcher.unsubscribe(
                            self._get_control_on_topic(handler.device_id, handler.control_id)
                        )
                        for handler in self._control_handlers.values()
                    ]
                )

            # Make a copy of the set to avoid modification during iteration
            tasks_to_cancel = list(self._on_topic_running_handlers)
            for task in tasks_to_cancel:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    # Task cancellation is expected during cleanup; suppress the exception.
                    pass
            self._on_topic_running_handlers.clear()

            for task in self._on_topic_running_handlers:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._on_topic_running_handlers.clear()

            self._control_handlers.clear()

            if self._devices:
                await asyncio.gather(*[device.remove_device() for device in self._devices.values()])

            self._devices.clear()

            self._initialized = False
            self.logger.info("Cleanup completed")

    async def rebuild(self, changes: DeviceChange) -> None:
        async with self._lock:
            self.logger.info(
                "Rebuilding devices: %d added, %d removed, %d updated",
                len(changes.added),
                len(changes.removed),
                len(changes.updated),
            )

            if len(changes.removed):
                await asyncio.gather(
                    *[self._remove_device_internal(device_id) for device_id in changes.removed]
                )

            if len(changes.updated):
                update_tasks = []
                for device_info in changes.updated:
                    if device_info.id in self._devices:
                        update_tasks.append(self._update_device_internal(device_info))
                    else:
                        self.logger.warning("Device %s marked for update but not found", device_info.id)
                if update_tasks:
                    await asyncio.gather(*update_tasks)

            if len(changes.added):
                await asyncio.gather(
                    *[self._add_device_internal(device_info) for device_info in changes.added]
                )

            self.logger.info("Rebuild completed")

    async def add_device(self, device_info: DeviceInfo) -> None:
        async with self._lock:
            await self._add_device_internal(device_info)

    async def remove_device(self, device_id: str) -> None:
        async with self._lock:
            await self._remove_device_internal(device_id)

    async def update_device(self, device_info: DeviceInfo) -> None:
        async with self._lock:
            await self._update_device_internal(device_info)

    async def set_control_value(self, device_id: str, control_id: str, value: str) -> None:
        async with self._lock:
            if device_id not in self._devices:
                self.logger.warning("Device %s not found", device_id)
                return

            device = self._devices[device_id]
            await device.set_control_value(control_id, value)

    async def set_control_title(self, device_id: str, control_id: str, title: str) -> None:
        async with self._lock:
            if device_id not in self._devices:
                self.logger.warning("Device %s not found", device_id)
                return

            device = self._devices[device_id]
            await device.set_control_title(control_id, title)

    async def set_control_error(self, device_id: str, control_id: str, error: str) -> None:
        async with self._lock:
            if device_id not in self._devices:
                self.logger.warning("Device %s not found", device_id)
                return

            device = self._devices[device_id]
            await device.set_control_error(control_id, error)

    async def register_control_handler(self, device_id: str, control_id: str, callback: Callable) -> None:
        async with self._lock:
            handler_key = f"{device_id}/{control_id}"

            if handler_key in self._control_handlers:
                self.logger.warning("Handler already registered for %s/%s", device_id, control_id)
                return

            topic = self._get_control_on_topic(device_id, control_id)
            handler = ControlHandler(device_id, control_id, callback)
            self._control_handlers[handler_key] = handler

            await self._mqtt_dispatcher.subscribe(
                topic, lambda msg: self._handle_on_message(handler_key, msg)
            )

            self.logger.debug("Registered handler for %s", topic)

    async def unregister_control_handler(self, device_id: str, control_id: str) -> None:
        async with self._lock:
            await self._unregister_control_handler_internal(device_id, control_id)

    async def _unregister_control_handler_internal(self, device_id: str, control_id: str) -> None:
        handler_key = f"{device_id}/{control_id}"

        if handler_key not in self._control_handlers:
            return

        topic = self._get_control_on_topic(device_id, control_id)

        await self._mqtt_dispatcher.unsubscribe(topic)
        del self._control_handlers[handler_key]

        self.logger.debug("Unregistered handler for %s", topic)

    def get_device_ids(self) -> Set[str]:
        return set(self._devices.keys())

    def has_device(self, device_id: str) -> bool:
        return device_id in self._devices

    async def _add_device_internal(self, device_info: DeviceInfo) -> None:
        device_id = device_info.id

        if device_id in self._devices:
            raise RuntimeError(f"Device {device_id} already exists")

        device = Device(
            mqtt_client=self._mqtt_dispatcher.client,
            device_mqtt_name=device_id,
            driver_name="wb-mqtt-dali",
            device_title=device_info.title,
        )

        if self._initialized:
            await device.initialize()

        for control_info in device_info.controls:
            await self._add_control(device, control_info)

        self._devices[device_id] = device
        self.logger.info("Added device %s", device_id)

    async def _remove_device_internal(self, device_id: str) -> None:
        if device_id not in self._devices:
            self.logger.warning("Device %s not found for removal", device_id)
            return

        device = self._devices[device_id]

        handlers_to_remove = [key for key in self._control_handlers.keys() if key.startswith(device_id)]

        for handler_key in handlers_to_remove:
            parts = handler_key.split("/", 1)
            if len(parts) == 2:
                await self._unregister_control_handler_internal(parts[0], parts[1])

        await device.remove_device()
        del self._devices[device_id]
        self.logger.info("Removed device %s", device_id)

    async def _update_device_internal(self, device_info: DeviceInfo) -> None:
        device_id = device_info.id

        if device_id not in self._devices:
            self.logger.warning("Device %s not found for update", device_id)
            return

        device = self._devices[device_id]

        existing_controls = device.control_ids
        new_controls = {c.id for c in device_info.controls}

        for control_id in existing_controls - new_controls:
            await device.remove_control(control_id)
            await self._unregister_control_handler_internal(device_id, control_id)

        for control_info in device_info.controls:
            if control_info.id in existing_controls:
                await self._update_control(device, control_info.id, control_info)
            else:
                await self._add_control(device, control_info)

        if device_info.title != device.title:
            await device.set_device_title(device_info.title)

        self.logger.info("Updated device %s", device_id)

    async def _add_control(self, device: Device, control_info: ControlInfo) -> None:
        meta = ControlMeta(
            title=control_info.title,
            control_type=control_info.type,
            order=control_info.order,
            read_only=control_info.read_only,
        )
        value = control_info.value if control_info.value is not None else ""
        await device.create_control(control_info.id, meta, value)

    async def _update_control(self, device: Device, control_id: str, control_info: ControlInfo) -> None:
        await device.set_control_value(control_id, control_info.value)
        if control_info.title is not None:
            await device.set_control_title(control_id, control_info.title)
        await device.set_control_read_only(control_id, control_info.read_only)

    def _get_control_on_topic(self, device_id: str, control_id: str) -> str:
        return f"/devices/{device_id}/controls/{control_id}/on"

    async def _handle_on_message(self, handler_key: str, message: mqtt.MQTTMessage) -> None:
        if handler_key not in self._control_handlers:
            return

        handler = self._control_handlers[handler_key]

        try:
            payload = message.payload.decode("utf-8") if message.payload else ""
            self.logger.debug(
                "Handling /on message for %s/%s: %s",
                handler.device_id,
                handler.control_id,
                payload,
            )

            def task_finished(fut: asyncio.Task) -> None:
                self._on_topic_running_handlers.discard(fut)
                if fut.exception():
                    self.logger.error(
                        "Error handling /on message for %s/%s: %s",
                        handler.device_id,
                        handler.control_id,
                        fut.exception(),
                        exc_info=True,
                    )

            task = asyncio.create_task(handler.callback(message))
            self._on_topic_running_handlers.add(task)
            task.add_done_callback(task_finished)
        except Exception as e:
            self.logger.error(
                "Error handling /on message for %s/%s: %s",
                handler.device_id,
                handler.control_id,
                e,
                exc_info=True,
            )
