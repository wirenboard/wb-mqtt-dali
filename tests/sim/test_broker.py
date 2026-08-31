"""The in-process broker: retained replay, wildcards, the aiomqtt-shaped client."""

import asyncio

import pytest

from wb.mqtt_dali.sim.broker import Broker, Client, topic_matches


@pytest.mark.parametrize(
    "pattern, topic, expected",
    [
        ("/devices/wb-dali/controls/x", "/devices/wb-dali/controls/x", True),
        ("/devices/wb-dali/controls/x", "/devices/wb-dali/controls/y", False),
        ("/rpc/v1/wb-mqtt-dali/+/+/+", "/rpc/v1/wb-mqtt-dali/Editor/GetList/cid", True),
        ("/rpc/v1/wb-mqtt-dali/+/+/+", "/rpc/v1/wb-mqtt-dali/Editor/GetList/cid/reply", False),
        ("/devices/#", "/devices/a/b/c", True),
        ("/devices/#", "/devices", True),
        ("+/b", "a/b", True),
        ("+/b", "a/c/b", False),
    ],
)
def test_topic_matching(pattern, topic, expected):
    assert topic_matches(pattern, topic) is expected


async def _drain(client, count, timeout=1.0):
    received = []

    async def reader():
        async for message in client.messages:
            received.append(message)
            if len(received) >= count:
                return

    await asyncio.wait_for(reader(), timeout)
    return received


@pytest.mark.asyncio
async def test_publish_reaches_subscriber():
    broker = Broker()
    client = Client(broker, "reader")
    await client.subscribe("/devices/+/controls/level")

    broker.publish("/devices/lamp/controls/level", "128")
    broker.publish("/devices/lamp/controls/other", "ignored")

    (message,) = await _drain(client, 1)
    assert message.topic.value == "/devices/lamp/controls/level"
    assert message.payload == b"128"
    assert message.retain is False


@pytest.mark.asyncio
async def test_client_receives_its_own_publish():
    broker = Broker()
    client = Client(broker, "loopback")
    await client.subscribe("/rpc/v1/wb-mqtt-dali/+/+/+")

    await client.publish("/rpc/v1/wb-mqtt-dali/Editor/GetList/cid", '{"params":{},"id":1}')

    (message,) = await _drain(client, 1)
    assert message.payload == b'{"params":{},"id":1}'


@pytest.mark.asyncio
async def test_retained_replayed_on_subscribe():
    broker = Broker()
    broker.publish("/wb-dali/bus1/commissioning", '{"status":"idle"}', retain=True)

    client = Client(broker, "late")
    await client.subscribe("/wb-dali/+/commissioning")

    (message,) = await _drain(client, 1)
    assert message.retain is True
    assert message.payload == b'{"status":"idle"}'


@pytest.mark.asyncio
async def test_empty_retained_payload_clears_the_topic():
    broker = Broker()
    broker.publish("/rpc/v1/wb-mqtt-dali/Editor/GetList", "1", retain=True)
    broker.publish("/rpc/v1/wb-mqtt-dali/Editor/GetList", None, retain=True)

    assert broker.retained("/rpc/v1/wb-mqtt-dali/Editor/GetList") is None


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    broker = Broker()
    client = Client(broker, "reader")
    await client.subscribe("/devices/lamp/controls/level")
    await client.unsubscribe("/devices/lamp/controls/level")

    broker.publish("/devices/lamp/controls/level", "128")

    with pytest.raises(asyncio.TimeoutError):
        await _drain(client, 1, timeout=0.05)


@pytest.mark.asyncio
async def test_client_id_matches_mqtt_dispatcher_expectation():
    broker = Broker()
    client = Client(broker, "wb-mqtt-dali-browser")
    # MQTTDispatcher.client_id reads client._client._client_id  # pylint: disable=protected-access  # aiomqtt reads it there
    assert client._client._client_id.decode() == "wb-mqtt-dali-browser"
