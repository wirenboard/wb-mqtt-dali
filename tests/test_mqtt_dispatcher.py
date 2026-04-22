import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher

# pylint: disable=redefined-outer-name


class MockMessage:  # pylint: disable=R0903
    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


class MockClient:
    def __init__(self):
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()
        self._message_generator = None
        self._client = MagicMock()
        self._client._client_id = "test-client-id"

    def set_message_generator(self, generator):
        self._message_generator = generator

    @asynccontextmanager
    async def unfiltered_messages(self) -> AsyncIterator:
        if self._message_generator is None:

            async def empty_generator():
                yield

            yield empty_generator()
        else:
            yield self._message_generator

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_client():
    return MockClient()


@pytest.fixture
def dispatcher(mock_client):
    return MQTTDispatcher(mock_client)


@pytest.mark.asyncio
async def test_subscribe_adds_topic(dispatcher, mock_client):
    callback = AsyncMock()

    await dispatcher.subscribe("test/topic", callback)

    assert "test/topic" in dispatcher._subscriptions  # pylint: disable=W0212
    assert callback in dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212
    mock_client.subscribe.assert_called_once_with("test/topic")


@pytest.mark.asyncio
async def test_subscribe_multiple_callbacks(dispatcher, mock_client):
    callback1 = AsyncMock()
    callback2 = AsyncMock()

    await dispatcher.subscribe("test/topic", callback1)
    await dispatcher.subscribe("test/topic", callback2)

    mock_client.subscribe.assert_called_once_with("test/topic")

    assert callback1 in dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212
    assert callback2 in dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212


@pytest.mark.asyncio
async def test_unsubscribe_specific_callback(dispatcher, mock_client):
    callback1 = AsyncMock()
    callback2 = AsyncMock()

    await dispatcher.subscribe("test/topic", callback1)
    await dispatcher.subscribe("test/topic", callback2)

    await dispatcher.unsubscribe("test/topic", callback1)

    assert callback1 not in dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212
    assert callback2 in dispatcher._subscriptions["test/topic"]  # pylint: disable=W0212

    mock_client.unsubscribe.assert_not_called()


@pytest.mark.asyncio
async def test_unsubscribe_all_callbacks(dispatcher, mock_client):
    callback1 = AsyncMock()
    callback2 = AsyncMock()

    await dispatcher.subscribe("test/topic", callback1)
    await dispatcher.subscribe("test/topic", callback2)

    await dispatcher.unsubscribe("test/topic")

    assert "test/topic" not in dispatcher._subscriptions  # pylint: disable=W0212
    mock_client.unsubscribe.assert_called_once_with("test/topic")


@pytest.mark.asyncio
async def test_clear_subscriptions(dispatcher, mock_client):
    callback1 = AsyncMock()
    callback2 = AsyncMock()

    await dispatcher.subscribe("topic1", callback1)
    await dispatcher.subscribe("topic2", callback2)

    await dispatcher.clear_subscriptions()

    assert len(dispatcher._subscriptions) == 0  # pylint: disable=W0212
    assert mock_client.unsubscribe.call_count == 2


@pytest.mark.asyncio
async def test_dispatch_message(dispatcher):
    callback = MagicMock()
    message = MockMessage("test/topic", b"test payload")

    await dispatcher.subscribe("test/topic", callback)
    dispatcher._dispatch_message(message)  # pylint: disable=W0212

    callback.assert_called_once_with(message)


@pytest.mark.asyncio
async def test_dispatch_message_multiple_callbacks(dispatcher):
    callback1 = MagicMock()
    callback2 = MagicMock()
    message = MockMessage("test/topic", b"test payload")

    await dispatcher.subscribe("test/topic", callback1)
    await dispatcher.subscribe("test/topic", callback2)
    dispatcher._dispatch_message(message)  # pylint: disable=W0212

    callback1.assert_called_once_with(message)
    callback2.assert_called_once_with(message)


@pytest.mark.asyncio
async def test_dispatch_message_no_handlers(dispatcher):
    message = MockMessage("unknown/topic", b"test payload")

    dispatcher._dispatch_message(message)  # pylint: disable=W0212


@pytest.mark.asyncio
async def test_run_dispatcher(dispatcher, mock_client):
    fut = asyncio.Future()

    original_message = MockMessage("test/topic", b"test payload")
    incoming_message = None

    def callback(message):
        nonlocal incoming_message
        incoming_message = message
        fut.set_result(None)

    await dispatcher.subscribe("test/topic", callback)

    async def message_generator():
        yield original_message

    mock_client.set_message_generator(message_generator())

    asyncio.create_task(dispatcher.run())

    await asyncio.sleep(0.1)

    await asyncio.wait_for(fut, timeout=1.0)
    assert incoming_message == original_message


@pytest.mark.asyncio
async def test_get_subscribed_topics(dispatcher):
    callback1 = MagicMock()
    callback2 = MagicMock()

    await dispatcher.subscribe("topic1", callback1)
    await dispatcher.subscribe("topic2", callback2)

    topics = dispatcher.get_subscribed_topics()

    assert topics == {"topic1", "topic2"}


@pytest.mark.asyncio
async def test_is_running(dispatcher, mock_client):
    assert not dispatcher.is_running

    async def message_generator():
        while True:
            await asyncio.sleep(0.1)
            yield MockMessage("test/topic", b"test")

    mock_client.set_message_generator(message_generator())

    task = asyncio.create_task(dispatcher.run())

    await asyncio.sleep(0.1)

    assert dispatcher.is_running

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not dispatcher.is_running


@pytest.mark.asyncio
async def test_concurrent_subscribe_unsubscribe(dispatcher):
    callbacks = [AsyncMock() for _ in range(10)]

    await asyncio.gather(*[dispatcher.subscribe(f"topic{i}", callbacks[i]) for i in range(10)])

    assert len(dispatcher._subscriptions) == 10  # pylint: disable=W0212

    await asyncio.gather(*[dispatcher.unsubscribe(f"topic{i}") for i in range(10)])

    assert len(dispatcher._subscriptions) == 0  # pylint: disable=W0212


def test_client_id(dispatcher):
    assert dispatcher.client_id == "test-client-id"
