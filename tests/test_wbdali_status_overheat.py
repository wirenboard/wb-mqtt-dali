import asyncio
import json
import logging
import unittest

try:
    from unittest.mock import MagicMock
except ImportError:
    from mock import MagicMock

import paho.mqtt.client as mqtt
from dali.command import Command
from dali.frame import ForwardFrame

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import Overheat

# pylint: disable=duplicate-code


class MockMqttClient:
    def __init__(self):
        self._client = MagicMock()
        self._client._client_id = "test-wbdali-status-overheat-client"
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


class TestWBDALIStatusOverheat(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = WBDALIConfig()
        self.mock_mqtt_client = MockMqttClient()
        self.mock_mqtt_dispatcher = MQTTDispatcher(self.mock_mqtt_client)
        self.mock_logger = MagicMock(spec=logging.Logger)

    async def test_status_5_returns_overheat(self):
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await driver.initialize()
        await self.mock_mqtt_client.clear_publishes()

        fut = asyncio.create_task(driver.send(_MockCommand()))
        payload = await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )
        payload_data = json.loads(payload)
        self.assertEqual(payload_data["params"]["count"], 2)

        message = mqtt.MQTTMessage(
            topic=(
                f"/devices/{self.config.device_name}/controls/" f"bus_{self.config.bus}_bulk_send_reply_0"
            ).encode()
        )
        message.payload = str(0x0500).encode()
        self.mock_mqtt_dispatcher._dispatch_message(message)  # pylint: disable=protected-access

        result = await fut
        self.assertIsInstance(result, Overheat)
