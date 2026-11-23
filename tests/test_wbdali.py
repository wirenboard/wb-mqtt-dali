import asyncio
import logging

try:
    from unittest.mock import MagicMock
except ImportError:
    from mock import MagicMock

from typing import Optional

import aiomqtt
from dali.address import GearShort
from dali.command import Command, Response
from dali.frame import BackwardFrame, ForwardFrame
from dali.gear.general import DTR0

from wb.mqtt_dali.bus_traffic import BusTrafficItem, BusTrafficSource
from wb.mqtt_dali.helvar.digidim import ExtendedDtrMsb, ReadE2
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import (
    MODBUS_READ_HOLDING_REGISTERS,
    FramePriority,
    WBDALIConfig,
    WBDALIDriver,
    encode_frame_for_modbus,
)
from wb.mqtt_dali.wbdali_error_response import (
    NoResponseFromGateway,
    WbGatewayTransmissionError,
)

from ._fake_rpc_client import FakePortLoadClient
from ._virtual_time import VirtualTimeTestCase

# pylint: disable=line-too-long, protected-access


class MockMqttClient:
    def __init__(self, *_args, **_kwargs):
        self._client = MagicMock()
        self._client._client_id = "test-wbdali-client"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def subscribe(self, topic: str) -> None:
        pass

    async def unsubscribe(self, topic: str) -> None:
        pass


class _MockCommand(Command):
    def __init__(self, sendtwice=False, response_class=None, data=None):
        if data is None:
            data = [0x12, 0x34]
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


