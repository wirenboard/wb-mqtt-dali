import asyncio
import logging
from typing import Awaitable, Callable, Dict, Set

import asyncio_mqtt as aiomqtt
import paho.mqtt.client as mqtt

MessageCallback = Callable[[mqtt.MQTTMessage], Awaitable[None]]


class MQTTDispatcher:
    def __init__(self, client: aiomqtt.Client):
        self.client = client
        self._subscriptions: Dict[str, Set[MessageCallback]] = {}
        self._running = False
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str, callback: MessageCallback) -> None:
        async with self._lock:
            if topic not in self._subscriptions:
                self._subscriptions[topic] = set()
                await self.client.subscribe(topic)

            self._subscriptions[topic].add(callback)

    async def unsubscribe(self, topic: str, callback: MessageCallback = None) -> None:
        async with self._lock:
            if topic not in self._subscriptions:
                return

            if callback is None:
                del self._subscriptions[topic]
                await self.client.unsubscribe(topic)
            else:
                self._subscriptions[topic].discard(callback)

                if not self._subscriptions[topic]:
                    del self._subscriptions[topic]
                    await self.client.unsubscribe(topic)

    async def clear_subscriptions(self) -> None:
        async with self._lock:
            topics = list(self._subscriptions.keys())
            for topic in topics:
                await self.client.unsubscribe(topic)

            self._subscriptions.clear()

    async def run(self) -> None:
        self._running = True
        try:
            async with self.client.unfiltered_messages() as messages:
                async for message in messages:
                    await self._dispatch_message(message)
        except Exception as e:
            logging.error(e)
            raise
        finally:
            async with self._lock:
                self._subscriptions.clear()
            self._running = False

    async def _dispatch_message(self, message: mqtt.MQTTMessage) -> None:
        topic = str(message.topic)

        callbacks = set()
        async with self._lock:
            for callback_topic, cbs in self._subscriptions.items():
                if mqtt.topic_matches_sub(callback_topic, topic):
                    callbacks.update(cbs)

        for callback in callbacks:
            try:
                await callback(message)
            except Exception as e:
                logging.error("Error in callback for topic %s: %s", topic, e)

    @property
    def is_running(self) -> bool:
        return self._running

    def get_subscribed_topics(self) -> Set[str]:
        return set(self._subscriptions.keys())

    @property
    def client_id(self) -> str:
        return self.client._client._client_id  # pylint: disable=W0212
