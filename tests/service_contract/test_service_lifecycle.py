"""Regression tests for the default service lifecycle contract."""

import asyncio
import logging
import os
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiomqtt
from aiomqtt.exceptions import MqttConnectError
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from wb.mqtt_dali.main import BrokerSessions, default_service


class FakeClient:
    def __init__(self, enter_errors=None, stop_requested=None):
        self._enter_errors = list(enter_errors or [])
        self._stop_requested = stop_requested
        self.enter_calls = 0

    async def __aenter__(self):
        error = self._enter_errors[self.enter_calls] if self.enter_calls < len(self._enter_errors) else None
        self.enter_calls += 1
        if error is not None:
            if self._stop_requested is not None:
                self._stop_requested.set()
            raise error
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeDispatcher:
    def __init__(self, drop_connection=False):
        self.connected = True
        self._drop_connection = drop_connection

    async def connection_restored(self):
        self.connected = True

    def connection_lost(self):
        self.connected = False

    async def replay_retained(self):
        return None

    async def run(self):
        if self._drop_connection:
            self._drop_connection = False
            await asyncio.sleep(0)
            raise aiomqtt.MqttError("broker connection lost")
        await asyncio.Event().wait()


class FakeGateway:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self):
        self.start_calls += 1

    async def stop(self):
        self.stop_calls += 1


class TestServiceLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_mqtt_authentication_failure_returns_2(self):
        """Each MQTT v3/v5 authentication refusal stops retries and maps to exit code 2."""
        for reason_code in (
            4,
            5,
            ReasonCode(PacketTypes.CONNACK, identifier=134),
            ReasonCode(PacketTypes.CONNACK, identifier=135),
        ):
            with self.subTest(reason_code=reason_code):
                client = FakeClient([MqttConnectError(reason_code)])
                gateway = FakeGateway()

                result = await BrokerSessions(client, FakeDispatcher(), gateway).run()

                self.assertEqual(result, 2)
                self.assertEqual(client.enter_calls, 1)
                self.assertEqual(gateway.stop_calls, 1)

    async def test_non_connect_code_error_remains_retryable(self):
        """A runtime Paho code matching an old CONNACK value is still retried."""
        client = FakeClient([aiomqtt.MqttCodeError(4), MqttConnectError(135)])

        with patch("wb.mqtt_dali.main.RECONNECT_DELAY_S", 0):
            result = await BrokerSessions(client, FakeDispatcher(), FakeGateway()).run()

        self.assertEqual(result, 2)
        self.assertEqual(client.enter_calls, 2)

    async def test_non_authentication_connack_error_remains_retryable(self):
        """A non-authentication CONNACK failure is retried until a fatal authentication refusal."""
        server_unavailable = ReasonCode(PacketTypes.CONNACK, identifier=136)
        client = FakeClient([MqttConnectError(server_unavailable), MqttConnectError(135)])

        with patch("wb.mqtt_dali.main.RECONNECT_DELAY_S", 0):
            result = await BrokerSessions(client, FakeDispatcher(), FakeGateway()).run()

        self.assertEqual(result, 2)
        self.assertEqual(client.enter_calls, 2)

    async def test_invalid_broker_url_returns_2(self):
        """A malformed MQTT URL is logged and maps to exit code 2."""
        args = SimpleNamespace(config="test.conf", broker_url="mqtt://user:secret@host")

        def invalid_client_factory(_url):
            raise ValueError("No MQTT hostname specified")

        with patch("wb.mqtt_dali.main.load_config", return_value={}), patch(
            "wb.mqtt_dali.main.DaliDatabase", return_value=object()
        ), self.assertLogs(level=logging.ERROR) as logs:
            result = await default_service(
                args,
                client_factory=invalid_client_factory,
            )

        self.assertEqual(result, 2)
        self.assertIn("Invalid MQTT broker URL", "\n".join(logs.output))
        self.assertNotIn("secret", "\n".join(logs.output))

    async def test_signals_during_config_load_return_0(self):
        """SIGINT and SIGTERM raised while loading configuration are handled as clean stops."""
        args = SimpleNamespace(config="test.conf", broker_url="mqtt://localhost")

        for stop_signal in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(stop_signal=stop_signal):
                dispatcher = FakeDispatcher()
                gateway = FakeGateway()

                def interrupt_config(_filepath, signal_to_send=stop_signal):
                    os.kill(os.getpid(), signal_to_send)
                    return {"gateways": []}

                def gateway_factory(*_args, gateway_to_use=gateway):
                    return gateway_to_use

                with patch("wb.mqtt_dali.main.load_config", side_effect=interrupt_config), patch(
                    "wb.mqtt_dali.main.DaliDatabase", return_value=object()
                ), patch("wb.mqtt_dali.main.MQTTDispatcher", return_value=dispatcher), self.assertLogs(
                    level=logging.INFO
                ) as logs:
                    result = await asyncio.wait_for(
                        default_service(
                            args,
                            client_factory=lambda _url: FakeClient(),
                            gateway_factory=gateway_factory,
                        ),
                        timeout=2,
                    )

                self.assertEqual(result, 0)
                self.assertEqual(gateway.stop_calls, 1)
                self.assertIn("test.conf: 0 configured gateway(s)", "\n".join(logs.output))

    async def test_stop_during_outage_reports_uncleared_topics(self):
        """After a live session, brokerless shutdown reports retained cleanup failure and exits 0."""
        stop_requested = asyncio.Event()
        client = FakeClient(
            [None, aiomqtt.MqttError("connection refused")],
            stop_requested=stop_requested,
        )
        dispatcher = FakeDispatcher(drop_connection=True)
        gateway = FakeGateway()

        with patch("wb.mqtt_dali.main.RECONNECT_DELAY_S", 0), self.assertLogs(level=logging.ERROR) as logs:
            result = await BrokerSessions(client, dispatcher, gateway, stop_requested=stop_requested).run()

        self.assertEqual(result, 0)
        self.assertEqual(gateway.stop_calls, 1)
        self.assertIn("Unable to clear retained MQTT topics", "\n".join(logs.output))

    async def test_stop_during_initial_outage_reports_uncleared_topics(self):
        """Brokerless shutdown before the first session reports retained cleanup failure and exits 0."""
        stop_requested = asyncio.Event()
        client = FakeClient(
            [aiomqtt.MqttError("connection refused")],
            stop_requested=stop_requested,
        )
        gateway = FakeGateway()

        with self.assertLogs(level=logging.ERROR) as logs:
            result = await BrokerSessions(
                client, FakeDispatcher(), gateway, stop_requested=stop_requested
            ).run()

        self.assertEqual(result, 0)
        self.assertEqual(gateway.stop_calls, 1)
        self.assertIn("Unable to clear retained MQTT topics", "\n".join(logs.output))
