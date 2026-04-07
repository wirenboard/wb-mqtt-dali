import asyncio
import logging
from typing import Callable, Dict, Optional, Set

import aiomqtt
import paho.mqtt.client as mqtt
from paho.mqtt.matcher import MQTTMatcher

MessageCallback = Callable[[mqtt.MQTTMessage], None]


class MQTTDispatcher:
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
        self._retained_cache: Dict[str, mqtt.MQTTMessage] = {}
        self._running = False
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str, callback: MessageCallback) -> None:
        replay: Optional[mqtt.MQTTMessage] = None
        async with self._lock:
            if topic not in self._subscriptions:
                callbacks: Set[MessageCallback] = {callback}
                self._subscriptions[topic] = callbacks
                self._matcher[topic] = callbacks
                try:
                    await self.client.subscribe(topic)
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
                await self.client.unsubscribe(topic)
            else:
                self._subscriptions[topic].discard(callback)

                if not self._subscriptions[topic]:
                    del self._subscriptions[topic]
                    del self._matcher[topic]
                    self._retained_cache.pop(topic, None)
                    await self.client.unsubscribe(topic)

    async def clear_subscriptions(self) -> None:
        async with self._lock:
            topics = list(self._subscriptions.keys())
            for topic in topics:
                await self.client.unsubscribe(topic)

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
            async with self._lock:
                self._subscriptions.clear()
                self._matcher = MQTTMatcher()
                self._retained_cache.clear()
                self._running = False

    def _dispatch_message(self, message: mqtt.MQTTMessage) -> None:
        topic = str(message.topic)

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

    @property
    def is_running(self) -> bool:
        return self._running

    def get_subscribed_topics(self) -> Set[str]:
        return set(self._subscriptions.keys())

    @property
    def client_id(self) -> str:
        client_id = self.client._client._client_id  # pylint: disable=W0212
        return client_id.decode() if isinstance(client_id, bytes) else client_id
