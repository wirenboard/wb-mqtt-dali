import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Union

import paho.mqtt.client as mqtt

from .mqtt_dispatcher import MessageCallback, MQTTDispatcher
from .wbmqtt import ControlMeta, Device, TranslatedTitle


@dataclass
class ControlInfo:  # pylint: disable=R0903
    # MQTT control ID
    # Must be unique per device
    # Control topic /devices/{device_mqtt_name}/controls/{id}
    id: str
    meta: ControlMeta
    value: Optional[str] = None


@dataclass
class DeviceInfo:  # pylint: disable=R0903
    # MQTT device ID
    # Must be unique
    # Device topic /devices/{id}
    id: str
    title: Optional[Union[str, TranslatedTitle]] = None
    controls: List[ControlInfo] = field(default_factory=list)


@dataclass
class DeviceChange:
    # Newly added devices
    added: List[DeviceInfo] = field(default_factory=list)

    # List of device IDs matching DeviceInfo.id
    removed: List[str] = field(default_factory=list)


class ControlHandler:  # pylint: disable=R0903
    def __init__(self, device_id: str, control_id: str, callback: MessageCallback):
        self.device_id = device_id
        self.control_id = control_id
        self.callback = callback


class DevicePublisher:
    def __init__(
        self,
        mqtt_dispatcher: MQTTDispatcher,
        logger: logging.Logger,
    ):
        self.logger = logger.getChild("DevicePublisher")

        self._mqtt_dispatcher = mqtt_dispatcher
        self._devices: Dict[str, Device] = {}
        self._control_handlers: Dict[str, ControlHandler] = {}
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return

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

            self._control_handlers.clear()

            if self._devices:
                await asyncio.gather(*[device.remove_device() for device in self._devices.values()])

            self._devices.clear()

            self._initialized = False
            self.logger.info("Cleanup completed")

    async def rebuild(self, changes: DeviceChange) -> None:
        async with self._lock:
            self.logger.info(
                "Rebuilding devices: %d added, %d removed",
                len(changes.added),
                len(changes.removed),
            )

            if len(changes.removed):
                await asyncio.gather(
                    *[self._remove_device_internal(device_id) for device_id in changes.removed]
                )

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

    async def set_device_title(self, device_id: str, title: Optional[Union[str, TranslatedTitle]]) -> None:
        async with self._lock:
            if device_id not in self._devices:
                self.logger.warning("Device %s not found", device_id)
                return

            device = self._devices[device_id]
            await device.set_device_title(title)

    async def set_control_value(self, device_id: str, control_id: str, value: str) -> None:
        async with self._lock:
            if device_id not in self._devices:
                self.logger.warning("Device %s not found", device_id)
                return

            device = self._devices[device_id]
            await device.set_control_value(control_id, value)

    async def set_control_title(
        self, device_id: str, control_id: str, title: Union[str, TranslatedTitle]
    ) -> None:
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

    async def register_control_handler(
        self, device_id: str, control_id: str, callback: MessageCallback
    ) -> None:
        """
        Register a handler to receive "on" control messages for a specific device/control.
        The handler is executed in a separate asyncio Task,
        so it can perform asynchronous operations without blocking MQTT message dispatching.

        Parameters
        ----------
        device_id : str
                Identifier of the device for which to register the control handler.
        control_id : str
                Identifier of the control (within the device) to listen for "on" messages.
        callback : MessageCallback
                Callable to be invoked when a message is received for the subscribed topic.
        """

        if inspect.iscoroutinefunction(callback) or inspect.iscoroutine(callback):
            raise ValueError("Async callbacks are not supported. Please provide a synchronous callback.")

        async with self._lock:
            handler_key = f"{device_id}/{control_id}"

            if handler_key in self._control_handlers:
                raise RuntimeError(f"Handler already registered for {device_id}/{control_id}")

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

        del self._control_handlers[handler_key]

        topic = self._get_control_on_topic(device_id, control_id)
        await self._mqtt_dispatcher.unsubscribe(topic)

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

        handlers_to_remove = [key for key in self._control_handlers if key.startswith(device_id)]

        for handler_key in handlers_to_remove:
            parts = handler_key.split("/", 1)
            if len(parts) == 2:
                await self._unregister_control_handler_internal(parts[0], parts[1])

        await device.remove_device()
        del self._devices[device_id]
        self.logger.info("Removed device %s", device_id)

    async def _add_control(self, device: Device, control_info: ControlInfo) -> None:
        value = control_info.value if control_info.value is not None else ""
        await device.create_control(control_info.id, control_info.meta, value)

    def _get_control_on_topic(self, device_id: str, control_id: str) -> str:
        return f"/devices/{device_id}/controls/{control_id}/on"

    def _handle_on_message(self, handler_key: str, message: mqtt.MQTTMessage) -> None:
        if handler_key not in self._control_handlers:
            return

        handler = self._control_handlers[handler_key]
        try:
            if self.logger.isEnabledFor(logging.DEBUG):
                payload = message.payload.decode("utf-8") if message.payload else ""
                self.logger.debug("Handling %s/%s/on: %s", handler.device_id, handler.control_id, payload)
            handler.callback(message)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error(
                "Error handling %s/%s/on: %s",
                handler.device_id,
                handler.control_id,
                e,
                exc_info=True,
            )
