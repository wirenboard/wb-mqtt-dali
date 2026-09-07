"""The service across broker sessions, driven with fakes; the stop request is a real SIGTERM."""

import asyncio
import os
import signal
import unittest
from enum import Enum
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import patch

import aiomqtt

from wb.mqtt_dali.main import default_service
from wb.mqtt_dali.wbmqtt import make_mqtt_client


class _FakeClient:
    """Entering is a connected session; after `sessions_before_refusing` of them every enter
    fails like an unreachable broker. Exiting is a no-op."""

    def __init__(self, sessions_before_refusing: Optional[int] = None):
        self.enter_calls = 0
        self._sessions_before_refusing = sessions_before_refusing

    async def __aenter__(self):
        self.enter_calls += 1
        if self._sessions_before_refusing is not None and self.enter_calls > self._sessions_before_refusing:
            raise aiomqtt.MqttError("connection refused")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SessionStream(Enum):
    DROP = "fails once the session has settled"
    END = "ends without an error"
    HOLD = "serves until cancelled"


class _FakeDispatcher:
    """`run_behaviours[i]` is how the i-th session's message stream behaves; the calls go to `events`."""

    def __init__(self, run_behaviours, events: List[str]):
        self._run_behaviours = list(run_behaviours)
        self._events = events
        self.connected = True
        self.run_calls = 0
        self.replay_calls = 0
        self.replayed_after_reconnect = asyncio.Event()

    async def connection_restored(self):
        self.connected = True
        self._events.append("restored")

    async def replay_retained(self):
        self.replay_calls += 1
        self._events.append("replay")
        if self.replay_calls > 1:
            self.replayed_after_reconnect.set()

    def connection_lost(self):
        if self.connected:
            self.connected = False
            self._events.append("lost")

    async def run(self):
        behaviour = self._run_behaviours[self.run_calls]
        self.run_calls += 1
        if behaviour is _SessionStream.HOLD:
            await asyncio.Event().wait()
        await asyncio.sleep(0)
        if behaviour is _SessionStream.DROP:
            raise aiomqtt.MqttError("broker connection lost")


class _FakeGateway:
    def __init__(self, dispatcher: _FakeDispatcher, events: List[str]):
        self._dispatcher = dispatcher
        self._events = events
        self.start_calls = 0
        self.stop_calls = 0
        self.connected_at_stop = None

    async def start(self):
        self.start_calls += 1
        self._events.append("start")

    async def stop(self):
        self.stop_calls += 1
        self.connected_at_stop = self._dispatcher.connected
        self._events.append("stop")


async def _poll_until(condition):
    while not condition():
        await asyncio.sleep(0.001)


class TestDefaultServiceReconnect(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events: List[str] = []

    async def _run_service(self, client, dispatcher, gateway, stop_when) -> int:
        async def send_sigterm():
            # `stop_when` completes inside a session, after the service installed its handlers.
            await stop_when()
            os.kill(os.getpid(), signal.SIGTERM)

        with patch("wb.mqtt_dali.main.load_config", return_value={}), patch(
            "wb.mqtt_dali.main.DaliDatabase", return_value=object()
        ), patch("wb.mqtt_dali.main.MQTTDispatcher", return_value=dispatcher), patch(
            "wb.mqtt_dali.main.RECONNECT_DELAY_S", 0.001
        ):
            stopper = asyncio.create_task(send_sigterm())
            try:
                return await asyncio.wait_for(
                    default_service(
                        SimpleNamespace(config="x", broker_url="y"),
                        client_factory=lambda _url: client,
                        gateway_factory=lambda *_a: gateway,
                    ),
                    timeout=5.0,
                )
            finally:
                stopper.cancel()

    async def test_lost_link_keeps_the_buses_and_replays_state_on_reconnect(self):
        """A dropped session leaves the gateway alone; the next one replays the mirror; SIGTERM
        then stops the gateway once."""
        client = _FakeClient()
        dispatcher = _FakeDispatcher([_SessionStream.DROP, _SessionStream.HOLD], self.events)
        gateway = _FakeGateway(dispatcher, self.events)

        result = await self._run_service(
            client, dispatcher, gateway, dispatcher.replayed_after_reconnect.wait
        )

        self.assertEqual(result, 0)
        self.assertEqual(gateway.start_calls, 1)
        self.assertEqual(client.enter_calls, 2)
        self.assertEqual(self.events, ["restored", "start", "replay", "lost", "restored", "replay", "stop"])

    async def test_a_message_stream_that_ends_cleanly_is_a_lost_link_too(self):
        """The dispatcher returns from a session without an error; the service reconnects and
        replays as after a drop."""
        client = _FakeClient()
        dispatcher = _FakeDispatcher([_SessionStream.END, _SessionStream.HOLD], self.events)
        gateway = _FakeGateway(dispatcher, self.events)

        result = await self._run_service(
            client, dispatcher, gateway, dispatcher.replayed_after_reconnect.wait
        )

        self.assertEqual(result, 0)
        self.assertEqual(client.enter_calls, 2)
        self.assertEqual(self.events, ["restored", "start", "replay", "lost", "restored", "replay", "stop"])

    async def test_stop_with_the_link_up_stops_the_gateway_before_disconnecting(self):
        """SIGTERM with the session up: the gateway is stopped while the link is still connected,
        and no loss is recorded."""
        client = _FakeClient()
        dispatcher = _FakeDispatcher([_SessionStream.HOLD], self.events)
        gateway = _FakeGateway(dispatcher, self.events)

        result = await self._run_service(
            client, dispatcher, gateway, lambda: _poll_until(lambda: gateway.start_calls == 1)
        )

        self.assertEqual(result, 0)
        self.assertEqual(gateway.stop_calls, 1)
        self.assertTrue(gateway.connected_at_stop)
        self.assertNotIn("lost", self.events)

    async def test_stop_during_outage_ends_the_service_without_a_session(self):
        """The link drops and every reconnect attempt is refused; SIGTERM ends the reconnect
        loop, the gateway is stopped with the link down and nothing is republished."""
        client = _FakeClient(sessions_before_refusing=1)
        dispatcher = _FakeDispatcher([_SessionStream.DROP], self.events)
        gateway = _FakeGateway(dispatcher, self.events)

        result = await self._run_service(
            client, dispatcher, gateway, lambda: _poll_until(lambda: client.enter_calls >= 3)
        )

        self.assertEqual(result, 0)
        self.assertEqual(gateway.stop_calls, 1)
        self.assertFalse(gateway.connected_at_stop)
        self.assertEqual(self.events, ["restored", "start", "replay", "lost", "stop"])


class TestMqttClientFactory(unittest.TestCase):
    def test_client_keepalive_is_15_s(self):
        with patch("wb.mqtt_dali.wbmqtt.aiomqtt.Client") as client_cls:
            make_mqtt_client("unix:///var/run/mosquitto/mosquitto.sock")

        self.assertEqual(client_cls.call_args.kwargs["keepalive"], 15)
