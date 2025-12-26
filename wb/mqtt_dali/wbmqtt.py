import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Optional

import asyncio_mqtt as aiomqtt

from .mqtt_dispatcher import MQTTDispatcher


@dataclass
class ControlMeta:
    control_type: str = "value"
    read_only: bool = False
    title: Optional[str] = None
    order: Optional[int] = None


@dataclass
class ControlState:
    meta: ControlMeta
    value: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self):
        self.meta = ControlMeta(
            control_type=self.meta.control_type,
            read_only=self.meta.read_only,
            title=self.meta.title,
            order=self.meta.order,
        )


class Device:
    def __init__(
        self,
        mqtt_client: aiomqtt.Client,
        device_mqtt_name: str,
        driver_name: str,
        device_title: Optional[str] = None,
    ) -> None:
        self._mqtt_client = mqtt_client
        self._base_topic = f"/devices/{device_mqtt_name}"
        self._device_mqtt_name = device_mqtt_name
        self._driver_name = driver_name
        self._device_title = device_title
        self._controls: dict[str, ControlState] = {}
        self._initialized = False

    async def initialize(self) -> None:
        if not self._initialized:
            await self._publish_device_meta()
            self._initialized = True

    async def republish_device(self) -> None:
        await self._publish_device_meta()
        for mqtt_control_name in list(self._controls.keys()):
            await self.republish_control(mqtt_control_name)

    async def remove_device(self) -> None:
        for mqtt_control_name in list(self._controls.keys()):
            await self.remove_control(mqtt_control_name)
        await self._publish(self._base_topic + "/meta", None)

    async def create_control(self, mqtt_control_name: str, meta: ControlMeta, value: str) -> None:
        self._controls[mqtt_control_name] = ControlState(meta=meta, value=None)
        await self._publish_control_meta(mqtt_control_name, meta)
        await self.set_control_value(mqtt_control_name, value)

    async def republish_control(self, mqtt_control_name: str) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control:
                await self._publish_control_meta(mqtt_control_name, control.meta)
                await self.set_control_value(mqtt_control_name, control.value, force=True)

    async def remove_control(self, mqtt_control_name: str) -> None:
        if mqtt_control_name in self._controls:
            self._controls.pop(mqtt_control_name)
            await self._publish(self._get_control_base_topic(mqtt_control_name), None)
            await self._publish(self._get_control_base_topic(mqtt_control_name) + "/meta/error", None)
            await self._publish(self._get_control_base_topic(mqtt_control_name) + "/meta", None)

    async def set_control_value(self, mqtt_control_name: str, value: str, force: bool = False) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control.value != value or force:
                control.value = value
                await self._publish(self._get_control_base_topic(mqtt_control_name), value)
            if control.error is not None:
                await self.set_control_error(mqtt_control_name, "")
        else:
            logging.debug("Can't set value of undeclared control %s", mqtt_control_name)

    async def set_control_read_only(self, mqtt_control_name: str, read_only: bool) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control.meta.read_only != read_only:
                control.meta.read_only = read_only
                await self._publish_control_meta(mqtt_control_name, control.meta)
        else:
            logging.debug(
                "Can't set readonly property of undeclared control %s",
                mqtt_control_name,
            )

    async def set_control_title(self, mqtt_control_name: str, title: str) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control.meta.title != title:
                control.meta.title = title
                await self._publish_control_meta(mqtt_control_name, control.meta)
        else:
            logging.debug("Can't set title of undeclared control %s", mqtt_control_name)

    async def set_control_error(self, mqtt_control_name: str, error: str) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            control.error = error if error else None
            error_topic = self._get_control_base_topic(mqtt_control_name) + "/meta/error"
            await self._publish(error_topic, error if error else None)
        else:
            logging.debug("Can't set error of undeclared control %s", mqtt_control_name)

    def _get_control_base_topic(self, mqtt_control_name: str) -> str:
        return f"{self._base_topic}/controls/{mqtt_control_name}"

    async def _publish_device_meta(self) -> None:
        meta_dict = {
            "driver": self._driver_name,
        }
        if self._device_title is not None:
            meta_dict["title"] = {"en": self._device_title}
        meta_json = json.dumps(meta_dict)
        await self._publish(self._base_topic + "/meta", meta_json)

    async def _publish_control_meta(self, mqtt_control_name: str, meta: ControlMeta) -> None:
        meta_dict = {
            "type": meta.control_type,
            "readonly": meta.read_only,
        }
        if meta.title is not None:
            meta_dict["title"] = {"en": meta.title}
        if meta.order is not None:
            meta_dict["order"] = meta.order

        if meta_dict:
            meta_json = json.dumps(meta_dict)
            await self._publish(self._get_control_base_topic(mqtt_control_name) + "/meta", meta_json)

    async def _publish(self, topic: str, value: Optional[str]) -> None:
        if value is None:
            logging.debug('Clear "%s"', topic)
        else:
            logging.debug('Publish "%s" "%s"', topic, value)
        await self._mqtt_client.publish(topic, value, retain=True)


async def retain_hack(mqtt_dispatcher: MQTTDispatcher) -> None:
    random.seed()
    retain_hack_topic = f"/wbretainhack/{random.random()}"

    event = asyncio.Event()

    async def on_retain_hack(_message):
        event.set()

    await mqtt_dispatcher.subscribe(retain_hack_topic, on_retain_hack)

    await mqtt_dispatcher.client.publish(retain_hack_topic, "2", qos=2)

    try:
        await asyncio.wait_for(event.wait(), timeout=10)
    except asyncio.TimeoutError:
        logging.warning("Retain hack timeout")
    finally:
        await mqtt_dispatcher.unsubscribe(retain_hack_topic)


async def remove_topics_by_driver(mqtt_dispatcher: MQTTDispatcher, driver_name: str) -> None:
    all_topics = []
    devices_to_remove = []
    devices_pattern = "/devices/#"

    async def collect_devices(message):
        topic = str(message.topic)
        all_topics.append(topic)
        parts = topic.split("/")
        if len(parts) == 4 and parts[3] == "meta" and message.payload:
            device_name = parts[2]
            try:
                meta = json.loads(message.payload.decode("utf-8"))
                if meta.get("driver") == driver_name:
                    devices_to_remove.append(device_name)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logging.debug("Failed to parse meta for %s: %s", topic, e)

    await mqtt_dispatcher.subscribe(devices_pattern, collect_devices)
    await retain_hack(mqtt_dispatcher)
    await asyncio.sleep(0.05)
    await mqtt_dispatcher.unsubscribe(devices_pattern)

    if not devices_to_remove:
        logging.debug("No devices found with driver '%s'", driver_name)
        return

    logging.info(
        "Found %d device(s) with driver '%s': %s",
        len(devices_to_remove),
        driver_name,
        devices_to_remove,
    )

    topics_to_remove = []
    for topic in all_topics:
        for device_name in devices_to_remove:
            if topic.startswith(f"/devices/{device_name}/"):
                topics_to_remove.append(topic)
                break

    logging.info("Removing %d topics for driver '%s'", len(topics_to_remove), driver_name)
    for topic in topics_to_remove:
        await mqtt_dispatcher.client.publish(topic, None, retain=True)
