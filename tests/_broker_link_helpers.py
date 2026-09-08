"""Doubles for the aiomqtt client under a real MQTTDispatcher: a recording client and a feedable inbox."""

import asyncio
from typing import Dict, List, NamedTuple, Optional, Tuple
from unittest.mock import MagicMock

import aiomqtt


class Publish(NamedTuple):
    topic: str
    payload: Optional[str]
    qos: int
    retain: bool


class Inbox:
    """The client's message stream: delivers what the test puts in, fails when told to."""

    def __init__(self, messages=()):
        self._queue: asyncio.Queue = asyncio.Queue()
        for message in messages:
            self.deliver(message)

    def deliver(self, message: aiomqtt.Message) -> None:
        self._queue.put_nowait(message)

    def fail(self) -> None:
        self._queue.put_nowait(aiomqtt.MqttError("Disconnected during message iteration"))

    def pending(self) -> int:
        return self._queue.qsize()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if isinstance(item, Exception):
            raise item
        return item


class RecordingClient:
    """Records every call in order; a publish waits for `ack` when set; `fail()` makes a call raise."""

    def __init__(self):
        self._client = MagicMock()
        self._client._client_id = "test-broker-link-client"
        self.calls: List[Tuple[str, str]] = []
        self.publishes: List[Publish] = []
        self.messages = Inbox()
        self.ack: Optional[asyncio.Event] = None
        self.failures: Dict[Tuple[str, str], Exception] = {}

    def fail(self, call: str, topic: str, error: Optional[Exception] = None) -> None:
        self.failures[(call, topic)] = error or aiomqtt.MqttError("The client is not currently connected")

    async def subscribe(self, topic: str) -> None:
        self._record("subscribe", topic)

    async def unsubscribe(self, topic: str) -> None:
        self._record("unsubscribe", topic)

    async def publish(
        self, topic: str, payload: Optional[str] = None, qos: int = 0, retain: bool = False
    ) -> None:
        self._record("publish", topic)
        self.publishes.append(Publish(topic, payload, qos, retain))
        if self.ack is not None:
            await self.ack.wait()

    def clear(self) -> None:
        self.calls.clear()
        self.publishes.clear()

    def drain_publishes(self) -> List[Publish]:
        drained, self.publishes = self.publishes, []
        return drained

    async def wait_for_publish(self, topic: str) -> Optional[str]:
        """The payload of the next publish to `topic`; it and everything before it are dropped."""
        for _ in range(100):
            for position, publish in enumerate(self.publishes):
                if publish.topic == topic:
                    del self.publishes[: position + 1]
                    return publish.payload
            await asyncio.sleep(0.01)
        raise TimeoutError(f"No publish to {topic}")

    def _record(self, call: str, topic: str) -> None:
        error = self.failures.get((call, topic))
        if error is not None:
            raise error
        self.calls.append((call, topic))
