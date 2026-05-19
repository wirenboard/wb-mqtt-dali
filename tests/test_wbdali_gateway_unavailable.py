"""Driver-level reactions to wb-mqtt-serial's /meta/error retained signal.

Covers SOFT-6841: when wb-mqtt-serial reports the gateway device unreachable
(payload `r`), the driver fails pending and incoming traffic with
`GatewayUnavailable`; once the signal clears, the driver re-syncs the gateway
queue and resumes normal operation.
"""

import asyncio
import logging
import unittest
from typing import List, Optional
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
from dali.command import Command, Response
from dali.frame import ForwardFrame

from wb.mqtt_dali.bus_traffic import BusTrafficItem
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import (
    GatewayUnavailable,
    WbGatewayTransmissionError,
)


class _MockMqttClient:
    def __init__(self):
        self._client = MagicMock()
        self._client._client_id = "test-gw-unavail-client"
        self._messages_to_broker: asyncio.Queue = asyncio.Queue()

    async def subscribe(self, _topic: str) -> None:
        return None

    async def unsubscribe(self, _topic: str) -> None:
        return None

    async def publish(self, topic: str, payload: str) -> None:
        await self._messages_to_broker.put((topic, payload))

    async def drain_publishes(self) -> List[tuple]:
        items: List[tuple] = []
        while not self._messages_to_broker.empty():
            items.append(self._messages_to_broker.get_nowait())
        return items

    async def wait_for_publish(self, topic: str, timeout: float = 1.0) -> str:
        while True:
            try:
                pub_topic, payload = await asyncio.wait_for(self._messages_to_broker.get(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"Timeout waiting for publish to {topic}") from exc
            if pub_topic == topic:
                return payload


class _MockCommand(Command):
    def __init__(self, data=None, response_class: Optional[type] = None):
        super().__init__(ForwardFrame(16, data or [0x12, 0x34]))
        self.sendtwice = False
        self.response = response_class

    def __str__(self):
        return "_MockCommand"


def _meta_error_topic(device_name: str) -> str:
    return f"/devices/{device_name}/meta/error"


def _build_retained_msg(topic: str, payload: bytes) -> mqtt.MQTTMessage:
    msg = mqtt.MQTTMessage(topic=topic.encode())
    msg.payload = payload
    msg.retain = True
    return msg


class TestGatewayUnavailable(unittest.IsolatedAsyncioTestCase):
    """Tests for the gateway-availability feature in WBDALIDriver."""

    def setUp(self):
        self.config = WBDALIConfig()
        self.mock_client = _MockMqttClient()
        self.dispatcher = MQTTDispatcher(self.mock_client)
        self.logger = MagicMock(spec=logging.Logger)

    async def _make_driver(self) -> WBDALIDriver:
        driver = WBDALIDriver(self.config, self.dispatcher, self.logger)
        await driver.initialize()
        await self.mock_client.drain_publishes()
        return driver

    def _deliver_meta_error(self, driver: WBDALIDriver, payload: bytes) -> None:
        # pylint: disable=protected-access
        topic = _meta_error_topic(driver.config.device_name)
        msg = _build_retained_msg(topic, payload)
        self.dispatcher._dispatch_message(msg)

    def _deliver_reply(self, driver: WBDALIDriver, slot: int, status_word: int) -> None:
        # pylint: disable=protected-access
        reply = mqtt.MQTTMessage(
            topic=f"/devices/{driver.config.device_name}/controls/"
            f"bus_{driver.config.bus}_bulk_send_reply_{slot}".encode()
        )
        reply.payload = str(status_word).encode()
        self.dispatcher._dispatch_message(reply)

    async def _wait_gateway_state(self, driver: WBDALIDriver, expected: bool) -> None:
        for _ in range(100):
            if driver.gateway_unavailable is expected:
                return
            await asyncio.sleep(0.01)
        self.fail(f"driver.gateway_unavailable did not become {expected}")

    async def test_pending_send_resolved_on_r(self):
        """Pending futures resolve as GatewayUnavailable with bus-traffic notifications."""
        driver = await self._make_driver()

        traffic: List[BusTrafficItem] = []
        driver.bus_traffic.register(traffic.append)

        cmd = _MockCommand()
        send_task = asyncio.create_task(driver.send(cmd))
        # Let the queue_sender pick up the item and publish to the gateway,
        # so it ends up in _waiting_for_responses (pending response).
        await self.mock_client.wait_for_publish(f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}")

        self._deliver_meta_error(driver, b"r")
        await self._wait_gateway_state(driver, True)

        result = await asyncio.wait_for(send_task, timeout=1.0)
        self.assertIsInstance(result, GatewayUnavailable)

        self.assertTrue(traffic, "bus-traffic notification expected for resolved pending send")
        self.assertTrue(
            any(isinstance(item.response, GatewayUnavailable) for item in traffic),
            "expected at least one bus-traffic entry with GatewayUnavailable",
        )

    async def test_new_send_fails_fast_while_r(self):
        """New sends short-circuit with GatewayUnavailable and never publish to wb-mqtt-serial."""
        driver = await self._make_driver()

        self._deliver_meta_error(driver, b"r")
        await self._wait_gateway_state(driver, True)

        result = await asyncio.wait_for(driver.send(_MockCommand()), timeout=0.5)
        self.assertIsInstance(result, GatewayUnavailable)
        self.assertEqual(str(result), "gateway unavailable")
        with self.assertRaises(RuntimeError):
            _ = result.raw_value
        with self.assertRaises(RuntimeError):
            _ = result.value

        # No Modbus RPC publish should have been emitted while unavailable.
        published = await self.mock_client.drain_publishes()
        self.assertFalse(
            any(t.startswith("/rpc/v1/wb-mqtt-serial/port/Load/") for t, _ in published),
            f"unexpected gateway traffic while unavailable: {published}",
        )

    async def test_send_works_after_null(self):
        """After r → empty, send() returns a normal Response from the gateway."""
        driver = await self._make_driver()

        self._deliver_meta_error(driver, b"r")
        await self._wait_gateway_state(driver, True)

        self._deliver_meta_error(driver, b"")
        await self._wait_gateway_state(driver, False)

        # Recovery is lazy: no publish until the next send. Sending a command
        # emits two RPCs in order — first the deferred queue-reset, then the batch.
        cmd = _MockCommand()
        send_task = asyncio.create_task(driver.send(cmd))
        topic = f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        await self.mock_client.wait_for_publish(topic)  # lazy queue reset
        await self.mock_client.wait_for_publish(topic)  # batch publish

        # status 2: transmission without response
        self._deliver_reply(driver, slot=0, status_word=0x0200)

        result = await asyncio.wait_for(send_task, timeout=1.0)
        self.assertIsInstance(result, Response)
        self.assertNotIsInstance(result, WbGatewayTransmissionError)

    async def test_queue_counters_resync_after_null(self):
        """Recovery restores counters so a full queue_size batch can be sent without drift.

        Advances internal counters before the outage by sending a few commands, then
        triggers r → empty. After recovery, the public batch_start_index must be back at
        0 and sending queue_size commands in one shot must produce queue_size normal
        responses (replies at indices 0..queue_size-1).
        """
        driver = await self._make_driver()

        size = driver.config.queue_size
        pre_count = max(1, size // 2)
        pre_cmds = [_MockCommand() for _ in range(pre_count)]
        pre_task = asyncio.create_task(driver.send_commands(pre_cmds))
        await self.mock_client.wait_for_publish(f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}")
        for i in range(pre_count):
            self._deliver_reply(driver, slot=i, status_word=0x0200)
        await pre_task
        # After pre-sends, batch_start_index has advanced (or wrapped); we don't assert
        # the exact value, only that recovery brings it back to 0.

        self._deliver_meta_error(driver, b"r")
        await self._wait_gateway_state(driver, True)
        self._deliver_meta_error(driver, b"")
        await self._wait_gateway_state(driver, False)

        # Counters are zeroed in the callback itself; the matching gateway-side
        # reset is deferred to the next batch (lazy resync).
        self.assertEqual(driver.batch_start_index, 0)

        cmds = [_MockCommand(data=[i & 0xFF, (i + 1) & 0xFF]) for i in range(size)]
        send_task = asyncio.create_task(driver.send_commands(cmds))
        topic = f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        await self.mock_client.wait_for_publish(topic)  # lazy queue reset
        await self.mock_client.wait_for_publish(topic)  # batch publish
        for i in range(size):
            self._deliver_reply(driver, slot=i, status_word=0x0200)

        results = await asyncio.wait_for(send_task, timeout=2.0)
        self.assertEqual(len(results), size)
        for r in results:
            self.assertNotIsInstance(r, WbGatewayTransmissionError)

    async def test_bus_traffic_resumes_immediately_after_recovery(self):
        """Bus-traffic listeners receive post-recovery commands without a gap.

        Recovery resets the Modbus queue positions but must keep the monotonic
        sequence_id counter (`_send_queue_item_index`) in sync with
        `BusTrafficCallbacks._last_item_sequence_id`. If the counter were ever
        rewound on recovery, the next `notify_command` would land in the
        buffer branch and the listener would never be called.
        """
        driver = await self._make_driver()

        # Advance the pre-r sequence counter by sending and replying to a few commands.
        pre_count = 3
        pre_task = asyncio.create_task(driver.send_commands([_MockCommand() for _ in range(pre_count)]))
        await self.mock_client.wait_for_publish(f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}")
        for i in range(pre_count):
            self._deliver_reply(driver, slot=i, status_word=0x0200)
        await pre_task

        # Outage + recovery.
        self._deliver_meta_error(driver, b"r")
        await self._wait_gateway_state(driver, True)
        self._deliver_meta_error(driver, b"")
        await self._wait_gateway_state(driver, False)

        # Register a listener after recovery and send a single command; without
        # `_send_queue_item_index` continuity, the resulting notify_command would
        # fall into the buffer-branch and the listener would never be called.
        traffic: List[BusTrafficItem] = []
        driver.bus_traffic.register(traffic.append)

        send_task = asyncio.create_task(driver.send(_MockCommand()))
        topic = f"/rpc/v1/wb-mqtt-serial/port/Load/{driver.rpc_client_id}"
        await self.mock_client.wait_for_publish(topic)  # lazy queue reset
        await self.mock_client.wait_for_publish(topic)  # batch publish
        self._deliver_reply(driver, slot=0, status_word=0x0200)
        await asyncio.wait_for(send_task, timeout=1.0)

        self.assertEqual(
            len(traffic),
            1,
            f"expected exactly one bus-traffic entry post-recovery, got {traffic}",
        )

    async def test_meta_error_other_values_ignored(self):
        """Payloads other than `r` and empty must not toggle the flag."""
        driver = await self._make_driver()

        for payload in (b"p", b"w", b"x"):
            self._deliver_meta_error(driver, payload)
            await asyncio.sleep(0.02)
            self.assertFalse(driver.gateway_unavailable, f"flag flipped on {payload!r}")

    async def test_repeated_r_null_idempotent(self):
        """r→r→empty and r→empty→empty are both safe; duplicate signals are no-ops."""
        driver = await self._make_driver()

        self._deliver_meta_error(driver, b"r")
        await self._wait_gateway_state(driver, True)
        self._deliver_meta_error(driver, b"r")  # duplicate
        await asyncio.sleep(0.02)
        self.assertTrue(driver.gateway_unavailable)

        self._deliver_meta_error(driver, b"")
        await self._wait_gateway_state(driver, False)

        self._deliver_meta_error(driver, b"")  # duplicate empty
        await asyncio.sleep(0.02)
        self.assertFalse(driver.gateway_unavailable)
        # Recovery is lazy — no publishes should have been emitted at all
        # while the driver sat idle between transitions.
        leftover = await self.mock_client.drain_publishes()
        self.assertEqual(leftover, [])

    async def test_retained_r_replayed_to_late_driver(self):
        """Retained `r` marks driver unavailable; late-subscribing sibling sees it via the dispatcher cache.

        Covers the multi-bus portion of plan S2: the first driver to subscribe to
        /meta/error receives the broker's retained delivery; a second driver
        subscribing afterwards must also observe the unavailable state (the
        dispatcher caches the retained payload and replays it).
        """
        driver_first = WBDALIDriver(WBDALIConfig(bus=1), self.dispatcher, self.logger)
        await driver_first.initialize()
        await self.mock_client.drain_publishes()

        # Broker delivers retained `r` to the only subscriber so far.
        self._deliver_meta_error(driver_first, b"r")
        await self._wait_gateway_state(driver_first, True)

        result = await asyncio.wait_for(driver_first.send(_MockCommand()), timeout=0.5)
        self.assertIsInstance(result, GatewayUnavailable)

        # A second bus on the same gateway initialises and subscribes later — the
        # dispatcher must replay the cached retained payload to its callback.
        driver_late = WBDALIDriver(WBDALIConfig(bus=2), self.dispatcher, self.logger)
        await driver_late.initialize()
        await self.mock_client.drain_publishes()
        await self._wait_gateway_state(driver_late, True)

        result_late = await asyncio.wait_for(driver_late.send(_MockCommand()), timeout=0.5)
        self.assertIsInstance(result_late, GatewayUnavailable)

        # Recovery returns both drivers to a working state.
        self._deliver_meta_error(driver_first, b"")
        await self._wait_gateway_state(driver_first, False)
        await self._wait_gateway_state(driver_late, False)

    async def test_two_gateways_independent(self):
        """`r` on one gateway's /meta/error does not affect a driver belonging to another gateway."""
        cfg_a = WBDALIConfig(device_name="wb-dali_A", bus=1)
        cfg_b = WBDALIConfig(device_name="wb-dali_B", bus=1)

        driver_a = WBDALIDriver(cfg_a, self.dispatcher, self.logger)
        driver_b = WBDALIDriver(cfg_b, self.dispatcher, self.logger)
        await driver_a.initialize()
        await driver_b.initialize()
        await self.mock_client.drain_publishes()

        self._deliver_meta_error(driver_a, b"r")
        await self._wait_gateway_state(driver_a, True)

        self.assertFalse(driver_b.gateway_unavailable)
        # A send via driver_b still flows through to the gateway publish.
        send_b = asyncio.create_task(driver_b.send(_MockCommand()))
        await self.mock_client.wait_for_publish(f"/rpc/v1/wb-mqtt-serial/port/Load/{driver_b.rpc_client_id}")
        send_b.cancel()
        try:
            await send_b
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    unittest.main()
