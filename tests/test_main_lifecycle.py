"""Exit codes of the service: a rejected login, an early stop, a broken broker URL."""

import asyncio
import logging
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiomqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from wb.mqtt_dali.main import EXIT_INVALIDARGUMENT, EXIT_SUCCESS, default_service, main
from wb.mqtt_dali.wbmqtt import parse_broker_url

from .test_main_reconnect import (
    _FakeClient,
    _FakeDispatcher,
    _FakeGateway,
    _FakeSignals,
    _SessionStream,
)


class _RejectingClient:
    """Every session attempt ends like a broker that refuses the credentials."""

    def __init__(self, reason_code):
        self.enter_calls = 0
        self._reason_code = reason_code

    async def __aenter__(self):
        self.enter_calls += 1
        raise aiomqtt.MqttCodeError(self._reason_code, "Connection refused")

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _service_args():
    return SimpleNamespace(config="x", broker_url="unix:///var/run/mosquitto/mosquitto.sock")


class TestDefaultServiceExitCodes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.dispatcher = _FakeDispatcher([_SessionStream.HOLD], self.events)
        self.gateway = _FakeGateway(self.dispatcher, self.events)
        self.signals = _FakeSignals()
        self.patches = [
            patch("wb.mqtt_dali.main.load_config", return_value={}),
            patch("wb.mqtt_dali.main.DaliDatabase", return_value=object()),
            patch("wb.mqtt_dali.main.MQTTDispatcher", return_value=self.dispatcher),
            patch("wb.mqtt_dali.main.RECONNECT_DELAY_S", 0.001),
            self.signals.patch_loop(),
        ]
        for one in self.patches:
            one.start()
            self.addCleanup(one.stop)

    async def _run_service(self, client) -> int:
        return await asyncio.wait_for(
            default_service(
                _service_args(),
                client_factory=lambda _url: client,
                gateway_factory=lambda *_a: self.gateway,
            ),
            timeout=5.0,
        )

    async def test_rejected_login_exits_with_2_without_reconnecting(self):
        """CONNACK 135 (not authorized) ends the service at once with 2, no reconnect attempts."""
        for reason_code in (ReasonCode(PacketTypes.CONNACK, "Not authorized"), 134):
            with self.subTest(reason_code=reason_code):
                client = _RejectingClient(reason_code)

                self.assertEqual(await self._run_service(client), EXIT_INVALIDARGUMENT)
                self.assertEqual(client.enter_calls, 1)

    async def test_sigterm_while_loading_exits_with_0(self):
        """The handlers are installed before the config load: a SIGTERM there ends the service with 0
        instead of killing it, and the loaded (empty) config is logged."""
        client = _FakeClient()

        def load_config_and_stop(_path):
            self.signals.handlers[signal.SIGTERM]()
            return {}

        with patch("wb.mqtt_dali.main.load_config", side_effect=load_config_and_stop), self.assertLogs(
            level=logging.INFO
        ) as logs:
            self.assertEqual(await self._run_service(client), EXIT_SUCCESS)

        self.assertTrue(any("loaded: 0 gateway(s)" in line for line in logs.output))
        self.assertFalse(any("cannot be removed" in line for line in logs.output))

    async def test_stop_during_an_outage_logs_that_the_topics_stay(self):
        """After a session was up, a stop with the link down cannot remove the retained topics: one
        error line says so."""
        client = _FakeClient(sessions_before_refusing=1)
        self.dispatcher = _FakeDispatcher([_SessionStream.DROP], self.events)
        self.gateway = _FakeGateway(self.dispatcher, self.events)
        patch("wb.mqtt_dali.main.MQTTDispatcher", return_value=self.dispatcher).start()

        async def stop_once_refused():
            while client.enter_calls < 3:
                await asyncio.sleep(0.001)
            self.signals.handlers[signal.SIGTERM]()

        stopper = asyncio.create_task(stop_once_refused())
        with self.assertLogs(level=logging.ERROR) as logs:
            self.assertEqual(await self._run_service(client), EXIT_SUCCESS)
        stopper.cancel()

        self.assertTrue(any("retained topics cannot be removed" in line for line in logs.output))


class TestBrokerUrlArgument(unittest.IsolatedAsyncioTestCase):
    async def test_a_broken_broker_url_is_an_argument_error(self):
        for url in ("bogus://x", "tcp://localhost", "unix://"):
            with self.subTest(url=url), self.assertRaises(SystemExit) as raised:
                await main(["wb-mqtt-dali", "-b", url, "--list-commands"])
            self.assertEqual(raised.exception.code, 2)


class TestParseBrokerUrl(unittest.TestCase):
    def test_transports(self):
        self.assertEqual(
            parse_broker_url("unix:///var/run/mosquitto/mosquitto.sock"),
            {"transport": "unix", "hostname": "/var/run/mosquitto/mosquitto.sock"},
        )
        self.assertEqual(
            parse_broker_url("tcp://user:pw@host:1883"),
            {
                "transport": "tcp",
                "hostname": "host",
                "port": 1883,
                "username": "user",
                "password": "pw",
            },
        )
        self.assertEqual(parse_broker_url("ws://host:9001")["transport"], "websockets")
