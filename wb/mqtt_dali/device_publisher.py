import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Set

from .mqtt_dispatcher import MQTTDispatcher
from .wbmqtt import ControlMeta, Device, remove_topics_by_driver


class DeviceChange:  # pylint: disable=R0903
    def __init__(
        self,
        added: Optional[List[Dict[str, Any]]] = None,
        removed: Optional[List[str]] = None,
        updated: Optional[List[Dict[str, Any]]] = None,
    ):
        self.added = added or []
        self.removed = removed or []
        self.updated = updated or []


class ControlHandler:  # pylint: disable=R0903
    def __init__(self, device_id: str, control_id: str, callback: Callable):
        self.device_id = device_id
        self.control_id = control_id
        self.callback = callback


class DevicePublisher:
    def __init__(
        self,
        mqtt_dispatcher: MQTTDispatcher,
        bus_id: str,
    ):
        self._mqtt_dispatcher = mqtt_dispatcher
        self._bus_id = bus_id
        self._devices: Dict[str, Device] = {}
        self._control_handlers: Dict[str, ControlHandler] = {}
        self._initialized = False
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger(f"{__name__}.{bus_id}")

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return

            await remove_topics_by_driver(self._mqtt_dispatcher, "wb-mqtt-dali")

            if self._devices:
                await asyncio.gather(*[device.initialize() for device in self._devices.values()])

            self._initialized = True
            self.logger.info("DevicePublisher initialized for bus %s", self._bus_id)

    async def cleanup(self) -> None:
        async with self._lock:
            self.logger.info("Cleaning up all devices for bus %s", self._bus_id)

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
            self.logger.info("Cleanup completed for bus %s", self._bus_id)

    async def rebuild(self, changes: DeviceChange) -> None:
        async with self._lock:
            self.logger.info(
                "Rebuilding devices: %d added, %d removed, %d updated",
                len(changes.added),
                len(changes.removed),
                len(changes.updated),
            )

            if changes.removed:
                await asyncio.gather(
                    *[self._remove_device_internal(device_id) for device_id in changes.removed]
                )

            if changes.updated:
                update_tasks = []
                for device_info in changes.updated:
                    device_id = device_info["id"]
                    if device_id in self._devices:
                        update_tasks.append(self._update_device_internal(device_id, device_info))
                    else:
                        self.logger.warning("Device %s marked for update but not found", device_id)
                if update_tasks:
                    await asyncio.gather(*update_tasks)

            if changes.added:
                await asyncio.gather(
                    *[self._add_device_internal(device_info) for device_info in changes.added]
                )

            self.logger.info("Rebuild completed for bus %s", self._bus_id)

    async def add_device(self, device_info: Dict[str, Any]) -> None:
        async with self._lock:
            await self._add_device_internal(device_info)

    async def remove_device(self, device_id: str) -> None:
        async with self._lock:
            await self._remove_device_internal(device_id)

    async def update_device(self, device_id: str, device_info: Dict[str, Any]) -> None:
        async with self._lock:
            await self._update_device_internal(device_id, device_info)

    async def set_control_value(self, device_id: str, control_id: str, value: str) -> None:
        async with self._lock:
            if device_id not in self._devices:
                self.logger.warning("Device %s not found", device_id)
                return

            device = self._devices[device_id]
            await device.set_control_value(control_id, value)

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

    async def _add_device_internal(self, device_info: Dict[str, Any]) -> None:
        device_id = device_info["id"]
        device_mqtt_name = f"{self._bus_id}_{device_id}"

        if device_id in self._devices:
            self.logger.warning("Device %s already exists, skipping", device_id)
            return

        device = Device(
            mqtt_client=self._mqtt_dispatcher.client,
            device_mqtt_name=device_mqtt_name,
            driver_name=device_info.get("driver", self._bus_id),
            device_title=device_info.get("title"),
        )

        if self._initialized:
            await device.initialize()

        for control_info in device_info.get("controls", []):
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

    async def _update_device_internal(self, device_id: str, device_info: Dict[str, Any]) -> None:
        if device_id not in self._devices:
            self.logger.warning("Device %s not found for update", device_id)
            return

        device = self._devices[device_id]

        existing_controls = set(device._controls.keys())
        new_controls = {c["id"] for c in device_info.get("controls", [])}

        for control_id in existing_controls - new_controls:
            await device.remove_control(control_id)
            await self._unregister_control_handler_internal(device_id, control_id)

        for control_info in device_info.get("controls", []):
            control_id = control_info["id"]
            if control_id in existing_controls:
                await self._update_control(device, control_id, control_info)
            else:
                await self._add_control(device, control_info)

        new_title = device_info.get("title")
        new_driver = device_info.get("driver", self._bus_id)

        if new_title != device._device_title or new_driver != device._driver_name:
            device._device_title = new_title
            device._driver_name = new_driver
            await device.republish_device()

        self.logger.info("Updated device %s", device_id)

    async def _add_control(self, device: Device, control_info: Dict[str, Any]) -> None:
        control_id = control_info["id"]
        meta = ControlMeta(
            title=control_info.get("title"),
            control_type=control_info.get("type", "value"),
            order=control_info.get("order"),
            read_only=control_info.get("read_only", False),
        )
        value = control_info.get("value", "")
        await device.create_control(control_id, meta, value)

    async def _update_control(self, device: Device, control_id: str, control_info: Dict[str, Any]) -> None:
        new_value = control_info.get("value", "")
        await device.set_control_value(control_id, new_value)

        new_title = control_info.get("title")
        if new_title is not None:
            await device.set_control_title(control_id, new_title)

        new_read_only = control_info.get("read_only", False)
        await device.set_control_read_only(control_id, new_read_only)

    def _get_control_on_topic(self, device_id: str, control_id: str) -> str:
        device_mqtt_name = f"{self._bus_id}_{device_id}"
        return f"/devices/{device_mqtt_name}/controls/{control_id}/on"

    async def _handle_on_message(self, handler_key: str, message) -> None:
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
            await handler.callback(message)
        except Exception as e:
            self.logger.error(
                "Error handling /on message for %s/%s: %s",
                handler.device_id,
                handler.control_id,
                e,
                exc_info=True,
            )
