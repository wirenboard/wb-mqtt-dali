"""Retained-message replay for multi-subscriber topics.

The broker only delivers a retained message on a *new* SUBSCRIBE; the dispatcher
deduplicates broker subscribes per topic, so additional callbacks added after the
initial subscription would otherwise miss the retained state. These tests pin the
behaviour required by the gateway-reset feature: every callback on the same topic
sees the latest retained payload regardless of subscribe order.
"""

from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import pytest

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher


class _FakeClient:  # pylint: disable=too-few-public-methods
    def __init__(self):
        self.subscribe = AsyncMock()
        self.unsubscribe = AsyncMock()
        self._client = MagicMock()
        self._client._client_id = "test"


@pytest.mark.asyncio
async def test_retained_replayed_to_late_subscriber():
    """Late subscribers receive the cached retained message immediately."""
    dispatcher = MQTTDispatcher(_FakeClient())

    cb1 = MagicMock()
    await dispatcher.subscribe("topic/x", cb1)

    retained = aiomqtt.Message("topic/x", b"r", 0, True, 0, None)
    dispatcher._dispatch_message(retained)  # pylint: disable=protected-access
    cb1.assert_called_once_with(retained)

    cb2 = MagicMock()
    await dispatcher.subscribe("topic/x", cb2)
    cb2.assert_called_once_with(retained)


@pytest.mark.asyncio
async def test_non_retained_not_replayed():
    """Non-retained messages must not be cached for late subscribers."""
    dispatcher = MQTTDispatcher(_FakeClient())

    cb1 = MagicMock()
    await dispatcher.subscribe("topic/y", cb1)

    msg = aiomqtt.Message("topic/y", b"hello", 0, False, 0, None)
    dispatcher._dispatch_message(msg)  # pylint: disable=protected-access

    cb2 = MagicMock()
    await dispatcher.subscribe("topic/y", cb2)
    cb2.assert_not_called()


@pytest.mark.asyncio
async def test_unsubscribe_clears_retained_cache():
    """Fully unsubscribing drops the cached retained payload so it isn't replayed later."""
    dispatcher = MQTTDispatcher(_FakeClient())

    cb1 = MagicMock()
    await dispatcher.subscribe("topic/z", cb1)
    dispatcher._dispatch_message(  # pylint: disable=protected-access
        aiomqtt.Message("topic/z", b"r", 0, True, 0, None)
    )
    await dispatcher.unsubscribe("topic/z")

    cb2 = MagicMock()
    await dispatcher.subscribe("topic/z", cb2)
    cb2.assert_not_called()


@pytest.mark.asyncio
async def test_empty_retained_evicts_cache():
    """An empty retained payload is MQTT's 'delete retained' — the cache must drop the
    previous payload so a late subscriber after the delete gets no replay."""
    dispatcher = MQTTDispatcher(_FakeClient())

    cb1 = MagicMock()
    await dispatcher.subscribe("topic/w", cb1)

    # First, a real retained payload arrives and is cached.
    retained = aiomqtt.Message("topic/w", b"r", 0, True, 0, None)
    dispatcher._dispatch_message(retained)  # pylint: disable=protected-access
    cb1.assert_called_once_with(retained)

    # Then the broker signals retained-delete via an empty retained message.
    delete = aiomqtt.Message("topic/w", b"", 0, True, 0, None)
    dispatcher._dispatch_message(delete)  # pylint: disable=protected-access

    # A late subscriber that arrives after the delete must not receive the
    # previously-cached payload (and must not see the empty one either).
    cb2 = MagicMock()
    await dispatcher.subscribe("topic/w", cb2)
    cb2.assert_not_called()
