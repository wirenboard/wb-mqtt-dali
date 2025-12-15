import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Optional

import asyncio_mqtt as aiomqtt


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
        await self._publish(self._base_topic + "/meta", None)
        for mqtt_control_name in list(self._controls.keys()):
            await self.remove_control(mqtt_control_name)

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
            await self._publish(self._get_control_base_topic(mqtt_control_name) + "/meta", None)

    async def set_control_value(self, mqtt_control_name: str, value: str, force: bool = False) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control.value != value or force:
                control.value = value
                await self._publish(self._get_control_base_topic(mqtt_control_name), value)
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


async def retain_hack(mqtt_client: aiomqtt.Client) -> None:
    random.seed()
    retain_hack_topic = f"/wbretainhack/{random.random()}"

    event = asyncio.Event()

    async def on_retain_hack(message):
        event.set()

    await mqtt_client.subscribe(retain_hack_topic)

    async def wait_for_message():
        async with mqtt_client.unfiltered_messages() as messages:
            async for message in messages:
                if message.topic == retain_hack_topic:
                    await on_retain_hack(message)
                    break

    listener_task = asyncio.create_task(wait_for_message())

    await mqtt_client.publish(retain_hack_topic, "2", qos=2)

    try:
        await asyncio.wait_for(event.wait(), timeout=10)
    except asyncio.TimeoutError:
        logging.warning("Retain hack timeout")
    finally:
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass
        await mqtt_client.unsubscribe(retain_hack_topic)


async def remove_topics_by_device_prefix(mqtt_client: aiomqtt.Client, device_prefix: str) -> None:
    topics = []
    pattern = "/devices/" + device_prefix
    devices_pattern = "/devices/#"

    await mqtt_client.subscribe(devices_pattern)

    retain_hack_done = asyncio.Event()

    async def collect_topics():
        async with mqtt_client.unfiltered_messages() as messages:
            async for message in messages:
                topic = str(message.topic)
                if topic.startswith(pattern):
                    topics.append(topic)
                if retain_hack_done.is_set():
                    break

    collector_task = asyncio.create_task(collect_topics())

    await retain_hack(mqtt_client)
    retain_hack_done.set()

    await asyncio.sleep(0.05)

    collector_task.cancel()
    try:
        await collector_task
    except asyncio.CancelledError:
        pass

    await mqtt_client.unsubscribe(devices_pattern)

    for topic in topics:
        logging.debug("Clear old topic %s", topic)
        await mqtt_client.publish(topic, None, retain=True)
