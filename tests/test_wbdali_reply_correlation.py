"""A gateway reply reaches only the command whose acknowledged write caused it."""

import asyncio
import json
import logging
import unittest
from typing import List, Optional, Union

import aiomqtt
from dali.command import Command, Response
from dali.frame import ForwardFrame

from wb.mqtt_dali.bus_traffic import BusTrafficItem, BusTrafficSource
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.mqtt_rpc_client import MQTTRPCClient
from wb.mqtt_dali.wbdali import FramePriority, WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import (
    GatewayWriteFailed,
    NoResponseFromGateway,
    TransmissionCancelled,
)

from ._broker_link_helpers import RecordingClient
from ._port_load_helpers import (
    DEVICE_SETUP_FAILED_ERROR,
    RPC_REQUEST_TIMEOUT_ERROR,
    SERIAL_PROTOCOL_ERROR,
    is_queue_reset,
    port_load_reply,
    run_dispatcher,
    wait_for_batch,
)
from ._virtual_time import VirtualTimeTestCase

BUS_LOGGER_NAME = "wb-dali_1_bus_1"

STATUS_WITHOUT_RESPONSE = 0x0200
STATUS_BACKWARD_FRAME_55 = 0x0155


def _command() -> Command:
    return Command(ForwardFrame(16, [0x12, 0x34]))


def _params(payload: str) -> dict:
    return json.loads(payload)["params"]


def _warnings(records: List[logging.LogRecord]) -> List[str]:
    return [r.getMessage() for r in records if r.levelno >= logging.WARNING]


