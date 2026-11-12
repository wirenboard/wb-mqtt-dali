import asyncio
import json
import logging
import unittest
from typing import Optional

import aiomqtt

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.mqtt_rpc_client import (
    MQTTRPCCallTimeoutError,
    MQTTRPCClient,
    MQTTRPCClientError,
    MQTTRPCError,
    MQTTRPCInvalidReplyError,
    MQTTRPCRequestTimeoutError,
    make_rpc_error,
    rpc_call,
)

from ._broker_link_helpers import RecordingClient


class MakeRpcErrorTests(unittest.TestCase):
    def test_request_timeout_codes_keep_the_reason_from_data(self):
        for code in (-32600, -32100):
            with self.subTest(code=code):
                exc = make_rpc_error(
                    {"code": code, "message": "Request timeout", "data": "RPC request timeout"}
                )
                self.assertIsInstance(exc, MQTTRPCRequestTimeoutError)
                self.assertEqual((exc.code, exc.data), (code, "RPC request timeout"))

    def test_other_codes_are_plain_rpc_errors(self):
        exc = make_rpc_error({"code": -32000, "message": "Server error", "data": "Port IO error"})
        self.assertNotIsInstance(exc, MQTTRPCRequestTimeoutError)
        self.assertEqual((exc.code, exc.rpc_message, exc.data), (-32000, "Server error", "Port IO error"))

    def test_an_error_that_is_not_an_object_becomes_its_text(self):
        exc = make_rpc_error("boom")
        self.assertIs(type(exc), MQTTRPCError)
        self.assertEqual(exc.rpc_message, "boom")


class MQTTRPCClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mqtt_client = RecordingClient()
        self.dispatcher = MQTTDispatcher(self.mqtt_client)
        self.rpc = MQTTRPCClient()
        self.request_topic = ""

    async def asyncSetUp(self):
        dispatch_task = asyncio.create_task(self.dispatcher.run())
        self.addCleanup(dispatch_task.cancel)
        await self.rpc.start(self.dispatcher)
        self.request_topic = f"/rpc/v1/drv/svc/meth/{self.rpc.client_id}"

    async def test_a_reply_with_the_call_id_returns_its_result(self):
        """A call publishes params and an id to its request topic; the reply with that id is its result."""
        call = asyncio.create_task(self.rpc.call("drv", "svc", "meth", {"a": 1}, 1.0))
        request = json.loads(await self.mqtt_client.wait_for_publish(self.request_topic))
        self.assertEqual(request["params"], {"a": 1})
        self._reply({"id": request["id"], "result": {"ok": True}})
        self.assertEqual(await call, {"ok": True})

    async def test_a_late_reply_to_an_earlier_call_is_ignored(self):
        """A reply carrying an earlier id does not complete the current call; its own id does."""
        call = asyncio.create_task(self.rpc.call("drv", "svc", "meth", {}, 1.0))
        request = json.loads(await self.mqtt_client.wait_for_publish(self.request_topic))
        self._reply({"id": request["id"] - 1, "result": "stale"})
        await asyncio.sleep(0.05)
        self.assertFalse(call.done())
        self._reply({"id": request["id"], "result": "fresh"})
        self.assertEqual(await call, "fresh")

    async def test_a_reply_with_no_call_waiting_or_retained_is_dropped(self):
        """A reply after its call timed out is dropped without an error; a retained one completes no call."""
        with self.assertRaises(MQTTRPCCallTimeoutError):
            await self.rpc.call("drv", "svc", "meth", {}, 0.05)
        request = json.loads(await self.mqtt_client.wait_for_publish(self.request_topic))
        with self.assertNoLogs(level=logging.ERROR):
            self._reply({"id": request["id"], "result": "late"})
            await asyncio.sleep(0.05)

        call = asyncio.create_task(self.rpc.call("drv", "svc", "meth", {}, 1.0))
        request = json.loads(await self.mqtt_client.wait_for_publish(self.request_topic))
        self._reply_raw(json.dumps({"id": request["id"], "result": "retained"}), retain=True)
        await asyncio.sleep(0.05)
        self.assertFalse(call.done())
        self._reply({"id": request["id"], "result": "fresh"})
        self.assertEqual(await call, "fresh")

    async def test_an_error_reply_raises_the_typed_error(self):
        """A -32600 error reply to the pending call raises MQTTRPCRequestTimeoutError."""
        call = asyncio.create_task(self.rpc.call("drv", "svc", "meth", {}, 1.0))
        request = json.loads(await self.mqtt_client.wait_for_publish(self.request_topic))
        self._reply(
            {"id": request["id"], "error": {"code": -32600, "message": "Request timeout", "data": "x"}}
        )
        with self.assertRaises(MQTTRPCRequestTimeoutError):
            await call

    async def test_an_unparsable_reply_fails_the_call_at_once(self):
        """A reply that is not JSON fails the pending call long before its timeout."""
        call = asyncio.create_task(self.rpc.call("drv", "svc", "meth", {}, 30.0))
        await self.mqtt_client.wait_for_publish(self.request_topic)
        self._reply_raw("not json")
        with self.assertRaises(MQTTRPCInvalidReplyError):
            await asyncio.wait_for(call, 1.0)

    async def test_no_reply_raises_a_call_timeout_with_a_plain_message(self):
        """No reply: MQTTRPCCallTimeoutError whose text has no empty code and data tail."""
        with self.assertRaises(MQTTRPCCallTimeoutError) as ctx:
            await self.rpc.call("drv", "svc", "meth", {}, 0.05)
        self.assertNotIn("None", str(ctx.exception))

    async def test_a_failed_publish_raises_the_mqtt_error_and_frees_the_client(self):
        """A refused publish raises aiomqtt.MqttError, not an RPC error; the next call goes through."""
        self.mqtt_client.fail("publish", self.request_topic)
        with self.assertRaises(aiomqtt.MqttError) as ctx:
            await self.rpc.call("drv", "svc", "meth", {}, 1.0)
        self.assertNotIsInstance(ctx.exception, MQTTRPCError)
        self.mqtt_client.failures.clear()
        call = asyncio.create_task(self.rpc.call("drv", "svc", "meth", {}, 1.0))
        request = json.loads(await self.mqtt_client.wait_for_publish(self.request_topic))
        self._reply({"id": request["id"], "result": "ok"})
        self.assertEqual(await asyncio.wait_for(call, 0.5), "ok")

    async def test_the_timeout_covers_waiting_for_the_previous_call(self):
        """A call queued behind an unanswered one fails within its own timeout, without publishing."""
        first = asyncio.create_task(self.rpc.call("drv", "svc", "meth", {}, 30.0))
        await self.mqtt_client.wait_for_publish(self.request_topic)
        with self.assertRaises(MQTTRPCCallTimeoutError):
            await asyncio.wait_for(self.rpc.call("drv", "svc", "meth", {}, 0.05), 1.0)
        self.assertEqual([], self.mqtt_client.publishes)
        first.cancel()

    async def test_stop_fails_the_pending_call_at_once(self):
        """stop() while a call waits for its reply fails the call long before its timeout."""
        call = asyncio.create_task(self.rpc.call("drv", "svc", "meth", {}, 30.0))
        await self.mqtt_client.wait_for_publish(self.request_topic)
        await self.rpc.stop()
        with self.assertRaises(MQTTRPCClientError):
            await asyncio.wait_for(call, 1.0)

    async def test_a_call_after_stop_fails_without_publishing(self):
        """A call on a stopped client raises MQTTRPCClientError and publishes nothing."""
        await self.rpc.stop()
        with self.assertRaises(MQTTRPCClientError):
            await self.rpc.call("drv", "svc", "meth", {}, 1.0)
        self.assertEqual([], self.mqtt_client.publishes)

    # --- Private ---

    def _reply(self, body: dict) -> None:
        self._reply_raw(json.dumps(body))

    def _reply_raw(self, payload: Optional[str], retain: bool = False) -> None:
        self.mqtt_client.messages.deliver(
            aiomqtt.Message(f"{self.request_topic}/reply", payload, 0, retain, 0, None)
        )


class RpcCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mqtt_client = RecordingClient()
        self.dispatcher = MQTTDispatcher(self.mqtt_client)

    async def asyncSetUp(self):
        dispatch_task = asyncio.create_task(self.dispatcher.run())
        self.addCleanup(dispatch_task.cancel)

    async def test_returns_the_result_and_drops_its_subscription(self):
        """rpc_call subscribes to its replies, returns the answered result, then unsubscribes."""
        call = asyncio.create_task(rpc_call("drv", "svc", "meth", {}, self.dispatcher, 1.0))
        request_topic = await self._wait_for_request()
        request = json.loads(await self.mqtt_client.wait_for_publish(request_topic))
        reply = json.dumps({"id": request["id"], "result": [1]})
        self.mqtt_client.messages.deliver(aiomqtt.Message(f"{request_topic}/reply", reply, 0, False, 0, None))
        self.assertEqual(await call, [1])
        self.assertEqual(set(), self.dispatcher.get_subscribed_topics())

    async def test_no_reply_raises_a_call_timeout_and_drops_its_subscription(self):
        """Unanswered rpc_call raises MQTTRPCCallTimeoutError and leaves no subscription behind."""
        with self.assertRaises(MQTTRPCCallTimeoutError):
            await rpc_call("drv", "svc", "meth", {}, self.dispatcher, 0.05)
        self.assertEqual(set(), self.dispatcher.get_subscribed_topics())

    # --- Private ---

    async def _wait_for_request(self) -> str:
        for _ in range(100):
            for publish in self.mqtt_client.publishes:
                if publish.topic.startswith("/rpc/v1/drv/svc/meth/"):
                    return publish.topic
            await asyncio.sleep(0.01)
        raise TimeoutError("No RPC request published")
