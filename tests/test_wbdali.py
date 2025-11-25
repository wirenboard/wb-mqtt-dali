import asyncio
import json
import unittest

try:
    from unittest.mock import AsyncMock, MagicMock, patch
except ImportError:
    from mock import AsyncMock, MagicMock, patch

from dali.command import Command, Response
from dali.frame import BackwardFrame, ForwardFrame

from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver


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
        self.config = WBDALIConfig(
            modbus_port_path="/dev/ttyRS485-1",
            device_name="wb-mdali",
            mqtt_host="localhost",
            mqtt_port=1883,
        )

    def test_encode_frame_for_modbus(self):
        """Test encoding of DALI frames into Modbus frames."""
        driver = WBDALIDriver(self.config)

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

        assert driver._encode_frame_for_modbus(frame_16) == 0x12340000  # pylint: disable=W0212
        assert driver._encode_frame_for_modbus(frame_24) == 0x12345601  # pylint: disable=W0212
        assert driver._encode_frame_for_modbus(frame_25) == 0x123453382  # pylint: disable=W0212

        with self.assertRaises(ValueError):
            driver._encode_frame_for_modbus(frame_invalid)  # pylint: disable=W0212

    @patch("asyncio_mqtt.Client")
    async def test_send_command_without_response(self, mock_mqtt_client_class):
        """Test sending a command that does not expect a response."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
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

    @patch("asyncio_mqtt.Client")
    async def test_send_command_with_response(self, mock_mqtt_client_class):
        """Test sending a command that expects a response."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
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

    @patch("asyncio_mqtt.Client")
    async def test_send_command_sendtwice_with_response_raises_error(self, mock_mqtt_client_class):
        """Test that sending a command with sendtwice=True and a response raises an error."""
        mock_mqtt_client = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)

        cmd = _MockCommand(sendtwice=True, response_class=MockResponse)

        with self.assertRaises(ValueError) as context:
            await driver.send(cmd)

        self.assertIn("Command with sendtwice=True cannot have a response", str(context.exception))

    @patch("asyncio_mqtt.Client")
    async def test_send_command_sendtwice_without_response(self, mock_mqtt_client_class):
        """Test sending a command with sendtwice=True and no response."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
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

    @patch("asyncio_mqtt.Client")
    async def test_add_cmd_to_send_buffer_single_command(self, mock_mqtt_client_class):
        """Test adding a single command to the send buffer."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
        driver.rpc_id_counter = 0

        driver.send_barrier = AsyncMock()
        driver.send_barrier.wait = AsyncMock(return_value=(0, [(5, 0x12340000)]))

        await driver._add_cmd_to_send_buffer(5, 0x12340000)  # pylint: disable=W0212

        mock_mqtt_client.publish.assert_called_once()

        call_args = mock_mqtt_client.publish.call_args
        topic, payload = call_args[0]
        self.assertEqual(topic, "/rpc/v1/wb-mqtt-serial/port/Load/dali-no-response")
        payload_data = json.loads(payload)
        self.assertEqual(payload_data["id"], 1)
        self.assertEqual(payload_data["params"]["slave_id"], driver.config.modbus_slave_id)
        self.assertEqual(payload_data["params"]["function"], 16)
        self.assertEqual(payload_data["params"]["address"], 1920 + 5 * 2)
        self.assertEqual(payload_data["params"]["count"], 2)
        self.assertEqual(payload_data["params"]["msg"], "12340000")
        self.assertEqual(payload_data["params"]["protocol"], "modbus")
        self.assertEqual(payload_data["params"]["format"], "HEX")

    @patch("asyncio_mqtt.Client")
    async def test_add_cmd_to_send_buffer_multiple_consecutive_commands(self, mock_mqtt_client_class):
        """Test adding multiple consecutive commands to the send buffer."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
        driver.rpc_id_counter = 0

        driver.send_barrier = AsyncMock()
        driver.send_barrier.wait = AsyncMock(
            return_value=(0, [(5, 0x12340000), (6, 0x56780000), (7, 0x9ABC0000)])
        )

        await driver._add_cmd_to_send_buffer(5, 0x12340000)  # pylint: disable=W0212
        mock_mqtt_client.publish.assert_called_once()

        call_args = mock_mqtt_client.publish.call_args
        _, payload = call_args[0]
        payload_data = json.loads(payload)
        self.assertEqual(payload_data["params"]["address"], 1920 + 5 * 2)
        self.assertEqual(payload_data["params"]["count"], 6)
        self.assertEqual(payload_data["params"]["msg"], "12340000567800009abc0000")

    @patch("asyncio_mqtt.Client")
    async def test_add_cmd_to_send_buffer_non_consecutive_commands(self, mock_mqtt_client_class):
        """Test adding non-consecutive commands to the send buffer."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
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
            self.assertEqual(first_call.kwargs["address"], 1920 + 5 * 2)
            self.assertEqual(first_call.kwargs["count"], 2)
            self.assertEqual(first_call.kwargs["msg"], "12340000")

            second_call = mock_send_modbus.call_args_list[1]
            self.assertEqual(second_call.kwargs["address"], 1920 + 8 * 2)
            self.assertEqual(second_call.kwargs["count"], 2)
            self.assertEqual(second_call.kwargs["msg"], "56780000")

    @patch("asyncio_mqtt.Client")
    async def test_send_modbus_rpc_no_response_mqtt_publish(self, mock_mqtt_client_class):
        """Test that send_modbus_rpc_no_response sends the correct MQTT publish."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
        driver.rpc_id_counter = 42

        await driver.send_modbus_rpc_no_response(function=16, address=1930, count=4, msg="1234567890abcdef")

        mock_mqtt_client.publish.assert_called_once()

        call_args = mock_mqtt_client.publish.call_args
        topic, payload = call_args[0]

        expected_topic = "/rpc/v1/wb-mqtt-serial/port/Load/dali-no-response"
        self.assertEqual(topic, expected_topic)

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

    @patch("asyncio_mqtt.Client")
    async def test_send_modbus_rpc_mqtt_connection_timeout(self, mock_mqtt_client_class):
        """Test that send_modbus_rpc_no_response raises TimeoutError on MQTT connection timeout."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_exception(asyncio.TimeoutError())
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)

        with self.assertRaises(asyncio.TimeoutError):
            await driver.send_modbus_rpc_no_response(
                function=16, address=1930, count=4, msg="1234567890abcdef"
            )

    @patch("asyncio_mqtt.Client")
    async def test_reset_queue(self, mock_mqtt_client_class):
        """Test that reset_queue sends the correct Modbus commands to reset the device queue."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
        driver.rpc_id_counter = 10
        driver.next_pointer = 5

        await driver.reset_queue()

        self.assertEqual(driver.next_pointer, 0)
        self.assertEqual(mock_mqtt_client.publish.call_count, 2)

        first_call = mock_mqtt_client.publish.call_args_list[0]
        first_payload = json.loads(first_call[0][1])
        self.assertEqual(first_payload["params"]["function"], 16)
        self.assertEqual(first_payload["params"]["address"], 1920)
        self.assertEqual(first_payload["params"]["count"], 10 * 2)
        self.assertEqual(first_payload["params"]["msg"], "0000fbdf" * 10)

        second_call = mock_mqtt_client.publish.call_args_list[1]
        second_payload = json.loads(second_call[0][1])
        self.assertEqual(second_payload["params"]["function"], 6)
        self.assertEqual(second_payload["params"]["address"], 1960)
        self.assertEqual(second_payload["params"]["count"], 1)
        self.assertEqual(second_payload["params"]["msg"], "0000")

    @patch("asyncio_mqtt.Client")
    async def test_get_next_pointer(self, mock_mqtt_client_class):
        """Test that get_next_pointer returns the correct pointer and future."""
        mock_mqtt_client = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
        driver.next_pointer = 5
        driver.cmd_counter = 10

        pointer, future = await driver.get_next_pointer()

        self.assertEqual(pointer, 5)
        self.assertIsInstance(future, asyncio.Future)
        self.assertEqual(driver.next_pointer, 6)
        self.assertEqual(driver.cmd_counter, 11)
        self.assertIs(driver.responses[5], future)

    @patch("asyncio_mqtt.Client")
    async def test_get_next_pointer_wraps_around(self, mock_mqtt_client_class):
        """Test that get_next_pointer wraps around when reaching device_queue_size."""
        mock_mqtt_client = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
        driver.next_pointer = 19
        driver.device_queue_size = 20

        pointer1, _ = await driver.get_next_pointer()
        self.assertEqual(pointer1, 19)
        self.assertEqual(driver.next_pointer, 0)

    @patch("asyncio_mqtt.Client")
    async def test_get_next_pointer_waits_for_pending_response(self, mock_mqtt_client_class):
        """Test that get_next_pointer waits if there is a pending response for the next pointer."""
        mock_mqtt_client = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
        driver.next_pointer = 3

        pending_future = asyncio.Future()
        driver.responses[3] = pending_future

        task = asyncio.create_task(driver.get_next_pointer())

        await asyncio.sleep(0.01)

        self.assertFalse(task.done())

        pending_future.set_result(None)

        pointer, _ = await task
        self.assertEqual(pointer, 3)

    @patch("asyncio_mqtt.Client")
    async def test_add_cmd_to_send_buffer_with_barrier_timeout(self, mock_mqtt_client_class):
        """Test that _add_cmd_to_send_buffer raises TimeoutError on barrier timeout."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)

        driver.send_barrier = AsyncMock()
        driver.send_barrier.wait = AsyncMock(side_effect=asyncio.TimeoutError())

        with self.assertRaises(asyncio.TimeoutError):
            await driver._add_cmd_to_send_buffer(5, 0x12340000)  # pylint: disable=W0212

    @patch("asyncio_mqtt.Client")
    async def test_send_modbus_rpc_increments_counter(self, mock_mqtt_client_class):
        """Test that rpc_id_counter increments correctly with multiple calls."""
        mock_mqtt_client = AsyncMock()
        connected_future = asyncio.Future()
        connected_future.set_result(None)
        mock_mqtt_client._connected = connected_future  # pylint: disable=W0212
        mock_mqtt_client.publish = AsyncMock()
        mock_mqtt_client_class.return_value = mock_mqtt_client

        driver = WBDALIDriver(self.config)
        driver.rpc_id_counter = 100

        await driver.send_modbus_rpc_no_response(function=16, address=1920, count=2, msg="12340000")

        self.assertEqual(driver.rpc_id_counter, 101)

        await driver.send_modbus_rpc_no_response(function=16, address=1922, count=2, msg="56780000")

        self.assertEqual(driver.rpc_id_counter, 102)
