import asyncio
import json
import logging
import random
import string
from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum, Flag, auto
from typing import Any, Optional, Union
from urllib.parse import urlparse

import aiomqtt

from .mqtt_dispatcher import MQTTDispatcher

# A dead link goes unnoticed for one or two keepalives; 15 s keeps that under half a minute.
MQTT_KEEPALIVE_S = 15

# Deadline for a broker acknowledgement (aiomqtt's client-wide setting). At QoS 2 one slow
# PUBCOMP holds DevicePublisher's lock and with it every publish of the bus; aiomqtt's own
# default would hold it for 10 s.
MQTT_PUBLISH_TIMEOUT_S = 5.0


@dataclass
class TranslatedTitle:
    en: Optional[str] = None
    ru: Optional[str] = None

    def is_empty(self) -> bool:
        return not self.en and not self.ru


def as_translated_title(title: Optional[Union[str, TranslatedTitle]]) -> Optional[TranslatedTitle]:
    return TranslatedTitle(en=title) if isinstance(title, str) else title


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
        self.title: Optional[TranslatedTitle] = as_translated_title(title)
        self.read_only = read_only
        self.order = order
        self.enum = enum
        self.minimum = minimum
        self.maximum = maximum
        self.units = units


class ControlError(Flag):
    """Per-control ``/meta/error`` state, modeled as WB error flags.

    Members map to the WB wire characters emitted on ``/meta/error``;
    ``ControlError.NONE`` (the empty flag) means "no error". Only ``READ``
    (``"r"``) and ``WRITE`` (``"w"``) are used by this project — ``p`` (late
    poll) is never detected here, so it is deliberately not modeled. ``Flag``
    requires integer bit values, so ``READ``/``WRITE`` use ``auto()`` and are
    mapped to their characters in :meth:`to_mqtt` rather than carrying the
    string as their value.
    """

    NONE = 0
    READ = auto()
    WRITE = auto()

    def __str__(self) -> str:
        return self.to_mqtt()

    def to_mqtt(self) -> str:
        chars = []
        if self & ControlError.READ:
            chars.append("r")
        if self & ControlError.WRITE:
            chars.append("w")
        return "".join(chars)


PUSHBUTTON_CONTROL_TYPE = "pushbutton"
ALARM_CONTROL_TYPE = "alarm"


def is_pushbutton(meta: ControlMeta) -> bool:
    return meta.control_type == PUSHBUTTON_CONTROL_TYPE


def is_alarm(meta: ControlMeta) -> bool:
    return meta.control_type == ALARM_CONTROL_TYPE


def value_is_retained(meta: ControlMeta) -> bool:
    return meta.control_type != PUSHBUTTON_CONTROL_TYPE


class PublishPolicy(Enum):
    """Whether an update repeating the published value still reaches the topic."""

    ON_CHANGE = auto()  # the value is state: a repeat says nothing new
    ALWAYS = auto()  # the value is an event: a repeat is a second occurrence


@dataclass
class ControlState:
    meta: ControlMeta
    value: Optional[str] = None
    error: ControlError = ControlError.NONE
    publish_policy: PublishPolicy = PublishPolicy.ON_CHANGE

    def __post_init__(self):
        # meta can be changed during runtime with set_control_read_only and set_control_title,
        # so we need to make a copy of it to not modify the original meta passed to the constructor
        self.meta = deepcopy(self.meta)


def _publishes_every_update(control: ControlState) -> bool:
    return control.publish_policy is PublishPolicy.ALWAYS or not value_is_retained(control.meta)


