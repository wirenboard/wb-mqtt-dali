"""Driver-level view of a lost broker link: an unreachable gateway, caught up with at the next send."""

import asyncio
import json
import logging
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

import aiomqtt
from dali.command import Command
from dali.frame import ForwardFrame

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import (
    GatewayUnavailable,
    NoResponseFromGateway,
    WbGatewayTransmissionError,
)

from ._broker_link_helpers import Inbox, RecordingClient


class _MockCommand(Command):
    def __init__(self):
        super().__init__(ForwardFrame(16, [0x12, 0x34]))
        self.sendtwice = False
        self.response = None


def _rpc_params(payload: str) -> dict:
    return json.loads(payload)["params"]


async def _end_session(run_task: asyncio.Task) -> None:
    run_task.cancel()
    try:
        await run_task
    except (asyncio.CancelledError, aiomqtt.MqttError):
        pass


class BrokerLinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_client = RecordingClient()
        self.dispatcher = MQTTDispatcher(self.mock_client)
        self.logger = MagicMock(spec=logging.Logger)
        self._session_task: Optional[asyncio.Task] = None

    async def asyncSetUp(self):
        self._run_dispatcher()

    def _run_dispatcher(self) -> None:
        """One dispatcher session: it ends when the message stream fails, or with the test."""
        self._session_task = asyncio.create_task(self.dispatcher.run())
        self.addAsyncCleanup(_end_session, self._session_task)

    async def _make_driver(self, response_timeout: float = 30.0) -> WBDALIDriver:
        driver = WBDALIDriver(WBDALIConfig(), self.dispatcher, self.logger)
        # Long enough that nothing times out inside a test unless the test asks for it.
        driver.response_timeout = response_timeout
        await driver.initialize()
        self.mock_client.drain_publishes()
        return driver

    def _rpc_topic(self, driver: WBDALIDriver) -> str:
        return f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"

    async def _fail_message_stream(self) -> None:
        """The broker drops: the session ends and its owner reports the loss, as the service does."""
        self.mock_client.messages.fail()
        await _end_session(self._session_task)
        self.dispatcher.connection_lost()

    async def _restore_link(self) -> None:
        """What the service does on reconnect: resubscribe and run a new session. The driver
        is not told."""
        await self.dispatcher.connection_restored()
        self.mock_client.messages = Inbox()
        self._run_dispatcher()

    def _deliver_reply(self, driver: WBDALIDriver, slot: int, status_word: int) -> None:
        topic = (
            f"/devices/{driver.config.device_name}/controls/bus_{driver.config.bus}_bulk_send_reply_{slot}"
        )
        self.mock_client.messages.deliver(aiomqtt.Message(topic, str(status_word), 0, False, 0, None))

    async def _deliver_retained_r(self, driver: WBDALIDriver) -> None:
        """The retained `r` on the gateway's `/meta/error`, as the broker delivers it on a
        (re)subscribe; returns once the driver has processed it."""
        topic = f"/devices/{driver.config.device_name}/meta/error"
        self.mock_client.messages.deliver(aiomqtt.Message(topic, b"r", 0, True, 0, None))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if self.mock_client.messages.pending() == 0 and driver.gateway_unavailable:
                return
        self.fail("retained `r` did not make the gateway unavailable")

    async def _expect_reset_then_batch_at_slot_zero(self, driver: WBDALIDriver) -> None:
        topic = self._rpc_topic(driver)
        reset = _rpc_params(await self.mock_client.wait_for_publish(topic))
        self.assertEqual((reset["function"], reset["address"], reset["msg"]), (6, 1432, "0000"))
        batch = _rpc_params(await self.mock_client.wait_for_publish(topic))
        self.assertEqual((batch["function"], batch["address"]), (16, 1400))

    async def test_new_commands_fail_at_once_and_in_flight_by_timeout_on_link_loss(self):
        """Link drops with three commands in flight: a new one is refused at once, the in-flight
        ones end by the response timeout."""
        driver = await self._make_driver(response_timeout=0.3)
        in_flight = asyncio.create_task(driver.send_commands([_MockCommand() for _ in range(3)]))
        await self.mock_client.wait_for_publish(self._rpc_topic(driver))

        await self._fail_message_stream()

        fresh = await asyncio.wait_for(driver.send(_MockCommand()), timeout=0.1)
        self.assertIsInstance(fresh, GatewayUnavailable)
        results = await asyncio.wait_for(in_flight, timeout=1.0)
        self.assertEqual([type(r) for r in results], [NoResponseFromGateway] * 3)
        self.assertEqual(self.mock_client.drain_publishes(), [])

    async def test_link_back_before_the_timeout_fails_in_flight_at_the_first_request(self):
        """Link back before the in-flight commands time out: the next request fails them and
        goes out after the queue reset, at slot 0."""
        driver = await self._make_driver()
        in_flight = asyncio.create_task(driver.send_commands([_MockCommand() for _ in range(3)]))
        await self.mock_client.wait_for_publish(self._rpc_topic(driver))
        await self._fail_message_stream()
        await self._restore_link()
        self.assertFalse(in_flight.done())

        send_task = asyncio.create_task(driver.send(_MockCommand()))

        results = await asyncio.wait_for(in_flight, timeout=0.5)
        self.assertEqual([type(r) for r in results], [GatewayUnavailable] * 3)
        await self._expect_reset_then_batch_at_slot_zero(driver)
        self._deliver_reply(driver, 0, 0x0200)
        self.assertNotIsInstance(await asyncio.wait_for(send_task, timeout=1.0), WbGatewayTransmissionError)

    async def test_batch_refused_by_the_client_fails_whole_and_the_next_one_resyncs(self):
        """The client refuses the batch's Modbus RPC while the link still counts as up: the batch
        and the commands in flight fail at once; the next batch takes the slots after them."""
        driver = await self._make_driver()
        topic = self._rpc_topic(driver)
        in_flight = asyncio.create_task(driver.send_commands([_MockCommand() for _ in range(2)]))
        await self.mock_client.wait_for_publish(topic)
        self.mock_client.fail("publish", topic)

        results = await asyncio.wait_for(
            driver.send_commands([_MockCommand() for _ in range(3)]), timeout=0.5
        )

        self.assertEqual([type(r) for r in results], [GatewayUnavailable] * 3)
        self.assertEqual(
            [type(r) for r in await asyncio.wait_for(in_flight, timeout=0.5)], [GatewayUnavailable] * 2
        )
        self.assertEqual(self.mock_client.drain_publishes(), [])
        self.mock_client.failures.clear()
        send_task = asyncio.create_task(driver.send(_MockCommand()))
        batch = _rpc_params(await self.mock_client.wait_for_publish(topic))
        self.assertEqual((batch["function"], batch["address"]), (16, 1410))
        self._deliver_reply(driver, 5, 0x0200)
        self.assertNotIsInstance(await asyncio.wait_for(send_task, timeout=1.0), WbGatewayTransmissionError)

    async def test_first_batch_after_reconnect_follows_a_queue_reset_from_slot_zero(self):
        """The queue indices advance before the outage. After the reconnect the next command
        is preceded by the pointer reset and written to slot 0."""
        driver = await self._make_driver()
        topic = self._rpc_topic(driver)
        pre = asyncio.create_task(driver.send_commands([_MockCommand() for _ in range(5)]))
        await self.mock_client.wait_for_publish(topic)
        for slot in range(5):
            self._deliver_reply(driver, slot, 0x0200)
        await pre
        await self._fail_message_stream()
        await self._restore_link()
        self.assertEqual(self.mock_client.drain_publishes(), [])

        send_task = asyncio.create_task(driver.send(_MockCommand()))

        await self._expect_reset_then_batch_at_slot_zero(driver)
        self._deliver_reply(driver, 0, 0x0200)
        self.assertNotIsInstance(await asyncio.wait_for(send_task, timeout=1.0), WbGatewayTransmissionError)

    async def test_standing_r_is_forgotten_on_reconnect_until_redelivered(self):
        """An `r` from before the outage is dropped at the first request; a retained `r`
        delivered later refuses commands again."""
        driver = await self._make_driver()
        await self._deliver_retained_r(driver)
        await self._fail_message_stream()
        await self._restore_link()

        send_task = asyncio.create_task(driver.send(_MockCommand()))

        await self._expect_reset_then_batch_at_slot_zero(driver)
        self.assertFalse(driver.gateway_unavailable)
        self._deliver_reply(driver, 0, 0x0200)
        self.assertNotIsInstance(await asyncio.wait_for(send_task, timeout=1.0), WbGatewayTransmissionError)

        await self._deliver_retained_r(driver)
        self.assertIsInstance(
            await asyncio.wait_for(driver.send(_MockCommand()), timeout=0.5), GatewayUnavailable
        )
        self.assertEqual(self.mock_client.drain_publishes(), [])

    async def test_r_redelivered_on_resubscribe_before_the_first_request_stands(self):
        """An `r` redelivered on resubscribe before the first request belongs to the new
        session: the request is refused."""
        driver = await self._make_driver()
        await self._deliver_retained_r(driver)
        await self._fail_message_stream()
        await self._restore_link()
        await self._deliver_retained_r(driver)

        result = await asyncio.wait_for(driver.send(_MockCommand()), timeout=0.5)

        self.assertIsInstance(result, GatewayUnavailable)
        self.assertTrue(driver.gateway_unavailable)
        self.assertEqual(self.mock_client.drain_publishes(), [])

    async def test_initialize_during_outage_completes_and_resyncs_on_reconnect(self):
        """A driver initialised during an outage raises nothing; after the reconnect the first
        command is preceded by the queue reset."""
        await self._fail_message_stream()
        driver = WBDALIDriver(WBDALIConfig(), self.dispatcher, self.logger)

        await driver.initialize()

        self.assertEqual(self.mock_client.drain_publishes(), [])
        self.assertIsInstance(
            await asyncio.wait_for(driver.send(_MockCommand()), timeout=0.5), GatewayUnavailable
        )
        await self._restore_link()
        send_task = asyncio.create_task(driver.send(_MockCommand()))
        await self._expect_reset_then_batch_at_slot_zero(driver)
        self.assertFalse(driver.gateway_unavailable)
        send_task.cancel()
        await driver.deinitialize()

    async def test_fresh_r_after_a_stale_one_fails_in_flight_at_once(self):
        """A stale `r` let the commands out after the reconnect; a fresh `r` fails the ones in
        flight at once, not by the response timeout."""
        driver = await self._make_driver()
        await self._deliver_retained_r(driver)
        await self._fail_message_stream()
        await self._restore_link()
        in_flight = asyncio.create_task(driver.send_commands([_MockCommand() for _ in range(3)]))
        await self._expect_reset_then_batch_at_slot_zero(driver)

        await self._deliver_retained_r(driver)

        results = await asyncio.wait_for(in_flight, timeout=0.5)
        self.assertEqual([type(r) for r in results], [GatewayUnavailable] * 3)

    async def test_reconnect_with_a_half_built_batch_restarts_it_at_slot_zero(self):
        """The sender holds a batch it has already counted when the link is lost and restored:
        the batch goes to slot 0 whole, the next one right after it."""
        driver = await self._make_driver()
        topic = self._rpc_topic(driver)
        pre = asyncio.create_task(driver.send(_MockCommand()))
        await self.mock_client.wait_for_publish(topic)
        self._deliver_reply(driver, 0, 0x0200)
        await pre
        with patch("wb.mqtt_dali.wbdali.WAIT_COMMANDS_FOR_BATCH_TIMEOUT_S", 10.0):
            held = asyncio.create_task(driver.send(_MockCommand()))
            await asyncio.sleep(0.05)
        await self._fail_message_stream()
        await self._restore_link()

        joined = asyncio.create_task(driver.send(_MockCommand()))

        await self._expect_reset_then_batch_at_slot_zero(driver)
        self._deliver_reply(driver, 0, 0x0200)
        self._deliver_reply(driver, 1, 0x0200)
        for task in (held, joined):
            self.assertNotIsInstance(await asyncio.wait_for(task, timeout=1.0), WbGatewayTransmissionError)
        following = asyncio.create_task(driver.send(_MockCommand()))
        batch = _rpc_params(await self.mock_client.wait_for_publish(topic))
        self.assertEqual((batch["function"], batch["address"]), (16, 1404))
        self._deliver_reply(driver, 2, 0x0200)
        self.assertNotIsInstance(await asyncio.wait_for(following, timeout=1.0), WbGatewayTransmissionError)

    async def test_full_queue_after_reconnect_is_numbered_from_slot_zero_without_drift(self):
        """Counters stand mid-queue at the reconnect; a queue_size batch then fills slots 0..15 in
        two writes and every reply lands."""
        driver = await self._make_driver()
        topic = self._rpc_topic(driver)
        size = driver.config.queue_size
        pre = asyncio.create_task(driver.send_commands([_MockCommand() for _ in range(size // 2)]))
        await self.mock_client.wait_for_publish(topic)
        for slot in range(size // 2):
            self._deliver_reply(driver, slot, 0x0200)
        await pre
        await self._fail_message_stream()
        await self._restore_link()

        send_task = asyncio.create_task(driver.send_commands([_MockCommand() for _ in range(size)]))

        await self._expect_reset_then_batch_at_slot_zero(driver)
        second = _rpc_params(await self.mock_client.wait_for_publish(topic))
        self.assertEqual((second["function"], second["address"]), (16, 1400 + size))
        for slot in range(size):
            self._deliver_reply(driver, slot, 0x0200)
        results = await asyncio.wait_for(send_task, timeout=1.0)
        self.assertEqual(len(results), size)
        self.assertFalse(any(isinstance(r, WbGatewayTransmissionError) for r in results))
