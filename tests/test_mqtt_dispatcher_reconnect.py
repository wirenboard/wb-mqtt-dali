"""MQTTDispatcher across a lost broker session: subscriptions and the retained mirror survive."""

import asyncio
import unittest

import aiomqtt

from wb.mqtt_dali.mqtt_dispatcher import BrokerDisconnectedError, MQTTDispatcher

from ._broker_link_helpers import Inbox, Publish, RecordingClient


def _message(topic):
    return aiomqtt.Message(topic, b"x", 0, False, 0, None)


class DispatcherReconnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = RecordingClient()
        self.dispatcher = MQTTDispatcher(self.client)

    async def _run_session(self, messages):
        """Deliver one session's messages, then drop the link the way the service does: the
        stream fails and the session owner reports the loss."""
        self.client.messages = Inbox(messages)
        self.client.messages.fail()
        with self.assertRaises(aiomqtt.MqttError):
            await self.dispatcher.run()
        self.dispatcher.connection_lost()

    async def _publish_retained(self, topic, payload, qos=2):
        await self.dispatcher.publish(topic, payload, qos=qos, retain=True)

    async def test_every_topic_is_resubscribed_and_still_routed_after_the_stream_fails(self):
        """Three subscribed topics; after the stream fails and the link is restored each is
        subscribed again and still routed."""
        received = {topic: [] for topic in ("a/1", "b/2", "c/3")}
        for topic, messages in received.items():
            await self.dispatcher.subscribe(topic, messages.append)
        await self._run_session([])
        self.assertFalse(self.dispatcher.connected)
        self.client.calls.clear()

        await self.dispatcher.connection_restored()

        self.assertEqual(self.client.calls, [("subscribe", topic) for topic in received])
        self.assertTrue(self.dispatcher.connected)
        await self._run_session([_message(topic) for topic in received])
        for topic, messages in received.items():
            self.assertEqual([m.topic.value for m in messages], [topic])

    async def test_subscription_made_while_disconnected_goes_out_on_reconnect(self):
        """A subscribe during the outage touches nothing on the client; the reconnect
        subscribes it and its messages arrive."""
        await self._run_session([])
        received = []

        await self.dispatcher.subscribe("late/topic", received.append)

        self.assertEqual(self.client.calls, [])
        await self.dispatcher.connection_restored()
        self.assertEqual(self.client.calls, [("subscribe", "late/topic")])
        await self._run_session([_message("late/topic")])
        self.assertEqual(len(received), 1)

    async def test_subscription_refused_by_the_client_is_kept_for_the_reconnect(self):
        """The client refuses a subscribe while the link still counts as up: the subscription is
        recorded, made on the reconnect and routed."""
        received = []
        self.client.fail("subscribe", "s")

        await self.dispatcher.subscribe("s", received.append)

        self.assertIn("s", self.dispatcher.get_subscribed_topics())
        await self._run_session([])
        self.client.failures.clear()
        await self.dispatcher.connection_restored()
        self.assertEqual(self.client.calls, [("subscribe", "s")])
        await self._run_session([_message("s")])
        self.assertEqual(len(received), 1)

    async def test_subscription_failing_for_another_reason_is_not_recorded(self):
        """A subscribe failing with anything but a link error propagates and leaves no record;
        the reconnect does not make it."""
        self.client.fail("subscribe", "s", RuntimeError("boom"))

        with self.assertRaises(RuntimeError):
            await self.dispatcher.subscribe("s", lambda _message: None)

        self.assertNotIn("s", self.dispatcher.get_subscribed_topics())
        await self._run_session([])
        await self.dispatcher.connection_restored()
        self.assertEqual(self.client.calls, [])

    async def test_unsubscribe_refused_by_the_client_still_drops_the_record(self):
        """The client refuses an unsubscribe while the link still counts as up: the caller sees no
        error and the reconnect does not subscribe the topic again."""
        await self.dispatcher.subscribe("u", lambda _message: None)
        self.client.fail("unsubscribe", "u")

        await self.dispatcher.unsubscribe("u")

        self.assertNotIn("u", self.dispatcher.get_subscribed_topics())
        await self._run_session([])
        self.client.clear()
        await self.dispatcher.connection_restored()
        self.assertEqual(self.client.calls, [])

    async def test_publishes_during_outage_reach_the_broker_once_as_the_last_retained_payload(self):
        """Retained publishes in an outage raise nothing and are replayed once each with the last
        payload; a non-retained one is refused and never replayed."""
        await self._run_session([])

        await self._publish_retained("a", "1")
        await self._publish_retained("a", "2")
        await self._publish_retained("b", "x", qos=1)
        await self._publish_retained("c", None, qos=1)
        with self.assertRaises(BrokerDisconnectedError):
            await self.dispatcher.publish("e", "event", qos=2, retain=False)

        self.assertEqual(self.client.calls, [])
        await self.dispatcher.connection_restored()
        await self.dispatcher.replay_retained()
        self.assertEqual(
            self.client.publishes,
            [Publish("a", "2", 2, True), Publish("b", "x", 1, True), Publish("c", None, 1, True)],
        )
        await self.dispatcher.publish("e", "event", qos=2, retain=False)
        self.assertEqual(self.client.publishes[-1], Publish("e", "event", 2, False))

    async def test_retained_publish_refused_by_the_client_is_left_to_the_replay(self):
        """The client refuses a retained publish while the link still counts as up: the caller
        sees no error and the next session replays the payload."""
        self.client.fail("publish", "t")

        await self._publish_retained("t", "v")

        self.assertEqual(self.client.publishes, [])
        await self._run_session([])
        self.client.failures.clear()
        await self.dispatcher.connection_restored()
        await self.dispatcher.replay_retained()
        self.assertEqual(self.client.publishes, [Publish("t", "v", 2, True)])

    async def test_replay_follows_the_order_of_appearance_and_repeats_clears(self):
        """A cleared error is replayed as a clear in place; a control removed and created again
        is replayed last, the clear of its error staying where it was."""
        base = "/devices/d/controls"
        await self._publish_retained("/devices/d/meta", "{}")
        for control in ("a", "b"):
            await self._publish_retained(f"{base}/{control}/meta", "m")
            await self._publish_retained(f"{base}/{control}", "v")
            await self._publish_retained(f"{base}/{control}/meta/error", "r")
        await self._publish_retained(f"{base}/a/meta/error", None)
        for suffix in ("", "/meta/error", "/meta"):
            await self._publish_retained(f"{base}/b{suffix}", None)
        await self._publish_retained(f"{base}/b/meta", "m2")
        await self._publish_retained(f"{base}/b", "v2")
        await self._run_session([])
        self.client.publishes.clear()

        await self.dispatcher.connection_restored()
        await self.dispatcher.replay_retained()

        self.assertEqual(
            [(p.topic, p.payload) for p in self.client.publishes],
            [
                ("/devices/d/meta", "{}"),
                (f"{base}/a/meta", "m"),
                (f"{base}/a", "v"),
                (f"{base}/a/meta/error", None),
                (f"{base}/b/meta/error", None),
                (f"{base}/b/meta", "m2"),
                (f"{base}/b", "v2"),
            ],
        )

    async def test_replay_holds_up_neither_incoming_messages_nor_the_end_of_the_session(self):
        """A message arriving while the replay waits for an acknowledgement reaches its callback;
        the session failing meanwhile stops the replay."""
        received = []
        await self.dispatcher.subscribe("in", received.append)
        await self._publish_retained("t1", "1")
        await self._publish_retained("t2", "2")
        await self._run_session([])
        self.client.publishes.clear()
        await self.dispatcher.connection_restored()
        self.assertEqual(self.client.publishes, [])
        self.client.ack = asyncio.Event()
        replay = asyncio.create_task(self.dispatcher.replay_retained())
        await asyncio.sleep(0)

        await self._run_session([_message("in")])

        self.assertEqual(len(received), 1)
        self.assertFalse(replay.done())
        self.client.ack.set()
        await replay
        self.assertEqual([p.topic for p in self.client.publishes], ["t1"])

    async def test_live_update_during_replay_ends_up_last_on_the_broker(self):
        """Live updates to the topic being replayed and to one not yet reached: the new payload
        is the last the client sees for both."""
        for topic in ("t1", "t2", "t3"):
            await self._publish_retained(topic, f"old{topic[1]}")
        await self._run_session([])
        self.client.publishes.clear()
        await self.dispatcher.connection_restored()
        self.client.ack = asyncio.Event()
        replay = asyncio.create_task(self.dispatcher.replay_retained())
        await asyncio.sleep(0)

        live = [
            asyncio.create_task(self._publish_retained("t1", "new1")),
            asyncio.create_task(self._publish_retained("t3", "new3")),
        ]
        await asyncio.sleep(0)
        self.client.ack.set()
        await asyncio.gather(replay, *live)

        self.assertEqual(
            {p.topic: p.payload for p in self.client.publishes}, {"t1": "new1", "t2": "old2", "t3": "new3"}
        )

    async def test_outage_during_replay_stops_it_and_the_next_session_replays_everything(self):
        """A failing publish ends the replay with its error; the next session replays everything."""
        for topic in ("t1", "t2", "t3"):
            await self._publish_retained(topic, "v")
        await self._run_session([])
        self.client.publishes.clear()
        await self.dispatcher.connection_restored()
        self.client.fail("publish", "t2")

        with self.assertRaises(aiomqtt.MqttError):
            await self.dispatcher.replay_retained()

        self.assertEqual([p.topic for p in self.client.publishes], ["t1"])
        self.dispatcher.connection_lost()
        self.client.failures.clear()
        self.client.publishes.clear()
        await self.dispatcher.connection_restored()
        await self.dispatcher.replay_retained()
        self.assertEqual([p.topic for p in self.client.publishes], ["t1", "t2", "t3"])
