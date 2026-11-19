import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiomqtt

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher

from ._virtual_time import VirtualTimeTestCase


class EmptyAsyncIterator:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class MockClient:
    def __init__(self):
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()
        self._client = MagicMock()
        self._client._client_id = "test-client-id"
        self.messages = EmptyAsyncIterator()

    def set_message_generator(self, generator):
        self.messages = generator

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class TestMQTTDispatcher(VirtualTimeTestCase):
    """Each test gets a fresh dispatcher over a mock client."""

    def setUp(self):
        self.mock_client = MockClient()
        self.dispatcher = MQTTDispatcher(self.mock_client)

    async def test_subscribe_adds_topic(self):
        callback = AsyncMock()

        await self.dispatcher.subscribe("test/topic", callback)

        assert "test/topic" in self.dispatcher._subscriptions  # pylint: disable=W0212
        assert callback in self.dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212
        self.mock_client.subscribe.assert_called_once_with("test/topic")

    async def test_subscribe_multiple_callbacks(self):
        callback1 = AsyncMock()
        callback2 = AsyncMock()

        await self.dispatcher.subscribe("test/topic", callback1)
        await self.dispatcher.subscribe("test/topic", callback2)

        self.mock_client.subscribe.assert_called_once_with("test/topic")

        assert callback1 in self.dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212
        assert callback2 in self.dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212

    async def test_unsubscribe_specific_callback(self):
        callback1 = AsyncMock()
        callback2 = AsyncMock()

        await self.dispatcher.subscribe("test/topic", callback1)
        await self.dispatcher.subscribe("test/topic", callback2)

        await self.dispatcher.unsubscribe("test/topic", callback1)

        assert callback1 not in self.dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212
        assert callback2 in self.dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212

        self.mock_client.unsubscribe.assert_not_called()

    async def test_unsubscribe_all_callbacks(self):
        callback1 = AsyncMock()
        callback2 = AsyncMock()

        await self.dispatcher.subscribe("test/topic", callback1)
        await self.dispatcher.subscribe("test/topic", callback2)

        await self.dispatcher.unsubscribe("test/topic")

        assert "test/topic" not in self.dispatcher._subscriptions  # pylint: disable=W0212
        self.mock_client.unsubscribe.assert_called_once_with("test/topic")

    async def test_clear_subscriptions(self):
        callback1 = AsyncMock()
        callback2 = AsyncMock()

        await self.dispatcher.subscribe("topic1", callback1)
        await self.dispatcher.subscribe("topic2", callback2)

        await self.dispatcher.clear_subscriptions()

        assert len(self.dispatcher._subscriptions) == 0  # pylint: disable=W0212
        assert self.mock_client.unsubscribe.call_count == 2

    async def test_dispatch_message(self):
        callback = MagicMock()
        message = aiomqtt.Message("test/topic", b"test payload", 0, False, 0, None)

        await self.dispatcher.subscribe("test/topic", callback)
        self.dispatcher._dispatch_message(message)  # pylint: disable=W0212

        callback.assert_called_once_with(message)

    async def test_dispatch_message_multiple_callbacks(self):
        callback1 = MagicMock()
        callback2 = MagicMock()
        message = aiomqtt.Message("test/topic", b"test payload", 0, False, 0, None)

        await self.dispatcher.subscribe("test/topic", callback1)
        await self.dispatcher.subscribe("test/topic", callback2)
        self.dispatcher._dispatch_message(message)  # pylint: disable=W0212

        callback1.assert_called_once_with(message)
        callback2.assert_called_once_with(message)

    async def test_dispatch_message_no_handlers(self):
        message = aiomqtt.Message("unknown/topic", b"test payload", 0, False, 0, None)

        self.dispatcher._dispatch_message(message)  # pylint: disable=W0212

    async def test_run_dispatcher(self):
        fut = asyncio.Future()

        original_message = aiomqtt.Message("test/topic", b"test payload", 0, False, 0, None)
        incoming_message = None

        def callback(message):
            nonlocal incoming_message
            incoming_message = message
            fut.set_result(None)

        await self.dispatcher.subscribe("test/topic", callback)

        async def message_generator():
            yield original_message

        self.mock_client.set_message_generator(message_generator())

        asyncio.create_task(self.dispatcher.run())

        await asyncio.sleep(0.1)

        await asyncio.wait_for(fut, timeout=1.0)
        assert incoming_message == original_message

    async def test_get_subscribed_topics(self):
        callback1 = MagicMock()
        callback2 = MagicMock()

        await self.dispatcher.subscribe("topic1", callback1)
        await self.dispatcher.subscribe("topic2", callback2)

        topics = self.dispatcher.get_subscribed_topics()

        assert topics == {"topic1", "topic2"}

    async def test_is_running(self):
        assert not self.dispatcher.is_running

        async def message_generator():
            while True:
                await asyncio.sleep(0.1)
                yield aiomqtt.Message("test/topic", b"test", 0, False, 0, None)

        self.mock_client.set_message_generator(message_generator())

        task = asyncio.create_task(self.dispatcher.run())

        await asyncio.sleep(0.1)

        assert self.dispatcher.is_running

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert not self.dispatcher.is_running

    async def test_concurrent_subscribe_unsubscribe(self):
        callbacks = [AsyncMock() for _ in range(10)]

        await asyncio.gather(*[self.dispatcher.subscribe(f"topic{i}", callbacks[i]) for i in range(10)])

        assert len(self.dispatcher._subscriptions) == 10  # pylint: disable=W0212

        await asyncio.gather(*[self.dispatcher.unsubscribe(f"topic{i}") for i in range(10)])

        assert len(self.dispatcher._subscriptions) == 0  # pylint: disable=W0212

    def test_client_id(self):
        assert self.dispatcher.client_id == "test-client-id"