class TestReplyCorrelation(VirtualTimeTestCase):
    def setUp(self):
        self.client = RecordingClient()
        self.dispatcher = MQTTDispatcher(self.client)
        self.logger = logging.getLogger(BUS_LOGGER_NAME)
        self.rpc = MQTTRPCClient()
        self.driver = WBDALIDriver(WBDALIConfig(), self.dispatcher, self.logger, self.rpc)
        self.topic = ""

    async def asyncSetUp(self):
        run_dispatcher(self, self.dispatcher)
        await self.driver.initialize()
        self.addAsyncCleanup(self.driver.deinitialize)
        self.topic = f"/rpc/v1/wb-mqtt-serial/port/Load/{self.rpc.client_id}"
        self.client.drain_publishes()

    async def test_reply_before_the_write_ack_is_dropped_and_the_one_after_it_taken(self):
        """Every slot republishes an old value before the reset and the batch are acknowledged:
        the commands get only the replies after the ack, nothing above DEBUG is logged."""
        self.client.auto_ack_port_load = False
        size = self.driver.config.queue_size
        with self.assertNoLogs(BUS_LOGGER_NAME, logging.WARNING):
            task = self._send(3)
            reset = await self.client.wait_for_publish(self.topic)
            self._reply_all(size, STATUS_BACKWARD_FRAME_55)
            self._ack(reset)
            batch = await self.client.wait_for_publish(self.topic)
            self._reply_all(size, STATUS_BACKWARD_FRAME_55)
            self._ack(batch)
            for slot in range(3):
                self._reply(slot, STATUS_WITHOUT_RESPONSE)

            results = await asyncio.wait_for(task, 1.0)

        self.assertEqual([r.raw_value for r in results], [None] * 3)

    async def test_empty_reply_payloads_at_startup_are_dropped(self):
        """wb-mqtt-serial starts and empties every reply topic before the first write: nothing above
        DEBUG is logged and the command gets its own reply."""
        with self.assertNoLogs(BUS_LOGGER_NAME, logging.WARNING):
            for slot in range(self.driver.config.queue_size):
                self._reply(slot, "")
            task = self._send(1)
            await wait_for_batch(self.client, self.topic)
            self._reply(0, STATUS_BACKWARD_FRAME_55)
            result = (await asyncio.wait_for(task, 20.0))[0]

        self.assertEqual(result.raw_value.as_integer, 0x55)

    async def test_reply_after_the_timeout_reaches_no_command_and_is_warned_once(self):
        """A command times out; its reply comes before the queue reset is acknowledged and again
        before the next write's ack: no command takes it, one WARNING about it."""
        self.assertIsInstance((await asyncio.wait_for(self._send(1), 20.0))[0], NoResponseFromGateway)
        self.client.drain_publishes()
        self.client.auto_ack_port_load = False

        with self.assertLogs(BUS_LOGGER_NAME, logging.WARNING) as logs:
            task = self._send(1)
            reset = await self.client.wait_for_publish(self.topic)
            self.assertTrue(is_queue_reset(reset))
            self._reply(0, STATUS_BACKWARD_FRAME_55)
            self._ack(reset)
            batch = await self.client.wait_for_publish(self.topic)
            self._reply(0, STATUS_BACKWARD_FRAME_55)
            self._ack(batch)
            self._reply(0, STATUS_WITHOUT_RESPONSE)

            self.assertIsNone((await asyncio.wait_for(task, 1.0))[0].raw_value)

        self.assertEqual(
            sum("reply came after the timeout" in message for message in _warnings(logs.records)), 1
        )

    async def test_queue_reset_ensures_the_configured_device_and_the_batch_does_not(self):
        """The first send: the pointer reset carries `ensure: configured_device`, the batch none."""
        task = self._send(1)
        reset = _params(await self.client.wait_for_publish(self.topic))
        batch = _params(await self.client.wait_for_publish(self.topic))
        self._reply(0, STATUS_WITHOUT_RESPONSE)
        await asyncio.wait_for(task, 1.0)

        self.assertEqual((reset["function"], reset.get("ensure")), (6, "configured_device"))
        self.assertEqual((batch["function"], batch.get("ensure")), (16, None))

    async def test_rpc_request_timeout_fails_the_batch_and_the_next_one_takes_its_slots(self):
        """wb-mqtt-serial drops a batch by `total_timeout`: it fails with a DEBUG record, the next
        batch goes to its slots without a reset, the commands written before get their replies."""
        written = self._send(2)
        await wait_for_batch(self.client, self.topic)
        self.client.auto_ack_port_load = False

        with self.assertLogs(BUS_LOGGER_NAME, logging.DEBUG) as logs:
            failed = self._send(3)
            batch = await self.client.wait_for_publish(self.topic)
            self.assertEqual(_params(batch)["address"], 1404)
            self._ack(batch, RPC_REQUEST_TIMEOUT_ERROR)
            failed_results = await asyncio.wait_for(failed, 1.0)
        self.assertEqual([str(r) for r in failed_results], ["gateway write skipped: serial port busy"] * 3)
        self.assertIn(
            (logging.DEBUG, "Gateway write failed (RPC request timeout)"),
            [(r.levelno, r.getMessage()) for r in logs.records],
        )

        following = self._send(1)
        next_batch = await self.client.wait_for_publish(self.topic)
        self.assertEqual((_params(next_batch)["function"], _params(next_batch)["address"]), (16, 1404))
        self._ack(next_batch)
        for slot in range(3):
            self._reply(slot, STATUS_WITHOUT_RESPONSE)
        self.assertEqual([r.raw_value for r in await asyncio.wait_for(written, 1.0)], [None, None])
        self.assertIsNone((await asyncio.wait_for(following, 1.0))[0].raw_value)

    async def test_serial_protocol_error_resets_the_queue_before_the_next_batch(self):
        """A batch behind a pending one gets `Serial protocol error`: both fail, the next batch goes
        to slot 0 after a reset with no WARNING of its own; replies in the failed slots reach no one."""
        await self._pass_initial_reset()
        written = self._send(2)
        await wait_for_batch(self.client, self.topic)
        self.client.auto_ack_port_load = False

        failed = self._send(2)
        batch = await self.client.wait_for_publish(self.topic)
        self.assertEqual(_params(batch)["address"], 1406)
        self._ack(batch, SERIAL_PROTOCOL_ERROR)
        self.assertEqual(
            [str(r) for r in await asyncio.wait_for(failed, 1.0)],
            ["gateway write failed"] * 2,
        )

        with self.assertNoLogs(BUS_LOGGER_NAME, logging.WARNING):
            following = self._send(1)
            reset = await self.client.wait_for_publish(self.topic)
            self.assertTrue(is_queue_reset(reset))
            self.assertEqual(
                [type(r) for r in await asyncio.wait_for(written, 1.0)], [NoResponseFromGateway] * 2
            )
            self._reply(3, STATUS_BACKWARD_FRAME_55)
            self._ack(reset)
            batch = await self.client.wait_for_publish(self.topic)
            self.assertEqual(_params(batch)["address"], 1400)
            self._ack(batch)
            self._reply(3, STATUS_BACKWARD_FRAME_55)
            self._reply(0, STATUS_WITHOUT_RESPONSE)
            self.assertIsNone((await asyncio.wait_for(following, 1.0))[0].raw_value)

    async def test_a_long_wait_for_the_write_ack_does_not_time_out_the_reply(self):
        """The ack comes later than the reply timeout after the write: the reply still counts."""
        self.driver.config.response_timeout_s = 1.0
        self.client.auto_ack_port_load = False
        task = self._send(1)
        self._ack(await self.client.wait_for_publish(self.topic))
        batch = await self.client.wait_for_publish(self.topic)

        await asyncio.sleep(2.0)
        self._ack(batch)
        self._reply(0, STATUS_WITHOUT_RESPONSE)

        self.assertIsNone((await asyncio.wait_for(task, 1.0))[0].raw_value)

    async def test_missing_ack_is_a_write_error_and_a_late_ack_arms_no_slot(self):
        """No ack in time: the batch fails and the next one goes to slot 0 after a reset; the late ack
        and the reply after it reach no command."""
        await self._pass_initial_reset()
        self.client.auto_ack_port_load = False
        with self.assertLogs(BUS_LOGGER_NAME, logging.WARNING) as logs:
            unacked = self._send(1)
            batch = await self.client.wait_for_publish(self.topic)
            self.assertIs(type((await asyncio.wait_for(unacked, 20.0))[0]), GatewayWriteFailed)
        self._ack(batch)
        self._reply(1, STATUS_BACKWARD_FRAME_55)

        following = self._send(1)
        reset = await self.client.wait_for_publish(self.topic)
        self.assertTrue(is_queue_reset(reset))
        self._ack(reset)
        next_batch = await self.client.wait_for_publish(self.topic)
        self.assertEqual(_params(next_batch)["address"], 1400)
        self._ack(next_batch)
        self._reply(0, STATUS_WITHOUT_RESPONSE)
        self.assertIsNone((await asyncio.wait_for(following, 1.0))[0].raw_value)

        self.assertTrue(any("Gateway write failed" in message for message in _warnings(logs.records)))

    async def test_a_cancelled_command_already_written_keeps_its_slot(self):
        """The caller of a written command is cancelled: the queue wraps around to its slot and
        waits for its reply before writing there."""
        await self._pass_initial_reset()
        cancelled = self._send(1)
        self.assertEqual(_params(await wait_for_batch(self.client, self.topic))["address"], 1402)
        cancelled.cancel()

        following = self._send(self.driver.config.queue_size)
        self.assertEqual(_params(await wait_for_batch(self.client, self.topic))["address"], 1404)
        self.assertEqual(_params(await wait_for_batch(self.client, self.topic))["address"], 1400)
        await asyncio.sleep(1.0)
        self.assertEqual([p for p in self.client.drain_publishes() if p.topic == self.topic], [])

        self._reply(1, STATUS_WITHOUT_RESPONSE)
        self.assertEqual(_params(await wait_for_batch(self.client, self.topic))["address"], 1402)
        await asyncio.wait_for(following, 20.0)

    async def test_a_cancelled_command_not_yet_written_is_not_written(self):
        """The caller is cancelled while its command waits to be batched: the command is not
        written and the next command takes its slot."""
        await self._pass_initial_reset()
        cancelled = self._send(1)
        for _ in range(3):
            await asyncio.sleep(0)
        cancelled.cancel()

        following = self._send(1)
        batch = _params(await wait_for_batch(self.client, self.topic))
        self.assertEqual((batch["address"], batch["count"]), (1402, 2))
        self._reply(1, STATUS_WITHOUT_RESPONSE)
        self.assertIsNone((await asyncio.wait_for(following, 20.0))[0].raw_value)

    async def test_bus_traffic_goes_on_after_a_dropped_command(self):
        """A command is dropped from its batch after its caller is cancelled: the listeners get it
        as cancelled, and the next command right after it."""
        traffic: List[BusTrafficItem] = []
        self.driver.bus_traffic.register(traffic.append)
        await self._pass_initial_reset()
        cancelled = self._send(1)
        for _ in range(3):
            await asyncio.sleep(0)
        cancelled.cancel()

        following = self._send(1)
        await wait_for_batch(self.client, self.topic)
        self._reply(1, STATUS_WITHOUT_RESPONSE)
        await asyncio.wait_for(following, 20.0)

        self.assertEqual([type(item.response) for item in traffic[1:]], [TransmissionCancelled, Response])

    async def test_a_command_cancelled_during_the_queue_reset_is_not_written(self):
        """The caller is cancelled while the queue reset before its batch waits for its ack: the
        command is not written and the next command goes to slot 0."""
        self.assertIsInstance((await asyncio.wait_for(self._send(1), 20.0))[0], NoResponseFromGateway)
        self.client.drain_publishes()
        self.client.auto_ack_port_load = False
        cancelled = self._send(1)
        reset = await self.client.wait_for_publish(self.topic)
        self.assertTrue(is_queue_reset(reset))
        cancelled.cancel()
        self._ack(reset)

        self.client.auto_ack_port_load = True
        following = self._send(1)
        batch = _params(await wait_for_batch(self.client, self.topic))
        self.assertEqual((batch["address"], batch["count"]), (1400, 2))
        self._reply(0, STATUS_WITHOUT_RESPONSE)
        self.assertIsNone((await asyncio.wait_for(following, 20.0))[0].raw_value)

    async def test_failed_queue_reset_fails_the_batch_and_is_sent_again(self):
        """The pointer reset is answered with an error: its batch fails, the next send resets
        again and writes to slot 0."""
        self.client.auto_ack_port_load = False
        failed = self._send(1)
        self._ack(await self.client.wait_for_publish(self.topic), DEVICE_SETUP_FAILED_ERROR)
        self.assertEqual(str((await asyncio.wait_for(failed, 1.0))[0]), "gateway write failed")

        self.client.auto_ack_port_load = True
        following = self._send(1)
        self.assertTrue(is_queue_reset(await self.client.wait_for_publish(self.topic)))
        self.assertEqual(_params(await self.client.wait_for_publish(self.topic))["address"], 1400)
        self._reply(0, STATUS_WITHOUT_RESPONSE)
        self.assertIsNone((await asyncio.wait_for(following, 1.0))[0].raw_value)

    async def test_reset_after_a_timeout_drops_the_later_batch_and_its_slots_stay_unarmed(self):
        """A command times out with a later batch still waiting: that batch fails at the reset;
        replies in its slots reach no command and raise no WARNING of their own."""
        await self._pass_initial_reset()
        timed_out = self._send(1)
        await wait_for_batch(self.client, self.topic)
        dropped = self._send(2)
        self.assertEqual(_params(await wait_for_batch(self.client, self.topic))["address"], 1404)
        self.assertIsInstance((await asyncio.wait_for(timed_out, 20.0))[0], NoResponseFromGateway)
        self.client.auto_ack_port_load = False

        with self.assertLogs(BUS_LOGGER_NAME, logging.WARNING) as logs:
            following = self._send(3)
            reset = await self.client.wait_for_publish(self.topic)
            self.assertTrue(is_queue_reset(reset))
            self.assertEqual(
                [type(r) for r in await asyncio.wait_for(dropped, 1.0)], [NoResponseFromGateway] * 2
            )
            self._reply(2, STATUS_BACKWARD_FRAME_55)
            self._ack(reset)
            batch = await self.client.wait_for_publish(self.topic)
            self.assertEqual(_params(batch)["address"], 1400)
            self._ack(batch)
            self._reply(3, STATUS_BACKWARD_FRAME_55)
            for slot in range(3):
                self._reply(slot, STATUS_WITHOUT_RESPONSE)
            results = await asyncio.wait_for(following, 1.0)

        self.assertEqual([r.raw_value for r in results], [None] * 3)
        self.assertEqual(_warnings(logs.records), ["Gateway did not reply, queue reset"])

    async def test_reply_in_a_timed_out_slot_after_the_reset_ack_is_dropped_quietly(self):
        """A command times out; a reply in its slot after the queue reset is acknowledged reaches
        no command and gives no WARNING of its own."""
        await self._pass_initial_reset()
        self.assertIsInstance((await asyncio.wait_for(self._send(1), 20.0))[0], NoResponseFromGateway)
        self.client.drain_publishes()
        self.client.auto_ack_port_load = False

        with self.assertLogs(BUS_LOGGER_NAME, logging.WARNING) as logs:
            following = self._send(1)
            self._ack(await self.client.wait_for_publish(self.topic))
            batch = await self.client.wait_for_publish(self.topic)
            self._reply(1, STATUS_BACKWARD_FRAME_55)
            self._ack(batch)
            self._reply(1, STATUS_BACKWARD_FRAME_55)
            self._reply(0, STATUS_WITHOUT_RESPONSE)
            self.assertIsNone((await asyncio.wait_for(following, 1.0))[0].raw_value)

        self.assertEqual(_warnings(logs.records), ["Gateway did not reply, queue reset"])

    async def test_rpc_error_text_fails_the_write_and_resets_the_queue_before_the_next_batch(self):
        """A write answered with a plain-text error fails its command with a WARNING; the next batch
        goes to slot 0 after a queue reset."""
        await self._pass_initial_reset()
        self.client.auto_ack_port_load = False
        with self.assertLogs(BUS_LOGGER_NAME, logging.WARNING) as logs:
            refused = self._send(1)
            batch = await self.client.wait_for_publish(self.topic)
            self.assertEqual(_params(batch)["address"], 1402)
            self.client.messages.deliver(
                aiomqtt.Message(
                    f"{self.topic}/reply",
                    json.dumps({"id": json.loads(batch)["id"], "error": "Port busy"}),
                    0,
                    False,
                    0,
                    None,
                )
            )
            self.assertEqual(str((await asyncio.wait_for(refused, 1.0))[0]), "gateway write failed")
        self.assertIn("Gateway write failed (Port busy)", _warnings(logs.records))

        self.client.auto_ack_port_load = True
        following = self._send(1)
        self.assertTrue(is_queue_reset(await self.client.wait_for_publish(self.topic)))
        self.assertEqual(_params(await self.client.wait_for_publish(self.topic))["address"], 1400)
        self._reply(0, STATUS_WITHOUT_RESPONSE)
        self.assertIsNone((await asyncio.wait_for(following, 1.0))[0].raw_value)

    async def test_deinitialize_while_a_batch_awaits_its_write_ack_cancels_its_commands(self):
        """The driver stops while a batch waits for its write ack: its commands get TransmissionCancelled."""
        await self._pass_initial_reset()
        self.client.auto_ack_port_load = False
        task = self._send(2)
        await self.client.wait_for_publish(self.topic)

        await self.driver.deinitialize()

        self.assertEqual([type(r) for r in await asyncio.wait_for(task, 1.0)], [TransmissionCancelled] * 2)

    async def _pass_initial_reset(self) -> None:
        """One answered command through the deferred queue reset; slot 1 is next."""
        task = self._send(1)
        await wait_for_batch(self.client, self.topic)
        self._reply(0, STATUS_WITHOUT_RESPONSE)
        await asyncio.wait_for(task, 1.0)

    def _send(self, count: int) -> asyncio.Task:
        return asyncio.create_task(
            self.driver.send_commands(
                [_command() for _ in range(count)],
                BusTrafficSource.WB,
                FramePriority.USER_ACTION,
                self.logger,
            )
        )

    def _ack(self, payload: str, error: Optional[dict] = None) -> None:
        self.client.messages.deliver(port_load_reply(self.topic, payload, error))

    def _reply(self, slot: int, status_word: Union[int, str]) -> None:
        topic = f"/devices/{self.driver.config.device_name}/controls/bus_1_bulk_send_reply_{slot}"
        self.client.messages.deliver(aiomqtt.Message(topic, str(status_word), 0, False, 0, None))

    def _reply_all(self, size: int, status_word: int) -> None:
        for slot in range(size):
            self._reply(slot, status_word)


if __name__ == "__main__":
    unittest.main()
