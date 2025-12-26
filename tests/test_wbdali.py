import asyncio
import json
import unittest

try:
    from unittest.mock import AsyncMock, MagicMock, patch
except ImportError:
    from mock import AsyncMock, MagicMock, patch

from dali.command import Command, Response
from dali.frame import BackwardFrame, ForwardFrame

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver


class MockMqttClient:
    def __init__(self, *args, **kwargs):
        self.publish = AsyncMock()
        self._client = MagicMock()
        self._client._client_id = "test-wbdali-client"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def subscribe(self, topic):
        pass

    def unfiltered_messages(self):
        async def empty_generator():
            return
            yield

        return empty_generator()


class _MockCommand(Command):
    def __init__(self, sendtwice=False, response_class=None):
        super().__init__(ForwardFrame(16, [0x12, 0x34]))
        self.sendtwice = sendtwice
        self.response = response_class

    def __str__(self):
        return "_MockCommand"


class MockResponse(Response):
    def __init__(self, frame):
        super().__init__(frame)
        self.frame = frame
        self.data = frame.as_integer if frame else None


class TestWBDALIDriver(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = WBDALIConfig()
        self.mock_mqtt_client = MockMqttClient()
        self.mock_mqtt_dispatcher = MQTTDispatcher(self.mock_mqtt_client)

    def test_encode_frame_for_modbus(self):
        """Test encoding of DALI frames into Modbus frames."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)

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

        # Test FF16 frame: priority=1 (bits 31..29), sendtwice=0 (bit 28), size=0 (bits 27..25), data=0x1234 (bits 24..0)
        # Result: 0x20000000 | 0x00000000 | 0x00000000 | 0x00001234 = 0x20001234
        assert driver._encode_frame_for_modbus(frame_16) == 0x20001234  # pylint: disable=W0212

        # Test FF24 frame: priority=1, sendtwice=0, size=1 (bits 27..25), data=0x123456
        # Result: 0x20000000 | 0x00000000 | 0x02000000 | 0x00123456 = 0x22123456
        assert driver._encode_frame_for_modbus(frame_24) == 0x22123456  # pylint: disable=W0212

        # Test FF25 frame: priority=1, sendtwice=0, size=2 (bits 27..25), data=0x1234567
        # Result: 0x20000000 | 0x00000000 | 0x04000000 | 0x01234567 = 0x25234567
        assert driver._encode_frame_for_modbus(frame_25) == 0x25234567  # pylint: disable=W0212

        # Test with sendtwice=True: bit 28 should be set
        # Result: 0x20000000 | 0x10000000 | 0x00000000 | 0x00001234 = 0x30001234
        assert (
            driver._encode_frame_for_modbus(frame_16, sendtwice=True) == 0x30001234
        )  # pylint: disable=W0212

        # Test with priority=3: bits 31..29 = 0b011 = 0x60000000
        # Result: 0x60000000 | 0x00000000 | 0x00000000 | 0x00001234 = 0x60001234
        assert driver._encode_frame_for_modbus(frame_16, priority=3) == 0x60001234  # pylint: disable=W0212

        # Test with sendtwice=True and priority=5
        # Result: 0xA0000000 | 0x10000000 | 0x00000000 | 0x00001234 = 0xB0001234
        assert (
            driver._encode_frame_for_modbus(frame_16, sendtwice=True, priority=5) == 0xB0001234
        )  # pylint: disable=W0212

        # Test invalid frame length
        with self.assertRaises(ValueError):
            driver._encode_frame_for_modbus(frame_invalid)  # pylint: disable=W0212

        # Test invalid priority
        with self.assertRaises(ValueError):
            driver._encode_frame_for_modbus(frame_16, priority=6)  # pylint: disable=W0212

    async def test_send_command_without_response(self):
        """Test sending a command that does not expect a response."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.bus_traffic._invoke = MagicMock()  # pylint: disable=W0212

        driver.send_barrier = AsyncMock()
        driver.send_barrier.wait = AsyncMock(return_value=(0, [(0, 0x12340000)]))
        driver.next_pointer = 0

        cmd = _MockCommand(sendtwice=False, response_class=None)

        mock_future = AsyncMock()
        mock_get_next_pointer = AsyncMock(return_value=(0, mock_future))
        with patch.object(driver, "get_next_pointer", mock_get_next_pointer):
            with patch.object(driver, "_add_cmd_to_send_buffer", new_callable=AsyncMock) as mock_add_cmd:
                result = await driver.send(cmd)
                self.assertIsNone(result)
                mock_add_cmd.assert_called_once()
                driver.bus_traffic._invoke.assert_called_once_with(cmd, None, False)  # pylint: disable=W0212

    async def test_send_command_with_response(self):
        """Test sending a command that expects a response."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.bus_traffic._invoke = MagicMock()  # pylint: disable=W0212

        driver.send_barrier = AsyncMock()
        driver.send_barrier.wait = AsyncMock(return_value=(0, [(0, 0x12340000)]))
        driver.next_pointer = 0

        cmd = _MockCommand(sendtwice=False, response_class=MockResponse)

        response_frame = BackwardFrame(0x56)
        mock_future = asyncio.Future()
        mock_future.set_result(response_frame)

        mock_get_next_pointer = AsyncMock(return_value=(0, mock_future))
        with patch.object(driver, "get_next_pointer", mock_get_next_pointer):
            with patch.object(driver, "_add_cmd_to_send_buffer", new_callable=AsyncMock) as mock_add_cmd:
                result = await driver.send(cmd)
                self.assertIsInstance(result, MockResponse)
                self.assertEqual(result.data, 0x56)
                mock_add_cmd.assert_called_once()
                driver.bus_traffic._invoke.assert_called_once_with(  # pylint: disable=W0212
                    cmd, result, False
                )

    async def test_send_command_sendtwice_with_response_raises_error(self):
        """Test that sending a command with sendtwice=True and a response raises an error."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)

        cmd = _MockCommand(sendtwice=True, response_class=MockResponse)

        with self.assertRaises(ValueError) as context:
            await driver.send(cmd)

        self.assertIn("Command with sendtwice=True cannot have a response", str(context.exception))

    async def test_send_command_sendtwice_without_response(self):
        """Test sending a command with sendtwice=True and no response."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.bus_traffic._invoke = MagicMock()  # pylint: disable=W0212

        cmd = _MockCommand(sendtwice=True, response_class=None)

        mock_future1 = AsyncMock()
        mock_future2 = AsyncMock()

        mock_get_next_pointer = AsyncMock(side_effect=[(0, mock_future1), (1, mock_future2)])
        with patch.object(driver, "get_next_pointer", mock_get_next_pointer):
            with patch.object(driver, "_add_cmd_to_send_buffer", new_callable=AsyncMock) as mock_add_cmd:
                result = await driver.send(cmd)
                self.assertIsNone(result)
                self.assertEqual(mock_add_cmd.call_count, 2)
                driver.bus_traffic._invoke.assert_called_once_with(cmd, None, False)  # pylint: disable=W0212

    async def test_add_cmd_to_send_buffer_single_command(self):
        """Test adding a single command to the send buffer."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.rpc_id_counter = 0

        driver.send_barrier = AsyncMock()
        driver.send_barrier.wait = AsyncMock(return_value=(0, [(5, 0x12340000)]))

        await driver._add_cmd_to_send_buffer(5, 0x12340000)  # pylint: disable=W0212

        self.mock_mqtt_client.publish.assert_called_once()

        call_args = self.mock_mqtt_client.publish.call_args
        topic, payload = call_args[0]
        self.assertTrue(topic.startswith("/rpc/v1/wb-mqtt-serial/port/Load/test-wbdali-client-"))
        payload_data = json.loads(payload)
        self.assertEqual(payload_data["id"], 1)
        self.assertEqual(payload_data["params"]["slave_id"], driver.config.modbus_slave_id)
        self.assertEqual(payload_data["params"]["function"], 16)
        self.assertEqual(payload_data["params"]["address"], 1400 + 5 * 2)
        self.assertEqual(payload_data["params"]["count"], 2)
        self.assertEqual(payload_data["params"]["msg"], "12340000")
        self.assertEqual(payload_data["params"]["protocol"], "modbus")
        self.assertEqual(payload_data["params"]["format"], "HEX")

    async def test_add_cmd_to_send_buffer_multiple_consecutive_commands(self):
        """Test adding multiple consecutive commands to the send buffer."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.rpc_id_counter = 0

        driver.send_barrier = AsyncMock()
        driver.send_barrier.wait = AsyncMock(
            return_value=(0, [(5, 0x12340000), (6, 0x56780000), (7, 0x9ABC0000)])
        )

        await driver._add_cmd_to_send_buffer(5, 0x12340000)  # pylint: disable=W0212
        self.mock_mqtt_client.publish.assert_called_once()

        call_args = self.mock_mqtt_client.publish.call_args
        _, payload = call_args[0]
        payload_data = json.loads(payload)
        self.assertEqual(payload_data["params"]["address"], 1400 + 5 * 2)
        self.assertEqual(payload_data["params"]["count"], 6)
        self.assertEqual(payload_data["params"]["msg"], "12340000567800009abc0000")

    async def test_add_cmd_to_send_buffer_non_consecutive_commands(self):
        """Test adding non-consecutive commands to the send buffer."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.rpc_id_counter = 0

        driver.send_barrier = AsyncMock()
        driver.send_barrier.wait = AsyncMock(
            return_value=(
                0,
                [(5, 0x12340000), (8, 0x56780000)],
            )
        )

        with patch.object(driver, "send_modbus_rpc_no_response", new_callable=AsyncMock) as mock_send_modbus:
            await driver._add_cmd_to_send_buffer(5, 0x12340000)  # pylint: disable=W0212
            self.assertEqual(mock_send_modbus.call_count, 2)

            first_call = mock_send_modbus.call_args_list[0]
            self.assertEqual(first_call.kwargs["address"], 1400 + 5 * 2)
            self.assertEqual(first_call.kwargs["count"], 2)
            self.assertEqual(first_call.kwargs["msg"], "12340000")

            second_call = mock_send_modbus.call_args_list[1]
            self.assertEqual(second_call.kwargs["address"], 1400 + 8 * 2)
            self.assertEqual(second_call.kwargs["count"], 2)
            self.assertEqual(second_call.kwargs["msg"], "56780000")

    async def test_send_modbus_rpc_no_response_mqtt_publish(self):
        """Test that send_modbus_rpc_no_response sends the correct MQTT publish."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.rpc_id_counter = 42

        await driver.send_modbus_rpc_no_response(function=16, address=1930, count=4, msg="1234567890abcdef")

        self.mock_mqtt_client.publish.assert_called_once()

        call_args = self.mock_mqtt_client.publish.call_args
        topic, payload = call_args[0]

        self.assertTrue(topic.startswith("/rpc/v1/wb-mqtt-serial/port/Load/test-wbdali-client-"))

        payload_data = json.loads(payload)
        expected_payload = {
            "params": {
                "slave_id": self.config.modbus_slave_id,
                "function": 16,
                "address": 1930,
                "count": 4,
                "frame_timeout": 0,
                "protocol": "modbus",
                "format": "HEX",
                "path": self.config.modbus_port_path,
                "baud_rate": self.config.modbus_baud_rate,
                "parity": self.config.modbus_parity,
                "data_bits": self.config.modbus_data_bits,
                "stop_bits": self.config.modbus_stop_bits,
                "msg": "1234567890abcdef",
            },
            "id": 43,
        }

        self.assertEqual(payload_data, expected_payload)
        self.assertEqual(driver.rpc_id_counter, 43)

    async def test_send_modbus_rpc_mqtt_publish_timeout(self):
        """Test that send_modbus_rpc_no_response raises exception when MQTT publish fails."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)

        self.mock_mqtt_client.publish.side_effect = Exception("MQTT publish failed")

        with self.assertRaises(Exception) as context:
            await driver.send_modbus_rpc_no_response(
                function=16, address=1930, count=4, msg="1234567890abcdef"
            )

        self.assertIn("MQTT publish failed", str(context.exception))

    async def test_reset_queue(self):
        """Test that reset_queue sends the correct Modbus commands to reset the device queue."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.rpc_id_counter = 10
        driver.next_pointer = 5

        await driver.reset_queue()

        self.assertEqual(driver.next_pointer, 0)
        self.assertEqual(self.mock_mqtt_client.publish.call_count, 1)

        first_call = self.mock_mqtt_client.publish.call_args_list[0]
        first_payload = json.loads(first_call[0][1])
        self.assertEqual(first_payload["params"]["function"], 6)
        self.assertEqual(first_payload["params"]["address"], 1432)  # config.channel * 1000 + 432
        self.assertEqual(first_payload["params"]["count"], 1)
        self.assertEqual(first_payload["params"]["msg"], "0000")

    async def test_get_next_pointer(self):
        """Test that get_next_pointer returns the correct pointer and future."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.next_pointer = 5
        driver.cmd_counter = 10

        pointer, future = await driver.get_next_pointer()

        self.assertEqual(pointer, 5)
        self.assertIsInstance(future, asyncio.Future)
        self.assertEqual(driver.next_pointer, 6)
        self.assertEqual(driver.cmd_counter, 11)
        self.assertIs(driver.responses[5], future)

    async def test_get_next_pointer_wraps_around(self):
        """Test that get_next_pointer wraps around when reaching device_queue_size."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.next_pointer = 19
        driver.device_queue_size = 20

        pointer1, _ = await driver.get_next_pointer()
        self.assertEqual(pointer1, 19)
        self.assertEqual(driver.next_pointer, 0)

    async def test_get_next_pointer_waits_for_pending_response(self):
        """Test that get_next_pointer waits if there is a pending response for the next pointer."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.next_pointer = 3

        pending_future = asyncio.Future()
        driver.responses[3] = pending_future

        task = asyncio.create_task(driver.get_next_pointer())

        await asyncio.sleep(0.01)

        self.assertFalse(task.done())

        pending_future.set_result(None)

        pointer, _ = await task
        self.assertEqual(pointer, 3)

    async def test_add_cmd_to_send_buffer_with_barrier_timeout(self):
        """Test that _add_cmd_to_send_buffer raises TimeoutError on barrier timeout."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)

        driver.send_barrier = AsyncMock()
        driver.send_barrier.wait = AsyncMock(side_effect=asyncio.TimeoutError())

        with self.assertRaises(asyncio.TimeoutError):
            await driver._add_cmd_to_send_buffer(5, 0x12340000)  # pylint: disable=W0212

    async def test_send_modbus_rpc_increments_counter(self):
        """Test that rpc_id_counter increments correctly with multiple calls."""
        driver = WBDALIDriver(self.config, mqtt_dispatcher=self.mock_mqtt_dispatcher)
        driver.rpc_id_counter = 100

        await driver.send_modbus_rpc_no_response(function=16, address=1920, count=2, msg="12340000")

        self.assertEqual(driver.rpc_id_counter, 101)

        await driver.send_modbus_rpc_no_response(function=16, address=1922, count=2, msg="56780000")

        self.assertEqual(driver.rpc_id_counter, 102)
