import asyncio
import logging
import unittest

try:
    from unittest.mock import MagicMock
except ImportError:
    from mock import MagicMock

import aiomqtt
from dali.command import Command
from dali.frame import ForwardFrame

from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import FramePriority, WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import Overheat

from ._fake_rpc_client import FakePortLoadClient

# pylint: disable=duplicate-code


class MockMqttClient:
    def __init__(self):
        self._client = MagicMock()
        self._client._client_id = "test-wbdali-status-overheat-client"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def subscribe(self, topic: str) -> None:
        del topic

    async def unsubscribe(self, topic: str) -> None:
        del topic


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
        rpc = FakePortLoadClient()
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, rpc)
        await driver.initialize()
        rpc.clear()

        fut = asyncio.create_task(
            driver.send(_MockCommand(), BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger)
        )
        params = await rpc.wait_for_write()
        self.assertEqual(params["count"], 2)

        message = aiomqtt.Message(
            topic=(
                f"/devices/{self.config.device_name}/controls/" f"bus_{self.config.bus}_bulk_send_reply_0"
            ),
            payload=str(0x0500),
            qos=0,
            retain=False,
            mid=0,
            properties=None,
        )
        self.mock_mqtt_dispatcher._dispatch_message(message)  # pylint: disable=protected-access

        result = await fut
        self.assertIsInstance(result, Overheat)
