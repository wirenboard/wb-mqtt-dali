import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set

import aiomqtt
from paho.mqtt.matcher import MQTTMatcher

MessageCallback = Callable[[aiomqtt.Message], None]


class BrokerDisconnectedError(aiomqtt.MqttError):
    """A non-retained publish while the broker link is down."""


@dataclass(frozen=True)
class _MirroredMessage:
    payload: Optional[str]
    qos: int


class MQTTDispatcher:  # pylint: disable=too-many-instance-attributes
    def __init__(self, client: aiomqtt.Client):
        self.client = client
        self._subscriptions: Dict[str, Set[MessageCallback]] = {}
        # Trie of subscription patterns -> the same callback set stored in
        # `_subscriptions`. Used by `_dispatch_message` to look up matches in
        # O(depth) instead of scanning every subscription.
        self._matcher: MQTTMatcher = MQTTMatcher()
        # Last retained message observed per exact topic, replayed to late subscribers.
        # The broker only delivers retained on the first SUBSCRIBE; without this cache
        # additional callbacks on the same topic miss the initial state.
        self._retained_cache: Dict[str, aiomqtt.Message] = {}
        self._running = False
        self._connected = True
        self._session = 0
        self._retained_mirror: Dict[str, _MirroredMessage] = {}
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def session(self) -> int:
        return self._session

    async def connection_restored(self) -> None:
        async with self._lock:
            for topic in self._subscriptions:
                await self.client.subscribe(topic)
            self._session += 1
            self._connected = True

    def connection_lost(self) -> None:
        self._connected = False
        # A clear made during the outage is never redelivered, so the cache must not outlive the session.
        self._retained_cache.clear()

    async def replay_retained(self) -> None:
        # Order of first appearance: a pair published in one order live can be replayed in the other.
        # Read as it goes: a topic updated during the replay goes out with the new payload or after it.
        for topic in list(self._retained_mirror):
            if not self._connected:
                return
            message = self._retained_mirror[topic]
            await self.client.publish(topic, message.payload, qos=message.qos, retain=True)

    async def publish(
        self, topic: str, payload: Optional[str] = None, qos: int = 0, retain: bool = False
    ) -> None:
        if retain:
            self._mirror_retained(topic, payload, qos)
            if not self._connected:
                return
            try:
                await self.client.publish(topic, payload, qos=qos, retain=True)
            except aiomqtt.MqttError as exc:
                logging.debug("Retained publish to %s left to the replay: %s", topic, exc)
            return
        if not self._connected:
            raise BrokerDisconnectedError(f"Broker disconnected, not publishing to {topic}")
        await self.client.publish(topic, payload, qos=qos, retain=False)

    async def subscribe(self, topic: str, callback: MessageCallback) -> None:
        replay: Optional[aiomqtt.Message] = None
        async with self._lock:
            if topic not in self._subscriptions:
                callbacks: Set[MessageCallback] = {callback}
                self._subscriptions[topic] = callbacks
                self._matcher[topic] = callbacks
                if self._connected:
                    try:
                        await self.client.subscribe(topic)
                    except aiomqtt.MqttError as exc:
                        logging.debug("Subscription to %s deferred: %s", topic, exc)
                    except Exception:
                        del self._subscriptions[topic]
                        del self._matcher[topic]
                        raise
            else:
                self._subscriptions[topic].add(callback)
                replay = self._retained_cache.get(topic)
        if replay is not None:
            try:
                callback(replay)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logging.error("Error replaying retained message for topic %s: %s", topic, e)

    async def unsubscribe(self, topic: str, callback: Optional[MessageCallback] = None) -> None:
        async with self._lock:
            if topic not in self._subscriptions:
                return

            if callback is None:
                del self._subscriptions[topic]
                del self._matcher[topic]
                self._retained_cache.pop(topic, None)
                await self._unsubscribe_client(topic)
            else:
                self._subscriptions[topic].discard(callback)

                if not self._subscriptions[topic]:
                    del self._subscriptions[topic]
                    del self._matcher[topic]
                    self._retained_cache.pop(topic, None)
                    await self._unsubscribe_client(topic)

    async def clear_subscriptions(self) -> None:
        async with self._lock:
            topics = list(self._subscriptions.keys())
            for topic in topics:
                await self._unsubscribe_client(topic)

            self._subscriptions.clear()
            self._matcher = MQTTMatcher()
            self._retained_cache.clear()

    async def run(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        try:
            async for message in self.client.messages:
                start = loop.time()
                self._dispatch_message(message)
                elapsed = loop.time() - start
                if elapsed > 0.1:
                    logging.warning(
                        "Dispatching message on topic %s took %.2f seconds", message.topic, elapsed
                    )
        except Exception as e:
            logging.error(e)
            raise
        finally:
            self._running = False

    def _dispatch_message(self, message: aiomqtt.Message) -> None:
        topic = message.topic.value

        if message.retain:
            if message.payload:
                self._retained_cache[topic] = message
            else:
                self._retained_cache.pop(topic, None)

        callbacks: Set[MessageCallback] = set()
        for cbs in self._matcher.iter_match(topic):
            callbacks.update(cbs)

        for callback in callbacks:
            try:
                callback(message)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logging.error("Error in callback for topic %s: %s", topic, e)

    def _mirror_retained(self, topic: str, payload: Optional[str], qos: int) -> None:
        current = self._retained_mirror.get(topic)
        if payload is not None and (current is None or current.payload is None):
            # Appearing (again): to the end, so the replay keeps the order the state was built in.
            self._retained_mirror.pop(topic, None)
        self._retained_mirror[topic] = _MirroredMessage(payload, qos)

    async def _unsubscribe_client(self, topic: str) -> None:
        if not self._connected:
            return
        try:
            await self.client.unsubscribe(topic)
        except aiomqtt.MqttError as exc:
            logging.debug("Unsubscribe from %s skipped: %s", topic, exc)

    @property
    def is_running(self) -> bool:
        return self._running

    def get_subscribed_topics(self) -> Set[str]:
        return set(self._subscriptions.keys())

    @property
    def client_id(self) -> str:
        client_id = self.client._client._client_id  # pylint: disable=W0212
        return client_id.decode() if isinstance(client_id, bytes) else client_id


def get_str_payload(message: aiomqtt.Message) -> str:
    if message.payload is None:
        return ""
    if isinstance(message.payload, (bytes, bytearray)):
        return message.payload.decode()
    return str(message.payload)
