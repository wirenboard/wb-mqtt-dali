import asyncio
import logging
import unittest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import aiomqtt

from wb.mqtt_dali.common_dali_device import DaliDeviceBase
from wb.mqtt_dali.gateway import BusPollingError, Gateway
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher

from ._app_controller_helpers import LoopKiller, wait_until
from ._broker_link_helpers import RecordingClient
from ._on_off_helpers import BusScript, ScriptedGatewayClient

DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access

_LAST_ENDPOINT_MARKER = "/rpc/v1/wb-mqtt-dali/Bus/ListCommands"


class GatewayRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # A configured device is absent from the bus, and publishing its error state raises
        # past every handler of the polling loop.
        self.client = ScriptedGatewayClient(BusScript(present_shorts=frozenset()), "gw", 1)
        self.client.fail("publish", "/devices/gw_bus_1_5/meta", LoopKiller())
        self.dispatcher = MQTTDispatcher(self.client)
        config = {"gateways": [{"device_id": "gw", "buses": [{"devices": [{"short": 5, "random": 123}]}]}]}
        self.gateway = Gateway(config, self.dispatcher, "/nonexistent.conf", MagicMock(), command_registry={})
        self.dispatch_task: Optional[asyncio.Task] = None

    async def asyncSetUp(self):
        self.dispatch_task = asyncio.create_task(self.dispatcher.run())

    async def asyncTearDown(self):
        self.dispatch_task.cancel()

    async def test_run_raises_when_a_bus_polling_loop_dies(self):
        """The polling loop of the only bus dies: run() raises BusPollingError."""
        with self.assertRaises(BusPollingError):
            await self._run()
        await self.gateway.stop()

    async def test_stop_after_a_bus_polling_loop_died_removes_the_bus_topics(self):
        """run() has raised BusPollingError: stop() completes and clears the retained topics of the bus."""
        with self.assertRaises(BusPollingError):
            await self._run()
        self.client.failures.clear()
        self.client.clear()

        await self.gateway.stop()

        self.assertIn(
            ("/devices/gw_bus_1_broadcast/meta", None), [(p.topic, p.payload) for p in self.client.publishes]
        )

    # --- Private ---

    async def _run(self) -> None:
        with patch("wb.mqtt_dali.gateway.remove_topics_by_driver", AsyncMock()), patch(
            "wb.mqtt_dali.gateway.wait_for_rpc_endpoint", AsyncMock()
        ), patch("wb.mqtt_dali.gateway.rpc_call", AsyncMock(side_effect=asyncio.TimeoutError)):
            await asyncio.wait_for(self.gateway.run(), timeout=5.0)


class GatewayWaitForWbMqttSerialTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_wb_mqtt_serial_then_starts_the_buses(self):
        """No config/Load RPC: one CRITICAL without traceback, run() keeps waiting and publishes nothing;
        wb-mqtt-serial publishes the RPC: the buses and the RPC endpoints come up."""
        client = RecordingClient()
        dispatcher = MQTTDispatcher(client)
        dispatch_task = asyncio.create_task(dispatcher.run())
        self.addCleanup(dispatch_task.cancel)
        config = {"gateways": [{"device_id": "gw", "buses": [{"devices": []}]}]}
        gateway = Gateway(config, dispatcher, "/nonexistent.conf", MagicMock(), command_registry={})

        with patch("wb.mqtt_dali.gateway.WB_MQTT_SERIAL_RPC_WAIT_BEFORE_REPORT_S", 0.01), patch(
            "wb.mqtt_dali.gateway.remove_topics_by_driver", AsyncMock()
        ), patch(
            "wb.mqtt_dali.gateway.rpc_call", AsyncMock(side_effect=asyncio.TimeoutError)
        ), self.assertLogs(
            level=logging.INFO
        ) as logs:
            run_task = asyncio.create_task(gateway.run())
            await asyncio.wait_for(
                wait_until(lambda: any(r.levelno == logging.CRITICAL for r in logs.records)), timeout=5.0
            )
            await asyncio.sleep(0.1)

            self.assertFalse(run_task.done())
            self.assertEqual([p.topic for p in client.publishes], [])

            client.messages.deliver(
                aiomqtt.Message("/rpc/v1/wb-mqtt-serial/config/Load", "1", 0, True, 0, None)
            )
            await asyncio.wait_for(
                wait_until(lambda: _LAST_ENDPOINT_MARKER in [p.topic for p in client.publishes]), timeout=5.0
            )
        await gateway.stop()
        await asyncio.wait_for(run_task, timeout=5.0)

        critical = [r for r in logs.records if r.levelno == logging.CRITICAL]
        self.assertEqual(len(critical), 1)
        self.assertIsNone(critical[0].exc_info)
        self.assertTrue(any(p.topic.startswith("/devices/gw_bus_1") for p in client.publishes))