class _CountingReadsClient(FakePortLoadClient):
    """Counts register reads and yields before answering, as a real RPC call does."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def call(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        self, driver: str, service: str, method: str, params: dict, timeout_s: float
    ):
        if params["function"] == MODBUS_READ_HOLDING_REGISTERS:
            self.reads += 1
            await asyncio.sleep(0)
        return await super().call(driver, service, method, params, timeout_s)


class TestWBDALIDriver(VirtualTimeTestCase):
    def setUp(self):
        self.config = WBDALIConfig()
        self.mock_mqtt_client = MockMqttClient()
        self.mock_mqtt_dispatcher = MQTTDispatcher(self.mock_mqtt_client)
        self.mock_logger = MagicMock(spec=logging.Logger)
        self.rpc = FakePortLoadClient()

    def simulate_timeout_response_from_gateway(self, queue_item_index: int, count: int = 1):
        """Simulate a timeout response from the gateway for a given batch start index."""
        for i in range(count):
            index = queue_item_index + i
            index %= self.config.queue_size
            message = aiomqtt.Message(
                topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_{index}",
                payload=str(0x0200),
                qos=0,
                retain=False,
                mid=0,
                properties=None,
            )
            self.mock_mqtt_dispatcher._dispatch_message(message)

    async def prepare_driver(self, driver: WBDALIDriver):
        """Initialise and fill the whole queue once, so the deferred reset is done and slot 0 is next."""
        await driver.initialize()
        self.rpc.clear()
        cmds = [_MockCommand() for _ in range(self.config.queue_size)]
        fut = asyncio.gather(
            driver.send_commands(cmds, BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger)
        )
        await self.rpc.wait_for_write()  # the deferred queue reset
        await self.rpc.wait_for_write()
        self.simulate_timeout_response_from_gateway(0, len(cmds))
        await fut

    def test_encode_frame_for_modbus(self):
        """Frame-size, sendtwice and priority bits are placed in the right slots
        of the encoded 32-bit Modbus register (IEC 62386-103:2022 §9.14.1)."""
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

        prio = FramePriority.AUTOMATIC
        # FF16, AUTOMATIC (=4), no sendtwice
        # 4 << 29 | 0x1234 = 0x80001234
        assert encode_frame_for_modbus(frame_16, sendtwice=False, priority=prio) == 0x80001234

        # FF24, AUTOMATIC, size code 1 in bits [27..25]
        assert encode_frame_for_modbus(frame_24, sendtwice=False, priority=prio) == 0x82123456

        # FF25, AUTOMATIC, size code 2
        assert encode_frame_for_modbus(frame_25, sendtwice=False, priority=prio) == 0x85234567

        # sendtwice toggles bit 28
        assert encode_frame_for_modbus(frame_16, sendtwice=True, priority=prio) == 0x90001234

        # Unsupported frame length still raises
        with self.assertRaises(ValueError):
            encode_frame_for_modbus(frame_invalid, sendtwice=False, priority=prio)

    async def test_send_command_without_response(self):
        """
        Test sending a command that does not expect a response.
        The test simulates getting a response from gateway with "No response" flag.
        """
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)
        cmd = _MockCommand(sendtwice=False, response_class=None)
        fut = asyncio.gather(driver.send(cmd, BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger))
        await self.rpc.wait_for_write()
        self.simulate_timeout_response_from_gateway(0)
        result = (await fut)[0]
        self.assertIsNone(result.raw_value)

    async def test_batch_with_ff17_is_not_sent(self):
        """Gateway firmware 1.2.3 does not send FF17: a batch with an FF17 frame is not written
        to the gateway and every frame of it fails."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)
        responses = await driver.send_commands(
            [DTR0(0), ExtendedDtrMsb(0), ReadE2(GearShort(1))],
            BusTrafficSource.WB,
            FramePriority.CONFIGURATION,
            driver.logger,
        )
        self.assertTrue(all(isinstance(response, WbGatewayTransmissionError) for response in responses))
        self.assertEqual(self.rpc.pending_writes(), 0)

    async def test_concurrent_ff17_batches_read_gateway_firmware_once(self):
        """Two FF17 batches sent at once before the firmware is known: one firmware read, both fail."""
        self.rpc = _CountingReadsClient()
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)
        self.rpc.reads = 0
        batches = await asyncio.gather(
            *(
                driver.send_commands(
                    [ReadE2(GearShort(short))],
                    BusTrafficSource.WB,
                    FramePriority.CONFIGURATION,
                    driver.logger,
                )
                for short in (1, 2)
            )
        )
        self.assertEqual(self.rpc.reads, 1)
        self.assertTrue(all(isinstance(r, WbGatewayTransmissionError) for batch in batches for r in batch))

    async def test_first_batch_with_ff17_is_sent_from_firmware_1_3_0(self):
        """Gateway firmware 1.3.0, an FF17 batch is the first after start: the queue is reset and the
        batch is written with the FF17 frame in it."""
        self.rpc.firmware_version = "1.3.0"
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await driver.initialize()
        self.rpc.clear()
        command = ReadE2(GearShort(1))
        fut = asyncio.gather(
            driver.send_commands([command], BusTrafficSource.WB, FramePriority.CONFIGURATION, driver.logger)
        )

        await self.rpc.wait_for_write()  # the deferred queue reset
        params = await self.rpc.wait_for_write()
        self.simulate_timeout_response_from_gateway(0)
        await fut

        reg = encode_frame_for_modbus(command.frame, False, FramePriority.CONFIGURATION)
        self.assertEqual(params["msg"], f"{((reg & 0xFFFF) << 16) | (reg >> 16):08x}")

    async def test_send_command_with_response(self):
        """Test sending a command that expects a response."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)

        cmd = _MockCommand(sendtwice=False, response_class=MockResponse)

        fut = asyncio.gather(driver.send(cmd, BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger))
        await self.rpc.wait_for_write()
        message = aiomqtt.Message(
            topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_0",
            payload=str(0x0156),
            qos=0,
            retain=False,
            mid=0,
            properties=None,
        )
        self.mock_mqtt_dispatcher._dispatch_message(message)
        result = (await fut)[0]
        self.assertIsInstance(result, MockResponse)
        self.assertEqual(result.data, 0x56)

    async def test_send_command_sendtwice_without_response(self):
        """Test sending a command with sendtwice=True and no response."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)

        cmd = _MockCommand(sendtwice=True, response_class=None)

        fut = asyncio.gather(driver.send(cmd, BusTrafficSource.WB, FramePriority.AUTOMATIC, driver.logger))
        params = await self.rpc.wait_for_write()
        self.assertEqual(params["count"], 2)
        self.assertEqual(params["msg"], "12349000")

        self.simulate_timeout_response_from_gateway(0, 2)

        result = (await fut)[0]
        self.assertIsNone(result.raw_value)

    async def test_send_single_command_rpc_request(self):
        """Test adding a single command to the send buffer."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)
        cmd = _MockCommand(sendtwice=False, response_class=None)
        fut = asyncio.gather(driver.send(cmd, BusTrafficSource.WB, FramePriority.AUTOMATIC, driver.logger))
        params = await self.rpc.wait_for_write()
        self.simulate_timeout_response_from_gateway(0)
        await fut
        self.assertEqual(params["device_id"], driver.config.device_name)
        self.assertEqual(params["function"], 16)
        self.assertEqual(params["address"], self.config.queue_start_modbus_address)
        self.assertEqual(params["count"], 2)
        self.assertEqual(params["msg"], "12348000")
        self.assertEqual(params["format"], "HEX")
        self.assertEqual(params["frame_timeout"], 0)

    async def test_send_commands(self):
        """Test adding multiple consecutive commands to the send buffer."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)
        cmds = [
            _MockCommand(sendtwice=False, data=[0x12, 0x34], response_class=MockResponse),
            _MockCommand(sendtwice=False, data=[0x56, 0x78], response_class=MockResponse),
            _MockCommand(sendtwice=False, data=[0x9A, 0xBC], response_class=None),
        ]
        fut = asyncio.gather(
            driver.send_commands(cmds, BusTrafficSource.WB, FramePriority.AUTOMATIC, driver.logger)
        )
        params = await self.rpc.wait_for_write()
        self.assertEqual(params["count"], 6)
        self.assertEqual(params["msg"], "12348000567880009abc8000")

        # Simulate responses
        for i in range(3):
            message = aiomqtt.Message(
                topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_{i}",
                payload=str(0x0150 + i) if i < 2 else str(0x0200),
                qos=0,
                retain=False,
                mid=0,
                properties=None,
            )
            self.mock_mqtt_dispatcher._dispatch_message(message)

        results = (await fut)[0]
        self.assertEqual(len(results), 3)
        self.assertIsInstance(results[0], MockResponse)
        self.assertEqual(results[0].data, 0x50)
        self.assertIsInstance(results[1], MockResponse)
        self.assertEqual(results[1].data, 0x51)
        self.assertIsNone(results[2].raw_value)

    async def test_batch_independent_commands(self):
        """Test adding multiple independent commands to the send buffer."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)
        cmds = [
            driver.send(
                _MockCommand(data=[0x12, 0x34]), BusTrafficSource.WB, FramePriority.AUTOMATIC, driver.logger
            ),
            driver.send(
                _MockCommand(data=[0x56, 0x78]), BusTrafficSource.WB, FramePriority.AUTOMATIC, driver.logger
            ),
            driver.send(
                _MockCommand(data=[0x9A, 0xBC]), BusTrafficSource.WB, FramePriority.AUTOMATIC, driver.logger
            ),
        ]
        fut = asyncio.gather(*cmds)
        params = await self.rpc.wait_for_write()
        self.simulate_timeout_response_from_gateway(0, len(cmds))
        await fut
        self.assertEqual(params["count"], 6)
        self.assertEqual(params["msg"], "12348000567880009abc8000")

    async def test_send_command_timeout(self):
        """Test that a command times out if no response is received."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await driver.initialize()

        cmd = _MockCommand(sendtwice=False, response_class=MockResponse)

        # Don't send any response, let it timeout
        result = await driver.send(cmd, BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, NoResponseFromGateway)

    async def test_unexpected_send_error_resets_the_queue_before_the_next_batch(self):
        """Writing a batch raises an unexpected error: its commands get a transmission error with an
        ERROR record, the next batch goes to slot 0 after a queue reset."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)
        answered = asyncio.gather(
            driver.send(_MockCommand(), BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger)
        )
        await self.rpc.wait_for_write()
        self.simulate_timeout_response_from_gateway(0)
        await answered

        self.rpc.error = RuntimeError("boom")
        failed = await driver.send_commands(
            [_MockCommand(), _MockCommand()], BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger
        )
        self.assertEqual([type(r) for r in failed], [WbGatewayTransmissionError] * 2)
        self.mock_logger.error.assert_called_once()

        self.rpc.error = None
        following = asyncio.gather(
            driver.send(_MockCommand(), BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger)
        )
        self.assertEqual((await self.rpc.wait_for_write())["function"], 6)
        self.assertEqual((await self.rpc.wait_for_write())["address"], 1400)
        self.simulate_timeout_response_from_gateway(0)
        self.assertIsNone((await following)[0].raw_value)

    async def test_handle_reply_message_with_framing_error(self):
        """Test handling a reply message with a framing error."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)

        cmd = _MockCommand(sendtwice=False, response_class=MockResponse)

        fut = asyncio.gather(driver.send(cmd, BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger))
        await self.rpc.wait_for_write()

        # Simulate framing error
        message = aiomqtt.Message(
            topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_0",
            payload=str(0x0300),
            qos=0,
            retain=False,
            mid=0,
            properties=None,
        )
        self.mock_mqtt_dispatcher._dispatch_message(message)

        result = (await fut)[0]
        self.assertIsNotNone(result)
        self.assertTrue(result.raw_value.error)

    async def test_reply_retained_message_ignored(self):
        """A retained reply message is dropped before parsing: the pending
        command stays unresolved and is not failed by the retained payload."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)

        cmd = _MockCommand(sendtwice=False, response_class=MockResponse)
        fut = asyncio.gather(driver.send(cmd, BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger))
        await self.rpc.wait_for_write()

        message = aiomqtt.Message(
            topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_0",
            payload="",
            qos=0,
            retain=True,
            mid=0,
            properties=None,
        )
        self.mock_mqtt_dispatcher._dispatch_message(message)

        # Retained drop must not resolve the waiter; a real timeout eventually
        # does. The outer wait_for guards against a hang if that regresses.
        result = (await asyncio.wait_for(fut, timeout=10.0))[0]
        self.assertIsInstance(result, NoResponseFromGateway)

    async def test_reply_malformed_payload_logged_with_context(self):
        """A non-numeric reply payload resolves the command as a transmission
        error and logs the failure with the reply topic for context."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)

        cmd = _MockCommand(sendtwice=False, response_class=MockResponse)
        fut = asyncio.gather(driver.send(cmd, BusTrafficSource.WB, FramePriority.USER_ACTION, driver.logger))
        await self.rpc.wait_for_write()

        topic = f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_0"
        message = aiomqtt.Message(
            topic=topic,
            payload="not-a-number",
            qos=0,
            retain=False,
            mid=0,
            properties=None,
        )
        self.mock_mqtt_dispatcher._dispatch_message(message)

        result = (await asyncio.wait_for(fut, timeout=1.0))[0]
        self.assertIsInstance(result, WbGatewayTransmissionError)
        logged_with_topic = any(topic in str(call_args) for call_args in driver.logger.error.call_args_list)
        self.assertTrue(logged_with_topic)

    async def test_bus_traffic_callbacks(self):
        """Test that bus traffic callbacks are invoked correctly."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)

        callback_invoked = asyncio.Event()
        received_request_frame = None
        received_response: Optional[Response] = None
        received_source = None

        def traffic_callback(item: BusTrafficItem):
            nonlocal received_request_frame, received_response, received_source
            received_request_frame = item.request
            received_response = item.response
            received_source = item.request_source
            callback_invoked.set()

        driver.bus_traffic.register(traffic_callback)

        cmd = _MockCommand(sendtwice=False, response_class=None)
        fut = asyncio.gather(
            driver.send(cmd, BusTrafficSource.LUNATONE, FramePriority.USER_ACTION, driver.logger)
        )

        await self.rpc.wait_for_write()

        message = aiomqtt.Message(
            topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_bulk_send_reply_0",
            payload=str(0x0112),
            qos=0,
            retain=False,
            mid=0,
            properties=None,
        )
        self.mock_mqtt_dispatcher._dispatch_message(message)
        await fut
        await asyncio.wait_for(callback_invoked.wait(), timeout=1.0)
        self.assertEqual(received_source, BusTrafficSource.LUNATONE)
        self.assertEqual(cmd.frame, received_request_frame)
        self.assertIsNotNone(received_response)
        self.assertEqual(BackwardFrame(0x12), received_response.raw_value)

    async def test_handle_ff24_message(self):
        """Test handling of 24-bit forward frame messages."""
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await driver.initialize()

        callback_invoked = asyncio.Event()

        def traffic_callback(item: BusTrafficItem):
            if item.request_source == BusTrafficSource.BUS:
                callback_invoked.set()

        driver.bus_traffic.register(traffic_callback)

        message = aiomqtt.Message(
            topic=f"/devices/{self.config.device_name}/controls/bus_{self.config.bus}_monitor_sporadic_frame_1",
            payload=str(0x1A900180088863B),
            qos=0,
            retain=False,
            mid=0,
            properties=None,
        )
        self.mock_mqtt_dispatcher._dispatch_message(message)

        await asyncio.wait_for(callback_invoked.wait(), timeout=1.0)

    async def test_queue_overflow(self):
        """Test behavior when queue reaches maximum size."""
        self.config.queue_size = 2
        driver = WBDALIDriver(self.config, self.mock_mqtt_dispatcher, self.mock_logger, self.rpc)
        await self.prepare_driver(driver)

        commands = [
            _MockCommand(),
            _MockCommand(data=[0x56, 0x78]),
            _MockCommand(data=[0x9A, 0xBC]),
        ]

        fut = asyncio.gather(
            driver.send_commands(commands, BusTrafficSource.WB, FramePriority.AUTOMATIC, driver.logger)
        )

        params = await self.rpc.wait_for_write()
        self.assertEqual(params["count"], 4)
        self.assertEqual(params["msg"], "1234800056788000")

        self.simulate_timeout_response_from_gateway(0, 2)

        params = await self.rpc.wait_for_write()
        self.assertEqual(params["count"], 2)
        self.assertEqual(params["msg"], "9abc8000")

        self.simulate_timeout_response_from_gateway(2)

        await fut