class Device:
    def __init__(
        self,
        mqtt_dispatcher: MQTTDispatcher,
        device_mqtt_name: str,
        driver_name: str,
        device_title: Optional[Union[str, TranslatedTitle]] = None,
    ) -> None:
        self._mqtt_dispatcher = mqtt_dispatcher
        self._base_topic = f"/devices/{device_mqtt_name}"
        self._device_mqtt_name = device_mqtt_name
        self._driver_name = driver_name
        self._device_title: Optional[TranslatedTitle] = as_translated_title(device_title)
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

    async def remove_device(self) -> None:
        for mqtt_control_name in list(self._controls.keys()):
            await self.remove_control(mqtt_control_name)
        await self._publish(self._base_topic + "/meta", None)

    async def create_control(
        self,
        mqtt_control_name: str,
        meta: ControlMeta,
        value: str,
        publish_policy: PublishPolicy = PublishPolicy.ON_CHANGE,
    ) -> None:
        self._controls[mqtt_control_name] = ControlState(meta=meta, value=None, publish_policy=publish_policy)
        await self._publish_control_meta(mqtt_control_name, meta)
        if value_is_retained(meta):
            await self.set_control_value(mqtt_control_name, value)

    async def remove_control(self, mqtt_control_name: str) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls.pop(mqtt_control_name)
            if value_is_retained(control.meta):
                await self._publish(self._get_control_base_topic(mqtt_control_name), None)
            await self._publish(self._get_control_base_topic(mqtt_control_name) + "/meta/error", None)
            await self._publish(self._get_control_base_topic(mqtt_control_name) + "/meta", None)

    async def set_control_value(self, mqtt_control_name: str, value: Optional[str]) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control.value != value or _publishes_every_update(control):
                if await self._publish(
                    self._get_control_base_topic(mqtt_control_name),
                    value,
                    retain=value_is_retained(control.meta),
                ):
                    control.value = value
            if control.error:
                await self.set_control_error(mqtt_control_name, ControlError.NONE)
        else:
            logging.debug("Can't set value of undeclared control %s", mqtt_control_name)

    async def set_control_state(
        self,
        mqtt_control_name: str,
        value: Optional[str],
        error: ControlError,
        title: Optional[Union[str, TranslatedTitle]] = None,
    ) -> None:
        """Publish whichever of the value, the error and the title changed; the news goes last."""
        control = self._controls.get(mqtt_control_name)
        if control is None:
            logging.debug("Can't set state of undeclared control %s", mqtt_control_name)
            return
        if title is not None:
            await self.set_control_title(mqtt_control_name, title)
        # A missing value leaves the value topic alone: publishing None deletes the retained
        # message, which only remove_control means to do.
        publish_value = value is not None and (control.value != value or _publishes_every_update(control))
        publish_error = control.error != error
        topic = self._get_control_base_topic(mqtt_control_name)
        if publish_error and not error:
            if await self._publish(f"{topic}/meta/error", None):
                control.error = error
        if publish_value:
            if await self._publish(topic, value, retain=value_is_retained(control.meta)):
                control.value = value
        if publish_error and error:
            if await self._publish(f"{topic}/meta/error", error.to_mqtt()):
                control.error = error

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
            title_obj = as_translated_title(title)
            if control.meta.title != title_obj:
                control.meta.title = title_obj
                await self._publish_control_meta(mqtt_control_name, control.meta)
        else:
            logging.debug("Can't set title of undeclared control %s", mqtt_control_name)

    async def set_control_error(self, mqtt_control_name: str, error: ControlError) -> None:
        if mqtt_control_name in self._controls:
            control = self._controls[mqtt_control_name]
            if control.error != error:
                error_topic = self._get_control_base_topic(mqtt_control_name) + "/meta/error"
                if await self._publish(error_topic, error.to_mqtt() or None):
                    control.error = error
        else:
            logging.debug("Can't set error of undeclared control %s", mqtt_control_name)

    async def set_device_title(self, title: Optional[Union[str, TranslatedTitle]]) -> None:
        title_obj = as_translated_title(title)
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

    async def _publish(self, topic: str, value: Optional[str], retain: bool = True) -> bool:
        """Publish ``value`` on ``topic``; ``False`` means the broker never confirmed it.

        A timed-out confirmation is logged here and nowhere else: the message stays queued in
        paho and may still arrive, so the caller can only republish on the next update.
        """
        if value is None:
            logging.debug('Clear "%s"', topic)
        else:
            logging.debug('Publish "%s" "%s"', topic, value)
        try:
            await self._mqtt_dispatcher.publish(topic, value, qos=2, retain=retain)
        except aiomqtt.MqttError as exc:
            logging.debug('Not published "%s": %s', topic, exc)
            return False
        return True


async def retain_hack(mqtt_dispatcher: MQTTDispatcher, timeout: float = 120.0) -> None:
    random.seed()
    retain_hack_topic = f"/wbretainhack/{random.random()*10000000:.0f}"

    event = asyncio.Event()

    def on_retain_hack(_message):
        event.set()

    await mqtt_dispatcher.subscribe(retain_hack_topic, on_retain_hack)
    try:
        await mqtt_dispatcher.publish(retain_hack_topic, "2", qos=2)
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

    def collect_devices(message):
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
    try:
        await retain_hack(mqtt_dispatcher, timeout)
        await asyncio.sleep(0.05)
    finally:
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
        await mqtt_dispatcher.publish(topic, None, retain=True)


def make_mqtt_client(broker_url: str) -> aiomqtt.Client:
    urlparse_result = urlparse(broker_url)
    transport = "websockets" if urlparse_result.scheme == "ws" else urlparse_result.scheme
    if transport not in {"tcp", "websockets", "unix"}:
        raise ValueError(f"Unsupported MQTT URL scheme: {urlparse_result.scheme or '<empty>'}")
    client_id_suffix = "".join(random.sample(string.ascii_letters + string.digits, 8))
    client_kwargs = {
        "identifier": f"wb-mqtt-dali-{client_id_suffix}",
        "keepalive": MQTT_KEEPALIVE_S,
        "logger": logging.getLogger("mqtt_client"),
        "transport": transport,
        "timeout": MQTT_PUBLISH_TIMEOUT_S,
    }
    if urlparse_result.scheme == "unix":
        client_kwargs["hostname"] = urlparse_result.path
    else:
        if urlparse_result.hostname is None:
            raise ValueError("No MQTT hostname specified")
        if urlparse_result.port is None:
            raise ValueError("No MQTT port specified")
        client_kwargs["hostname"] = urlparse_result.hostname
        client_kwargs["port"] = urlparse_result.port

    if urlparse_result.username:
        client_kwargs["username"] = urlparse_result.username
    if urlparse_result.password:
        client_kwargs["password"] = urlparse_result.password
    return aiomqtt.Client(**client_kwargs)
