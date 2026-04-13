import asyncio
import json
import logging
import random
import string
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Optional, Union
from urllib.parse import urlparse

import asyncio_mqtt as aiomqtt

from .mqtt_dispatcher import MQTTDispatcher


@dataclass
class TranslatedTitle:
    en: Optional[str] = None
    ru: Optional[str] = None

    def is_empty(self) -> bool:
        return not self.en and not self.ru


class ControlMeta:  # pylint: disable=too-many-instance-attributes, too-few-public-methods, too-many-arguments, R0917
    def __init__(
        self,
        control_type: str = "value",
        title: Optional[Union[str, TranslatedTitle]] = None,
        read_only: bool = False,
        order: Optional[int] = None,
        enum: Optional[dict[str, Optional[TranslatedTitle]]] = None,
        minimum: Optional[Union[int, float]] = None,
        maximum: Optional[Union[int, float]] = None,
        units: Optional[str] = None,
    ) -> None:
        self.control_type = control_type
        if isinstance(title, str):
            self.title: Optional[TranslatedTitle] = TranslatedTitle(en=title)
        else:
            self.title: Optional[TranslatedTitle] = title
        self.read_only = read_only
        self.order = order
        self.enum = enum
        self.minimum = minimum
        self.maximum = maximum
        self.units = units


@dataclass
class ControlState:
    meta: ControlMeta
    value: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self):
        # meta can be changed during runtime with set_control_read_only and set_control_title,
        # so we need to make a copy of it to not modify the original meta passed to the constructor
        self.meta = deepcopy(self.meta)


class Device:
    def __init__(
        self,
        mqtt_client: aiomqtt.Client,
        device_mqtt_name: str,
        driver_name: str,
        device_title: Optional[Union[str, TranslatedTitle]] = None,
    ) -> None:
        self._mqtt_client = mqtt_client
        self._base_topic = f"/devices/{device_mqtt_name}"
        self._device_mqtt_name = device_mqtt_name
        self._driver_name = driver_name
        if isinstance(device_title, str):
            self._device_title: Optional[TranslatedTitle] = TranslatedTitle(en=device_title)
        else:
            self._device_title: Optional[TranslatedTitle] = device_title
        self._controls: dict[str, ControlState] = {}
        self._initialized = False

    @property
    def control_ids(self) -> set[str]:
        return set(self._controls.keys())

    @property
    def title(self) -> Optional[TranslatedTitle]:
        return self._device_title

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

    async def set_control_value(
        self, mqtt_control_name: str, value: Optional[str], force: bool = False
    ) -> None:
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

    async def set_control_title(self, mqtt_control_name: str, title: Union[str, TranslatedTitle]) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if isinstance(title, str):
                title_obj = TranslatedTitle(en=title)
            else:
                title_obj = title
            if control.meta.title != title_obj:
                control.meta.title = title_obj
                await self._publish_control_meta(mqtt_control_name, control.meta)
        else:
            logging.debug("Can't set title of undeclared control %s", mqtt_control_name)

    async def set_control_error(self, mqtt_control_name: str, error: str) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            error_to_set = error if error else None
            if control.error != error_to_set:
                control.error = error_to_set
                error_topic = self._get_control_base_topic(mqtt_control_name) + "/meta/error"
                await self._publish(error_topic, error_to_set)
        else:
            logging.debug("Can't set error of undeclared control %s", mqtt_control_name)

    async def set_device_title(self, title: Optional[Union[str, TranslatedTitle]]) -> None:
        if isinstance(title, str):
            title_obj = TranslatedTitle(en=title)
        else:
            title_obj = title
        if self._device_title != title_obj:
            self._device_title = title_obj
            await self._publish_device_meta()

    def _get_control_base_topic(self, mqtt_control_name: str) -> str:
        return f"{self._base_topic}/controls/{mqtt_control_name}"

    async def _publish_device_meta(self) -> None:
        meta_dict: dict[str, Any] = {
            "driver": self._driver_name,
        }
        if self._device_title is not None and not self._device_title.is_empty():
            meta_dict["title"] = asdict(self._device_title)
        meta_json = json.dumps(meta_dict)
        await self._publish(self._base_topic + "/meta", meta_json)

    async def _publish_control_meta(  # pylint: disable=too-many-branches
        self, mqtt_control_name: str, meta: ControlMeta
    ) -> None:
        meta_dict = {
            "type": meta.control_type,
            "readonly": meta.read_only,
        }
        if meta.title is not None and not meta.title.is_empty():
            meta_dict["title"] = asdict(meta.title)
        if meta.order is not None:
            meta_dict["order"] = meta.order
        if meta.minimum is not None:
            meta_dict["min"] = meta.minimum
        if meta.maximum is not None:
            meta_dict["max"] = meta.maximum
        if meta.units is not None:
            meta_dict["units"] = meta.units
        if meta.enum is not None:
            enum = {}
            for key, value in meta.enum.items():
                translations = {}
                if value is not None:
                    for lang, translation in asdict(value).items():
                        if translation:
                            translations[lang] = translation
                if not translations:
                    translations["en"] = key
                enum[key] = translations
            if enum:
                meta_dict["enum"] = enum
        if meta_dict:
            meta_json = json.dumps(meta_dict)
            await self._publish(self._get_control_base_topic(mqtt_control_name) + "/meta", meta_json)

    async def _publish(self, topic: str, value: Optional[str]) -> None:
        if value is None:
            logging.debug('Clear "%s"', topic)
        else:
            logging.debug('Publish "%s" "%s"', topic, value)
        await self._mqtt_client.publish(topic, value, retain=True)


async def retain_hack(mqtt_dispatcher: MQTTDispatcher, timeout: float = 120.0) -> None:
    random.seed()
    retain_hack_topic = f"/wbretainhack/{random.random()*10000000:.0f}"

    event = asyncio.Event()

    async def on_retain_hack(_message):
        event.set()

    await mqtt_dispatcher.subscribe(retain_hack_topic, on_retain_hack)

    await mqtt_dispatcher.client.publish(retain_hack_topic, "2", qos=2)

    try:
        await asyncio.wait_for(event.wait(), timeout)
    except asyncio.TimeoutError:
        logging.warning("Retain hack timeout")
    finally:
        await mqtt_dispatcher.unsubscribe(retain_hack_topic)


async def remove_topics_by_driver(
    mqtt_dispatcher: MQTTDispatcher, driver_name: str, timeout: float = 120.0
) -> None:
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
    await retain_hack(mqtt_dispatcher, timeout)
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


def make_mqtt_client(broker_url: str) -> aiomqtt.Client:
    urlparse_result = urlparse(broker_url)
    if urlparse_result.scheme == "unix":
        hostname = urlparse_result.path
        port = 0
    else:
        if urlparse_result.hostname is None:
            raise ValueError("No MQTT hostname specified")
        if urlparse_result.port is None:
            raise ValueError("No MQTT port specified")
        hostname = urlparse_result.hostname
        port = urlparse_result.port
    auth = {}
    if urlparse_result.username:
        auth["username"] = urlparse_result.username
    if urlparse_result.password:
        auth["password"] = urlparse_result.password
    client_id_suffix = "".join(random.sample(string.ascii_letters + string.digits, 8))
    client = aiomqtt.Client(
        client_id=f"wb-mqtt-dali-{client_id_suffix}",
        hostname=hostname,
        port=port,
        transport="websockets" if urlparse_result.scheme == "ws" else urlparse_result.scheme,
        logger=logging.getLogger("mqtt_client"),
        **auth,
    )
    return client
