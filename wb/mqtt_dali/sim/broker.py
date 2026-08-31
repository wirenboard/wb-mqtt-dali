"""An in-process MQTT broker that speaks enough of the protocol for wb-mqtt-dali.

This is the daemon's own bus, not the DALI one. `Gateway`, `MQTTRPCServer` and
`ApplicationController` are production code that publishes and subscribes for a
living — the `Editor/*` RPC the web UI calls, the commissioning progress topic
it watches, the virtual devices it publishes — and they take an
``aiomqtt.Client`` to do it with. Giving them a loopback client attached to this
broker is what lets them run unmodified in a browser.

DALI traffic does not come through here. Commands go straight to the gateway's
Modbus registers through a register-polling driver.

Semantics that matter to the daemon and are therefore implemented here:

* wildcard subscriptions (``+`` single level, ``#`` multi level trailing),
* retained messages, replayed to a client when it subscribes,
* a publish with an empty payload clears the retained message for that topic,
* a client receives its own publishes, as it would through a real broker.

QoS is accepted and ignored: delivery is a direct call, so it is always exactly
once and always ordered.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, Iterator, List, Optional, Set, Union

Payload = Union[str, bytes, bytearray, int, float, None]


logger = logging.getLogger("wb.mqtt_dali.sim.broker")


class Topic:
    """Stand-in for ``aiomqtt.Topic``; the daemon uses ``.value`` and ``str()``."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"Topic({self.value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Topic):
            return self.value == other.value
        return self.value == other

    def __hash__(self) -> int:
        return hash(self.value)

    def matches(self, pattern: str) -> bool:
        return topic_matches(pattern, self.value)


class Message:  # pylint: disable=too-few-public-methods
    """Stand-in for ``aiomqtt.Message``.

    ``payload`` is always ``bytes`` — that is what paho delivers and what
    ``get_str_payload`` / ``get_int_payload`` in the daemon are written against.
    """

    __slots__ = ("topic", "payload", "qos", "retain", "mid", "properties")

    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
        mid: int = 0,
    ) -> None:
        self.topic = Topic(topic)
        self.payload = payload
        self.qos = qos
        self.retain = retain
        self.mid = mid
        self.properties = None

    def __repr__(self) -> str:
        return f"Message({self.topic.value!r}, {self.payload!r}, retain={self.retain})"


def encode_payload(payload: Payload) -> bytes:
    if payload is None:
        return b""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode()
    return str(payload).encode()


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT topic filter matching, per MQTT 3.1.1 §4.7."""
    return _compiled_filter(pattern).match(topic) is not None


_filter_cache: Dict[str, "re.Pattern[str]"] = {}


def _compiled_filter(pattern: str) -> "re.Pattern[str]":
    compiled = _filter_cache.get(pattern)
    if compiled is None:
        compiled = re.compile(_filter_to_regex(pattern))
        _filter_cache[pattern] = compiled
    return compiled


def _filter_to_regex(pattern: str) -> str:
    parts = []
    levels = pattern.split("/")
    for index, level in enumerate(levels):
        last = index == len(levels) - 1
        if level == "#":
            # '#' matches this level and everything below, including the parent
            # topic itself: filter 'a/#' matches 'a'.
            parts.append("(/.*)?" if parts else ".*")
            return "^" + "".join(parts) + "$"
        if index:
            parts.append("/")
        parts.append("[^/]*" if level == "+" else re.escape(level))
        if last:
            break
    return "^" + "".join(parts) + "$"


class Broker:
    """The message bus every participant attaches to."""

    def __init__(self) -> None:
        self._clients: List["Client"] = []
        self._retained: Dict[str, Message] = {}
        self.published_count = 0

    def attach(self, client: "Client") -> None:
        self._clients.append(client)

    def detach(self, client: "Client") -> None:
        if client in self._clients:
            self._clients.remove(client)

    def publish(self, topic: str, payload: Payload = None, qos: int = 0, retain: bool = False) -> None:
        data = encode_payload(payload)
        self.published_count += 1

        if retain:
            if data:
                self._retained[topic] = Message(topic, data, qos, retain=True)
            else:
                self._retained.pop(topic, None)

        for client in list(self._clients):
            if client.is_subscribed(topic):
                client.deliver(Message(topic, data, qos, retain=False))

    def retained_matching(self, pattern: str) -> Iterator[Message]:
        for topic, message in list(self._retained.items()):
            if topic_matches(pattern, topic):
                yield message

    def retained(self, topic: str) -> Optional[Message]:
        return self._retained.get(topic)


class _PahoStub:  # pylint: disable=too-few-public-methods
    """Satisfies ``MQTTDispatcher.client_id``, which reads ``client._client._client_id``."""

    def __init__(self, client_id: str) -> None:
        self._client_id = client_id.encode()


class Client:
    """Stand-in for ``aiomqtt.Client``, attached to a :class:`Broker`.

    Only the surface the daemon uses is implemented: the async context manager,
    ``subscribe`` / ``unsubscribe`` / ``publish``, and the ``messages`` async
    iterator that :meth:`MQTTDispatcher.run` consumes.
    """

    def __init__(self, broker: Broker, client_id: str = "wb-mqtt-dali-browser") -> None:
        self.broker = broker
        self._client = _PahoStub(client_id)
        self._filters: Set[str] = set()
        self._inbox: "asyncio.Queue[Message]" = asyncio.Queue()
        self._connected = False
        broker.attach(self)

    # -- connection ------------------------------------------------------

    async def __aenter__(self) -> "Client":
        self._connected = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._connected = False
        self.broker.detach(self)

    # -- subscription ----------------------------------------------------

    def is_subscribed(self, topic: str) -> bool:
        return any(topic_matches(pattern, topic) for pattern in self._filters)

    def add_filter(self, topic: str) -> None:
        """Start matching a filter now, without waiting for the event loop.

        `Broker.publish` consults `is_subscribed` as it delivers, so a caller
        that subscribes and publishes in the same tick would otherwise miss its
        own message.
        """
        if topic in self._filters:
            return
        self._filters.add(topic)
        # A broker sends matching retained messages on SUBSCRIBE.
        for message in self.broker.retained_matching(topic):
            self.deliver(Message(message.topic.value, message.payload, message.qos, retain=True))

    def remove_filter(self, topic: str) -> None:
        self._filters.discard(topic)

    async def subscribe(self, topic: str, _qos: int = 0, **_kwargs) -> None:
        self.add_filter(topic)

    async def unsubscribe(self, topic: str, **_kwargs) -> None:
        self.remove_filter(topic)

    # -- traffic ---------------------------------------------------------

    async def publish(
        self,
        topic: str,
        payload: Payload = None,
        qos: int = 0,
        retain: bool = False,
        **_kwargs,
    ) -> None:
        self.broker.publish(topic, payload, qos, retain)

    def deliver(self, message: Message) -> None:
        self._inbox.put_nowait(message)

    @property
    def messages(self) -> "_MessageIterator":
        return _MessageIterator(self._inbox)


class _MessageIterator:
    """``async for message in client.messages`` — see ``MQTTDispatcher.run``."""

    def __init__(self, queue: "asyncio.Queue[Message]") -> None:
        self._queue = queue

    def __aiter__(self) -> "_MessageIterator":
        return self

    async def __anext__(self) -> Message:
        return await self._queue.get()


class MqttError(Exception):
    """Stand-in for ``aiomqtt.MqttError``; never raised by the loopback client."""
