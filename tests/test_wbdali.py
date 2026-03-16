import asyncio
import json
import logging
import unittest

try:
    from unittest.mock import MagicMock
except ImportError:
    from mock import MagicMock

import paho.mqtt.client as mqtt
from dali.command import Command, Response
from dali.frame import BackwardFrame, ForwardFrame

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver, encode_frame_for_modbus


class MockMqttClient:
    def __init__(self, *args, **kwargs):
        self._client = MagicMock()
        self._client._client_id = "test-wbdali-client"
        self._messages_to_broker = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def subscribe(self, topic: str) -> None:
        pass

    async def publish(self, topic: str, payload: str) -> None:
        await self._messages_to_broker.put((topic, payload))

    async def clear_publishes(self):
        while not self._messages_to_broker.empty():
            await self._messages_to_broker.get()

    async def wait_for_publish(self, topic: str, timeout: float = 1.0) -> str:
        """
        Wait for a message to be published to a specific topic.
        Args:
            topic (str): The topic to wait for a published message on.
            timeout (float, optional): Maximum time in seconds to wait for the message.
                Defaults to 1.0.
        Returns:
            str: The message payload that was published to the specified topic.
        Raises:
            TimeoutError: If no message is published to the specified topic within
                the timeout period.
        """

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
    def __init__(self, sendtwice=False, response_class=None, data=[0x12, 0x34]):
        super().__init__(ForwardFrame(16, data))
        self.sendtwice = sendtwice
        self.response = response_class

    def __str__(self):
        return "_MockCommand"


class MockResponse(Response):
    _expected = True

    def __init__(self, frame):
        super().__init__(frame)
        self.frame = frame
        self.data = frame.as_integer if frame else None


