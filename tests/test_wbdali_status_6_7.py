import asyncio
import logging
import unittest

try:
    from unittest.mock import MagicMock
except ImportError:
    from mock import MagicMock

import paho.mqtt.client as mqtt
from dali.command import Command, Response
from dali.frame import ForwardFrame

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import TransmissionFailed

# pylint: disable=duplicate-code


class MockMqttClient:
    def __init__(self):
        self._client = MagicMock()
        self._client._client_id = "test-wbdali-status-6-7-client"
        self._messages_to_broker = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def subscribe(self, topic: str) -> None:
        del topic

    async def publish(self, topic: str, payload: str) -> None:
        await self._messages_to_broker.put((topic, payload))

    async def clear_publishes(self):
        while not self._messages_to_broker.empty():
            await self._messages_to_broker.get()

    async def wait_for_publish(self, topic: str, timeout: float = 1.0) -> str:
        while True:
            try:
                published_topic, message = await asyncio.wait_for(
                    self._messages_to_broker.get(), timeout=timeout
                )
                if published_topic == topic:
                    return message
            except asyncio.TimeoutError:
                raise TimeoutError(f"Timeout waiting for publish to topic: {topic}") from None


class _MockCommand(Command):
    def __init__(self):
        super().__init__(ForwardFrame(16, [0x12, 0x34]))
        self.sendtwice = False
        self.response = None


class TestWBDALIStatus6And7(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = WBDALIConfig()
        self.mock_mqtt_client = MockMqttClient()
        self.mock_mqtt_dispatcher = MQTTDispatcher(self.mock_mqtt_client)
        self.mock_logger = MagicMock(spec=logging.Logger)

    async def test_status_6_is_a_broken_response(self):
        """A legacy status 6 reply: the command gets a response with a framing error, as for status 3."""
        result = await self._exchange(0x0612)

        self.assertTrue(result.raw_value.error)

    async def test_status_7_returns_transmission_failed(self):
        """A status 7 reply: the command gets TransmissionFailed."""
        result = await self._exchange(0x0700)

        self.assertIsInstance(result, TransmissionFailed)

    async def _exchange(self, status_word: int) -> Response:
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await driver.initialize()
        await self.mock_mqtt_client.clear_publishes()

        fut = asyncio.create_task(driver.send(_MockCommand()))
        await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )

        message = mqtt.MQTTMessage(
            topic=(
                f"/devices/{self.config.device_name}/controls/" f"bus_{self.config.bus}_bulk_send_reply_0"
            ).encode()
        )
        message.payload = str(status_word).encode()
        self.mock_mqtt_dispatcher._dispatch_message(message)  # pylint: disable=protected-access

        return await fut