class TestWBDALIDriver(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = WBDALIConfig()
        self.mock_mqtt_client = MockMqttClient()
        self.mock_mqtt_dispatcher = MQTTDispatcher(self.mock_mqtt_client)
        self.mock_logger = MagicMock(spec=logging.Logger)

    async def simulate_timeout_response_from_gateway(self, queue_item_index: int, count: int = 1):
        """Simulate a timeout response from the gateway for a given batch start index."""
        for i in range(count):
            index = queue_item_index + i
            index %= self.config.queue_size
            message = mqtt.MQTTMessage(
                topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_{index}".encode()
            )
            message.payload = str(0).encode()
            await self.mock_mqtt_dispatcher._dispatch_message(message)

    async def prepare_driver(self, driver: WBDALIDriver):
        await driver.initialize()
        await self.mock_mqtt_client.clear_publishes()
        if driver.batch_start_index != 0:
            batch_start_index = driver.batch_start_index
            cmds = [_MockCommand() for _ in range(self.config.queue_size - driver.batch_start_index)]
            fut = asyncio.gather(driver.send_commands(cmds))
            await self.mock_mqtt_client.wait_for_publish(
                topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
            )
            await self.simulate_timeout_response_from_gateway(batch_start_index, len(cmds))
            await fut

    def test_encode_frame_for_modbus(self):
        """Test encoding of DALI frames into Modbus frames."""
        frame_16 = MagicMock()
        frame_16.__len__.return_value = 16
        frame_16.as_integer = 0x1234

        frame_24 = MagicMock()
        frame_24.__len__.return_value = 24
        frame_24.as_integer = 0x123456

        frame_25 = MagicMock()
        frame_25.__len__.return_value = 25
        frame_25.as_integer = 0x1234567

        frame_invalid = MagicMock()
        frame_invalid.__len__.return_value = 32

        # Test FF16 frame: priority=4 (bits 31..29), sendtwice=0 (bit 28), size=0 (bits 27..25), data=0x1234 (bits 24..0)
        # Result: 0x80000000 | 0x00000000 | 0x00000000 | 0x00001234 = 0x80001234
        assert encode_frame_for_modbus(frame_16) == 0x80001234  # pylint: disable=W0212

        # Test FF24 frame: priority=4, sendtwice=0, size=1 (bits 27..25), data=0x123456
        # Result: 0x80000000 | 0x00000000 | 0x02000000 | 0x00123456 = 0x82123456
        assert encode_frame_for_modbus(frame_24) == 0x82123456  # pylint: disable=W0212

        # Test FF25 frame: priority=4, sendtwice=0, size=2 (bits 27..25), data=0x1234567
        # Result: 0x80000000 | 0x00000000 | 0x04000000 | 0x01234567 = 0x85234567
        assert encode_frame_for_modbus(frame_25) == 0x85234567  # pylint: disable=W0212

        # Test with sendtwice=True: bit 28 should be set
        # Result: 0x80000000 | 0x10000000 | 0x00000000 | 0x00001234 = 0x90001234
        assert encode_frame_for_modbus(frame_16, sendtwice=True) == 0x90001234  # pylint: disable=W0212

        # Test with priority=3: bits 31..29 = 0b011 = 0x60000000
        # Result: 0x60000000 | 0x00000000 | 0x00000000 | 0x00001234 = 0x60001234
        assert encode_frame_for_modbus(frame_16, priority=3) == 0x60001234  # pylint: disable=W0212

        # Test with sendtwice=True and priority=5
        # Result: 0xA0000000 | 0x10000000 | 0x00000000 | 0x00001234 = 0xB0001234
        assert (
            encode_frame_for_modbus(frame_16, sendtwice=True, priority=5) == 0xB0001234
        )  # pylint: disable=W0212

        # Test invalid frame length
        with self.assertRaises(ValueError):
            encode_frame_for_modbus(frame_invalid)  # pylint: disable=W0212

        # Test invalid priority
        with self.assertRaises(ValueError):
            encode_frame_for_modbus(frame_16, priority=6)  # pylint: disable=W0212

    async def test_send_command_without_response(self):
        """
        Test sending a command that does not expect a response.
        The test simulates getting a response from gateway with "No response" flag.
        """
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)
        cmd = _MockCommand(sendtwice=False, response_class=None)
        fut = asyncio.gather(driver.send(cmd))
        await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )
        await self.simulate_timeout_response_from_gateway(0)
        result = (await fut)[0]
        self.assertIsNone(result)

    async def test_send_command_with_response(self):
        """Test sending a command that expects a response."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)

        cmd = _MockCommand(sendtwice=False, response_class=MockResponse)

        fut = asyncio.gather(driver.send(cmd))
        await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )
        message = mqtt.MQTTMessage(
            topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_0".encode()
        )
        message.payload = str(0x0156).encode()
        await self.mock_mqtt_dispatcher._dispatch_message(message)
        result = (await fut)[0]
        self.assertIsInstance(result, MockResponse)
        self.assertEqual(result.data, 0x56)

    async def test_send_command_sendtwice_without_response(self):
        """Test sending a command with sendtwice=True and no response."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)

        cmd = _MockCommand(sendtwice=True, response_class=None)

        fut = asyncio.gather(driver.send(cmd))
        payload = await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )

        payload_data = json.loads(payload)
        self.assertEqual(payload_data["params"]["count"], 2)
        self.assertEqual(payload_data["params"]["msg"], "12349000")

        await self.simulate_timeout_response_from_gateway(0, 2)

        result = (await fut)[0]
        self.assertIsNone(result)

    async def test_send_single_command_rpc_request(self):
        """Test adding a single command to the send buffer."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)
        cmd = _MockCommand(sendtwice=False, response_class=None)
        fut = asyncio.gather(driver.send(cmd))
        payload = await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )
        await self.simulate_timeout_response_from_gateway(0)
        await fut
        payload_data = json.loads(payload)
        self.assertEqual(payload_data["id"], driver.rpc_id_counter)
        self.assertEqual(payload_data["params"]["device_id"], driver.config.device_name)
        self.assertEqual(payload_data["params"]["function"], 16)
        self.assertEqual(payload_data["params"]["address"], self.config.queue_start_modbus_address)
        self.assertEqual(payload_data["params"]["count"], 2)
        self.assertEqual(payload_data["params"]["msg"], "12348000")
        self.assertEqual(payload_data["params"]["format"], "HEX")
        self.assertEqual(payload_data["params"]["frame_timeout"], 0)

    async def test_send_commands(self):
        """Test adding multiple consecutive commands to the send buffer."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)
        cmds = [
            _MockCommand(sendtwice=False, data=[0x12, 0x34], response_class=MockResponse),
            _MockCommand(sendtwice=False, data=[0x56, 0x78], response_class=MockResponse),
            _MockCommand(sendtwice=False, data=[0x9A, 0xBC], response_class=None),
        ]
        fut = asyncio.gather(driver.send_commands(cmds))
        payload = await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )
        payload_data = json.loads(payload)
        self.assertEqual(payload_data["params"]["count"], 6)
        self.assertEqual(payload_data["params"]["msg"], "12348000567880009abc8000")

        # Simulate responses
        for i in range(3):
            message = mqtt.MQTTMessage(
                topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_{i}".encode()
            )
            if i < 2:
                message.payload = str(0x0150 + i).encode()
            else:
                message.payload = str(0).encode()
            await self.mock_mqtt_dispatcher._dispatch_message(message)

        results = (await fut)[0]
        self.assertEqual(len(results), 3)
        self.assertIsInstance(results[0], MockResponse)
        self.assertEqual(results[0].data, 0x50)
        self.assertIsInstance(results[1], MockResponse)
        self.assertEqual(results[1].data, 0x51)
        self.assertIsNone(results[2])

    async def test_batch_independent_commands(self):
        """Test adding multiple independent commands to the send buffer."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)
        cmds = [
            driver.send(_MockCommand(data=[0x12, 0x34])),
            driver.send(_MockCommand(data=[0x56, 0x78])),
            driver.send(_MockCommand(data=[0x9A, 0xBC])),
        ]
        fut = asyncio.gather(*cmds)
        payload = await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )
        await self.simulate_timeout_response_from_gateway(0, len(cmds))
        await fut
        payload_data = json.loads(payload)
        self.assertEqual(payload_data["params"]["count"], 6)
        self.assertEqual(payload_data["params"]["msg"], "12348000567880009abc8000")

    async def test_send_modbus_rpc_increments_counter(self):
        """Test that rpc_id_counter increments correctly with multiple calls."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)
        rpc_id = driver.rpc_id_counter
        cmd = _MockCommand(sendtwice=False, response_class=None)
        for i in range(3):
            fut = asyncio.gather(driver.send(cmd))
            await self.mock_mqtt_client.wait_for_publish(
                topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
            )
            await self.simulate_timeout_response_from_gateway(i)
            await fut
            self.assertEqual(driver.rpc_id_counter, rpc_id + i + 1)

    async def test_send_command_timeout(self):
        """Test that a command times out if no response is received."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await driver.initialize()
        await self.mock_mqtt_client.clear_publishes()

        cmd = _MockCommand(sendtwice=False, response_class=MockResponse)

        # Don't send any response, let it timeout
        result = await driver.send(cmd)
        self.assertIsNotNone(result)
        self.assertIsNone(result.raw_value)

    async def test_handle_reply_message_with_framing_error(self):
        """Test handling a reply message with a framing error."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)

        cmd = _MockCommand(sendtwice=False, response_class=MockResponse)

        fut = asyncio.gather(driver.send(cmd))
        await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )

        # Simulate framing error
        message = mqtt.MQTTMessage(
            topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_0".encode()
        )
        message.payload = str(0x0300).encode()
        await self.mock_mqtt_dispatcher._dispatch_message(message)

        result = (await fut)[0]
        self.assertIsNotNone(result)
        self.assertTrue(result.raw_value.error)

    async def test_bus_traffic_callbacks(self):
        """Test that bus traffic callbacks are invoked correctly."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)

        callback_invoked = asyncio.Event()
        received_frame = None
        received_source = None

        def traffic_callback(frame, source, _frame_counter):
            nonlocal received_frame, received_source
            received_frame = frame
            received_source = source
            callback_invoked.set()

        driver.bus_traffic.register(traffic_callback)

        # Monitor commands
        cmd = _MockCommand(sendtwice=False, response_class=None)
        fut = asyncio.gather(driver.send(cmd, source="test_source"))
        await asyncio.wait_for(callback_invoked.wait(), timeout=1.0)
        self.assertEqual(received_source, "test_source")
        self.assertEqual(cmd.frame, received_frame)

        await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )

        # Reset for response monitoring
        callback_invoked.clear()
        received_frame = None
        received_source = None

        # Monitor response
        message = mqtt.MQTTMessage(
            topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_0".encode()
        )
        message.payload = str(0x0112).encode()
        await self.mock_mqtt_dispatcher._dispatch_message(message)
        await fut
        await asyncio.wait_for(callback_invoked.wait(), timeout=1.0)
        self.assertEqual(received_source, "bus")
        self.assertEqual(BackwardFrame(0x12), received_frame)

    async def test_handle_ff24_message(self):
        """Test handling of 24-bit forward frame messages."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await driver.initialize()

        callback_invoked = asyncio.Event()

        def traffic_callback(frame, source, _frame_counter):
            if source == "bus":
                callback_invoked.set()

        driver.bus_traffic.register(traffic_callback)

        message = mqtt.MQTTMessage(
            topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_monitor_sporadic_frame".encode()
        )
        message.payload = str(0x1A900180088863B).encode()
        await self.mock_mqtt_dispatcher._dispatch_message(message)

        await asyncio.wait_for(callback_invoked.wait(), timeout=1.0)

    async def test_queue_overflow(self):
        """Test behavior when queue reaches maximum size."""
        self.config.queue_size = 2
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger)
        await self.prepare_driver(driver)

        commands = [
            _MockCommand(),
            _MockCommand(data=[0x56, 0x78]),
            _MockCommand(data=[0x9A, 0xBC]),
        ]

        fut = asyncio.gather(driver.send_commands(commands))

        payload = await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )

        payload_data = json.loads(payload)
        self.assertEqual(payload_data["params"]["count"], 4)
        self.assertEqual(payload_data["params"]["msg"], "1234800056788000")
        self.assertEqual(driver.batch_start_index, 0)

        await self.simulate_timeout_response_from_gateway(0, 2)

        payload = await self.mock_mqtt_client.wait_for_publish(
            topic=f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        )

        payload_data = json.loads(payload)
        self.assertEqual(payload_data["params"]["count"], 2)
        self.assertEqual(payload_data["params"]["msg"], "9abc8000")

        await self.simulate_timeout_response_from_gateway(2)

        await fut
